"""In-process verification gates via agent hook surfaces (issue #2360).

Verification today happens *after* a worker finishes: the scheduler inspects
results and re-dispatches on failure, paying a full round-trip for every miss.
Several agent runtimes expose in-process enforcement surfaces -- Claude Code
hooks with blocking exit codes and a tool-permission matcher -- that can run our
checks the moment a worker believes it is done, inside the same session, before
the turn ends. This module is the adapter-agnostic policy + receipt core those
hooks drive.

Trust model
-----------
In-process hooks are **defence-in-depth and a cost optimisation**. The
scheduler-side evidence gate (:mod:`bernstein.core.evidence.completion_gate`)
remains authoritative and runs regardless. The point of the in-process gate is
to refuse the cheap miss early, not to replace the authoritative check.

The artefact IS the receipt
---------------------------
A gate outcome is never "a status plus a log". Every recordable outcome -- a
blocked completion or a refused out-of-scope write -- is sealed as an
:class:`~bernstein.core.evidence.bundle.EvidenceBundle`: the exact signed,
spine-anchored, HMAC-chained proof-of-done record issue #2362 defined. We do
**not** fork a second receipt schema. A downstream verifier holding the stored
blobs recomputes the bundle byte-identically and cannot tell, from the schema,
whether enforcement happened in-process or scheduler-side (AC3). Strip the
lineage spine and the audit chain and the receipt is just a file; anchored and
signed it is a chain-verifiable attestation.

Security (path allowlist)
-------------------------
The path matcher decides whether a Write/Edit target is in scope. The decision
is realpath-containment-checked: the candidate is resolved (following symlinks)
and must stay under the worktree root, so a ``..`` traversal or an in-scope
symlink that points outside the tree is refused before any textual prefix can
match (CodeQL path-injection hardening).
"""

from __future__ import annotations

import fnmatch
import json
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bernstein.core.evidence.bundle import (
    EvidenceProducer,
    ProducerOutcome,
    build_evidence_bundle,
    load_or_create_evidence_identity,
    parse_producers,
    run_producers,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from bernstein.core.evidence.bundle import EvidenceBundle

__all__ = [
    "ALLOW_EXIT_CODE",
    "BLOCKING_EXIT_CODE",
    "EDIT_TOOL_NAMES",
    "GateOutcome",
    "HookGatePolicy",
    "evaluate_completion_gate",
    "evaluate_path_gate",
    "path_within_allowlist",
    "policy_from_task_fields",
    "read_policy",
    "seal_gate_receipt",
    "write_policy",
]

#: Claude Code (and compatible surfaces) treat a hook exit of ``2`` as a hard
#: block: a PreToolUse hook refuses the tool call, a Stop hook refuses to let
#: the turn end. ``0`` allows the action through.
BLOCKING_EXIT_CODE = 2
ALLOW_EXIT_CODE = 0

#: Tool names whose invocation writes to the filesystem and is therefore
#: subject to the path allowlist. Any other tool (Bash, Read, ...) is out of
#: this gate's scope and passes through untouched.
EDIT_TOOL_NAMES = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})

#: Name of the synthetic producer used to seal a path-refusal receipt. The
#: refusal is recorded through the same evidence-bundle schema as a real
#: producer so the receipt is schema-identical to a completion receipt.
_PATH_REFUSAL_PRODUCER = "path_allowlist"

_POLICY_SUBPATH = (".sdd", "runtime", "hook_gate")
_POLICY_SCHEMA_VERSION = 1

# Keys Claude Code uses to carry the target path in a tool_input payload.
_PATH_KEYS = ("file_path", "notebook_path", "path", "filePath")


# ---------------------------------------------------------------------------
# Policy (derived from the task spec: owned_files + evidence_producers)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class HookGatePolicy:
    """The in-process gate policy for one task.

    The policy reuses task-spec fields that already exist: ``owned_files``
    becomes the write-path allowlist, and the *required* ``evidence_producers``
    become the completion verification the gate runs in-session. No new
    task-spec surface is introduced.

    Attributes:
        task_id: The task the policy governs.
        producers: The declared evidence producers; the *required* ones gate
            completion in-session (the same producers the scheduler-side gate
            runs, so the receipts share a schema).
        path_allowlist: Glob patterns (worktree-relative) an edit tool may
            write under. Empty means "no path enforcement" -- a pass-through.
    """

    task_id: str
    producers: tuple[EvidenceProducer, ...] = ()
    path_allowlist: tuple[str, ...] = ()

    @property
    def enforces_paths(self) -> bool:
        """True when at least one allowlist pattern is declared."""
        return any(p.strip() for p in self.path_allowlist)

    @property
    def enforces_completion(self) -> bool:
        """True when at least one *required* producer gates completion."""
        return any(p.required for p in self.producers)

    @property
    def is_active(self) -> bool:
        """True when the policy enforces either paths or completion."""
        return self.enforces_paths or self.enforces_completion

    def to_dict(self) -> dict[str, Any]:
        return {
            "v": _POLICY_SCHEMA_VERSION,
            "task_id": self.task_id,
            "producers": [p.to_dict() for p in self.producers],
            "path_allowlist": list(self.path_allowlist),
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> HookGatePolicy:
        producers = parse_producers(list(row.get("producers", [])))
        allow = tuple(str(p) for p in row.get("path_allowlist", []))
        return cls(task_id=str(row["task_id"]), producers=producers, path_allowlist=allow)


def policy_from_task_fields(
    task_id: str,
    *,
    owned_files: Sequence[str],
    evidence_producers: Sequence[dict[str, Any]],
) -> HookGatePolicy:
    """Build a :class:`HookGatePolicy` from raw task-spec fields.

    Args:
        task_id: The task id.
        owned_files: The task's declared write scope (the path allowlist).
        evidence_producers: The task's declared evidence producers.
    """
    return HookGatePolicy(
        task_id=task_id,
        producers=parse_producers(list(evidence_producers)),
        path_allowlist=tuple(owned_files),
    )


def _policy_path(workdir: Path, session_id: str) -> Path:
    return Path(workdir).joinpath(*_POLICY_SUBPATH, f"{session_id}.json")


def write_policy(workdir: Path, session_id: str, policy: HookGatePolicy) -> Path:
    """Persist ``policy`` for ``session_id`` so the in-session hook can read it.

    The file is written at spawn time by the orchestrator; the hook process
    reads it at gate time. ``session_id`` is used verbatim as a filename
    component, so callers must pass an already-validated value.
    """
    path = _policy_path(workdir, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(policy.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    return path


def read_policy(workdir: Path, session_id: str) -> HookGatePolicy | None:
    """Return the persisted policy for ``session_id`` or ``None`` if absent."""
    path = _policy_path(workdir, session_id)
    if not path.is_file():
        return None
    try:
        return HookGatePolicy.from_dict(json.loads(path.read_text(encoding="utf-8")))
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Path allowlist decision (realpath containment + glob)
# ---------------------------------------------------------------------------


def path_within_allowlist(
    candidate: str,
    *,
    workdir: Path,
    allowlist: Sequence[str],
) -> bool:
    """Return True when ``candidate`` is an in-scope write under ``workdir``.

    An empty allowlist means "no path enforcement" and returns True (AC4).
    Otherwise the candidate is resolved under the worktree root and must both
    stay contained (realpath containment; a ``..`` or symlink escape is
    refused) and match at least one allowlist pattern by glob or directory
    prefix.
    """
    patterns = [p.strip() for p in allowlist if p.strip()]
    if not patterns:
        return True

    root = Path(workdir).resolve()
    cand = Path(candidate)
    if not cand.is_absolute():
        cand = root / cand
    try:
        resolved = cand.resolve()
    except (OSError, RuntimeError):
        return False
    # Realpath containment: the resolved target must stay under the worktree.
    if resolved != root and not resolved.is_relative_to(root):
        return False
    rel = resolved.relative_to(root).as_posix()

    for pattern in patterns:
        norm = pattern.rstrip("/")
        if rel == norm or rel.startswith(norm + "/"):
            return True
        if fnmatch.fnmatch(rel, pattern) or fnmatch.fnmatch(rel, norm + "/*"):
            return True
    return False


# ---------------------------------------------------------------------------
# Gate outcomes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class GateOutcome:
    """The result of evaluating one gate event.

    Attributes:
        event: ``"pretooluse"`` (path matcher) or ``"completion"`` (Stop gate).
        gate_passed: True when the action is allowed, False when it is blocked.
        reason: Human-readable explanation (empty on pass).
        outcomes: The producer outcomes to seal into the receipt. For a path
            refusal this is a single synthetic refusal producer; for a
            completion gate it is every producer that ran.
    """

    event: str
    gate_passed: bool
    reason: str
    outcomes: tuple[ProducerOutcome, ...]

    @property
    def blocked(self) -> bool:
        """True when the action is refused (the inverse of ``gate_passed``)."""
        return not self.gate_passed

    @property
    def exit_code(self) -> int:
        """The hook process exit code enforcing this outcome in-session."""
        return ALLOW_EXIT_CODE if self.gate_passed else BLOCKING_EXIT_CODE


def _extract_target_path(tool_input: dict[str, Any]) -> str | None:
    for key in _PATH_KEYS:
        value = tool_input.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def evaluate_path_gate(
    policy: HookGatePolicy,
    *,
    tool_name: str,
    tool_input: dict[str, Any],
    workdir: Path,
) -> GateOutcome | None:
    """Evaluate the path allowlist for one PreToolUse event.

    Returns ``None`` -- meaning "not this gate's concern, pass through" -- when
    the tool is not an edit tool, when no path enforcement is configured (AC4),
    or when the payload carries no target path. Otherwise returns a
    :class:`GateOutcome`: blocked (with a synthetic refusal producer to seal)
    for an out-of-scope write, allowed otherwise.
    """
    if tool_name not in EDIT_TOOL_NAMES or not policy.enforces_paths:
        return None
    target = _extract_target_path(tool_input)
    if target is None:
        return None

    if path_within_allowlist(target, workdir=workdir, allowlist=policy.path_allowlist):
        return GateOutcome(event="pretooluse", gate_passed=True, reason="", outcomes=())

    reason = f"out-of-scope write refused: {target} not under allowlist {sorted(policy.path_allowlist)}"
    refusal = ProducerOutcome(
        producer=EvidenceProducer(
            name=_PATH_REFUSAL_PRODUCER,
            kind="generic",
            command=(),
            required=True,
        ),
        exit_code=1,
        output=reason.encode("utf-8"),
    )
    return GateOutcome(event="pretooluse", gate_passed=False, reason=reason, outcomes=(refusal,))


def evaluate_completion_gate(
    policy: HookGatePolicy,
    *,
    workdir: Path,
    runner: Callable[[EvidenceProducer], tuple[int, bytes]] | None = None,
    producer_timeout_s: int = 600,
) -> GateOutcome:
    """Run the task's *required* producers in-session and decide completion.

    Runs every required producer (the same producers the scheduler-side
    evidence gate runs) via ``runner`` or a subprocess runner rooted at
    ``workdir``. The gate passes iff every required producer exits 0; a failure
    blocks the turn (exit code 2). When no required producer is declared the
    gate is a pass-through (AC4).
    """
    required = tuple(p for p in policy.producers if p.required)
    if not required:
        return GateOutcome(event="completion", gate_passed=True, reason="", outcomes=())

    active_runner = runner
    if active_runner is None:
        from bernstein.core.evidence.bundle import _default_runner

        active_runner = _default_runner(Path(workdir), timeout=producer_timeout_s)

    outcomes = run_producers(required, runner=active_runner)
    failed = tuple(o for o in outcomes if not o.passed)
    gate_passed = not failed
    reason = ""
    if failed:
        names = ", ".join(o.producer.name for o in failed)
        reason = f"completion blocked: required verification failed ({names})"
    return GateOutcome(event="completion", gate_passed=gate_passed, reason=reason, outcomes=outcomes)


# ---------------------------------------------------------------------------
# Receipt sealing (reuses the issue #2362 evidence-bundle schema)
# ---------------------------------------------------------------------------


def seal_gate_receipt(
    *,
    workdir: Path,
    task_id: str,
    outcomes: Sequence[ProducerOutcome],
    timestamp: int,
    hmac_key: bytes | None = None,
) -> EvidenceBundle:
    """Seal ``outcomes`` into a signed, chain-anchored gate receipt.

    The receipt IS an :class:`~bernstein.core.evidence.bundle.EvidenceBundle`
    -- the same schema issue #2362 defined -- built via ``build_evidence_bundle``
    and mirrored into the HMAC audit chain, so it is byte-identical in schema to
    a scheduler-side bundle (AC3). Resolves the evidence identity, HMAC key, and
    audit chain under ``workdir/.sdd``.
    """
    workdir = Path(workdir)
    if hmac_key is None:
        from bernstein.core.security.audit import load_or_create_audit_key

        hmac_key = load_or_create_audit_key()
    private_pem, public_pem = load_or_create_evidence_identity(workdir / ".sdd" / "identity")

    from bernstein.core.security.audit_chain import AuditChainStore

    chain = AuditChainStore(workdir / ".sdd" / "audit", key=hmac_key)
    return build_evidence_bundle(
        workdir=workdir,
        lineage_root=workdir / ".sdd" / "lineage",
        hmac_key=hmac_key,
        private_key_pem=private_pem,
        public_key_pem=public_pem,
        task_id=task_id,
        outcomes=outcomes,
        timestamp=timestamp,
        chain=chain,
    )
