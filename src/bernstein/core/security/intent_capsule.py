"""Intent capsules: signed task-goal receipts with deterministic drift escalation.

Issue #2514. A task goal is free text. Plan approval signs off cost and risk,
attested approvals cover single tool calls, and hook gates cover completion --
but nothing binds the running worker's action stream to the goal the operator
actually approved. When a worker drifts (touches files outside scope, switches
to an action class nobody asked for, starts communicating externally on a
refactoring task), the drift is only discoverable in retrospect by reading the
journal.

This module compiles the approved goal into an **intent capsule** at approval
time: a canonical structured envelope (same canonical byte form as lineage
entries) listing the allowed action classes (capability vocabulary from
``templates/capabilities/surfaces.yaml``), file-scope globs, permitted adapter
set, egress classes, a cost-envelope reference, and an expiry. The capsule is
written to the HMAC audit chain and its hash is bound into the run journal, so
every subsequent journal step is attributable to one approved capsule.

The killer shape: the capsule IS a signed chain entry and the drift escalation
IS a signed receipt referencing it. Strip the audit chain and the run journal
and the feature collapses to a goal string with a log.

Determinism / no-LLM contract
------------------------------
:func:`classify_journal_event` and :func:`evaluate_conformance` are **pure**
functions of ``(journal, capsule)``. They read no clock, open no socket, and
call no model. Two verifiers on different machines recompute the byte-identical
:class:`ConformanceVerdict`. The drift-decision path imports nothing from an LLM
provider or adapter; :func:`assert_no_llm_imports` is the static guard and the
test-suite adds a runtime profiler assertion.
"""

from __future__ import annotations

import ast
import hashlib
import json
import types
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.orchestration.escalation import EscalationReceipt
    from bernstein.core.replay.journal import EventJournal
    from bernstein.core.security.audit import AuditEvent
    from bernstein.core.security.audit_chain import AuditChainStore
    from bernstein.core.tasks.models import TaskPlan

# ---------------------------------------------------------------------------
# Schema version + LLM-free marker
# ---------------------------------------------------------------------------

#: Version stamped into every capsule binding preimage. Bump only on a
#: wire-format change; canonicalisation and hashing key off it.
INTENT_CAPSULE_VERSION = 1

#: Journal event type recorded when a capsule is bound to a run. Every step
#: after this anchor is attributable to the one approved capsule.
CAPSULE_BOUND_EVENT = "intent.capsule_bound"

#: Marks this module as free of any LLM/model dependency on the drift-decision
#: path. Enforced statically by :func:`assert_no_llm_imports` and at runtime by
#: :func:`_assert_llm_free_runtime`.
LLM_FREE = True

#: Import prefixes that would put a model/provider on the drift-decision path.
_LLM_IMPORT_DENYLIST: tuple[str, ...] = (
    "anthropic",
    "openai",
    "litellm",
    "cohere",
    "mistralai",
    "ollama",
    "google.generativeai",
    "vertexai",
    "bernstein.adapters",
)


class IntentCapsuleError(RuntimeError):
    """Raised when a capsule cannot be compiled, stored, or read."""


# ---------------------------------------------------------------------------
# Deterministic event -> action-class mapping (reviewed data, no LLM)
# ---------------------------------------------------------------------------

#: Static map from a worker tool name to a capability surface (action class).
#: The surface vocabulary mirrors ``templates/capabilities/surfaces.yaml``.
#: Reviewed data, not inference: a tool name deterministically resolves to one
#: action class or to nothing.
_TOOL_TO_ACTION_CLASS: dict[str, str] = {
    "read": "fs.read",
    "read_file": "fs.read",
    "cat": "fs.read",
    "view": "fs.read",
    "write": "fs.write",
    "write_file": "fs.write",
    "edit": "fs.write",
    "edit_file": "fs.write",
    "apply_patch": "fs.write",
    "multiedit": "fs.write",
    "delete": "fs.delete",
    "rm": "fs.delete",
    "bash": "shell.exec",
    "shell": "shell.exec",
    "run_command": "shell.exec",
    "exec": "shell.exec",
    "webfetch": "web.fetch",
    "web_fetch": "web.fetch",
    "fetch": "web.fetch",
    "websearch": "web.search",
    "web_search": "web.search",
    "search": "web.search",
    "git_commit": "git.commit",
    "git_push": "git.push",
    "gh_issue_comment": "github.post_comment",
    "gh_pr_comment": "github.post_comment",
    "gh_pr_create": "github.post_pr",
    "gh_issue_create": "github.post_issue",
}

#: Static map from a journal event type to an action class. Applied only when
#: neither an explicit ``action_class`` nor a mappable ``tool`` is present.
_EVENT_TO_ACTION_CLASS: dict[str, str] = {
    "fs.read": "fs.read",
    "fs.write": "fs.write",
    "web.fetch": "web.fetch",
    "web.search": "web.search",
    "git.commit": "git.commit",
    "git.push": "git.push",
    "shell.exec": "shell.exec",
}

#: Action classes that carry outbound communication. An observed action class in
#: this set requires ``external_comm`` in the capsule's ``egress_classes``.
_EXTERNAL_COMM_ACTION_CLASSES: frozenset[str] = frozenset(
    {
        "web.fetch",
        "web.search",
        "git.push",
        "github.post_comment",
        "github.post_pr",
        "github.post_issue",
        "shell.exec",
    }
)


def classify_journal_event(event: dict[str, Any]) -> str | None:
    """Return the action class an observed journal event maps to, or ``None``.

    Resolution order (deterministic, no inference):

    1. an explicit truthy ``action_class`` field stamped by the worker/adapter,
    2. the ``tool`` field mapped through :data:`_TOOL_TO_ACTION_CLASS`,
    3. the ``event`` type mapped through :data:`_EVENT_TO_ACTION_CLASS`.

    Ticks, snapshots, and capsule-binding events carry no action class and
    return ``None`` so they never count as drift.
    """
    explicit = event.get("action_class")
    if isinstance(explicit, str) and explicit:
        return explicit
    tool = event.get("tool")
    if isinstance(tool, str) and tool:
        mapped = _TOOL_TO_ACTION_CLASS.get(tool) or _TOOL_TO_ACTION_CLASS.get(tool.lower())
        if mapped:
            return mapped
    event_type = event.get("event")
    if isinstance(event_type, str) and event_type:
        return _EVENT_TO_ACTION_CLASS.get(event_type)
    return None


# ---------------------------------------------------------------------------
# Capsule schema + canonicalisation (mirrors core/lineage/entry.py)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class IntentCapsule:
    """The approved goal compiled into a canonical, signable envelope.

    Frozen + slots so the dataclass shape itself is canonical: no surprise
    attribute can mutate the byte form. The free-text goal is bound by
    ``goal_digest`` and never stored verbatim.

    Attributes:
        v: Schema version (:data:`INTENT_CAPSULE_VERSION`).
        task_id: The task the capsule governs.
        plan_id: The approved :class:`TaskPlan` id the capsule compiled from.
        goal_digest: ``sha256:`` digest of the approved goal text.
        allowed_action_classes: Sorted capability surfaces the worker may use.
        file_scope_globs: Sorted globs the worker's writes are scoped to.
        permitted_adapters: Sorted adapter names permitted to execute.
        egress_classes: Sorted egress capability axes permitted (for example
            ``["external_comm"]``; empty means no outbound communication).
        cost_envelope_ref: ``sha256:`` reference to the approved cost envelope.
        expiry_ts: Integer Unix timestamp after which the capsule is stale.
    """

    v: int
    task_id: str
    plan_id: str
    goal_digest: str
    allowed_action_classes: tuple[str, ...]
    file_scope_globs: tuple[str, ...]
    permitted_adapters: tuple[str, ...]
    egress_classes: tuple[str, ...]
    cost_envelope_ref: str
    expiry_ts: int

    def to_canonical_dict(self) -> dict[str, Any]:
        """Return the JCS-canonical mapping (lists, sorted at compile time)."""
        return {
            "v": self.v,
            "task_id": self.task_id,
            "plan_id": self.plan_id,
            "goal_digest": self.goal_digest,
            "allowed_action_classes": list(self.allowed_action_classes),
            "file_scope_globs": list(self.file_scope_globs),
            "permitted_adapters": list(self.permitted_adapters),
            "egress_classes": list(self.egress_classes),
            "cost_envelope_ref": self.cost_envelope_ref,
            "expiry_ts": self.expiry_ts,
        }

    def to_dict(self) -> dict[str, Any]:
        """Alias of :meth:`to_canonical_dict` used for on-disk storage."""
        return self.to_canonical_dict()

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> IntentCapsule:
        return cls(
            v=int(row["v"]),
            task_id=str(row["task_id"]),
            plan_id=str(row["plan_id"]),
            goal_digest=str(row["goal_digest"]),
            allowed_action_classes=tuple(str(x) for x in row.get("allowed_action_classes", [])),
            file_scope_globs=tuple(str(x) for x in row.get("file_scope_globs", [])),
            permitted_adapters=tuple(str(x) for x in row.get("permitted_adapters", [])),
            egress_classes=tuple(str(x) for x in row.get("egress_classes", [])),
            cost_envelope_ref=str(row["cost_envelope_ref"]),
            expiry_ts=int(row["expiry_ts"]),
        )


def canonicalise(capsule: IntentCapsule) -> bytes:
    """RFC 8785 JCS canonical bytes for a capsule (mirrors lineage entries).

    ``sort_keys=True`` + minimal separators + UTF-8 covers the subset relevant
    to a flat object of strings, ints, and lists-of-strings.
    """
    return json.dumps(
        capsule.to_canonical_dict(),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def capsule_hash(capsule: IntentCapsule) -> str:
    """Return the ``sha256:``-prefixed content hash of the capsule bytes."""
    return "sha256:" + hashlib.sha256(canonicalise(capsule)).hexdigest()


def _sha256_ref(payload: Any) -> str:
    raw = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def allowed_action_classes_hash(capsule: IntentCapsule) -> str:
    """Return a compact commit to the capsule's allow-list."""
    return _sha256_ref(list(capsule.allowed_action_classes))


def compile_capsule(
    *,
    plan: TaskPlan,
    task_id: str,
    allowed_action_classes: list[str],
    file_scope_globs: list[str],
    permitted_adapters: list[str],
    egress_classes: list[str],
    expiry_ts: int,
) -> IntentCapsule:
    """Compile an :class:`IntentCapsule` from an approved plan and approval data.

    The goal text is bound by digest, never stored; the cost envelope is bound
    by a digest over the plan's cost fields so a verifier can confirm the
    capsule references the exact approved envelope without re-embedding it.

    Args:
        plan: The approved :class:`TaskPlan` (source of goal + cost envelope).
        task_id: The task the capsule governs.
        allowed_action_classes: Capability surfaces the worker may use.
        file_scope_globs: Globs the worker's writes are scoped to.
        permitted_adapters: Adapter names permitted to execute.
        egress_classes: Egress capability axes permitted (empty = no egress).
        expiry_ts: Unix timestamp after which the capsule is stale.
    """
    cost_envelope = {
        "plan_id": plan.id,
        "total_estimated_cost_usd": round(plan.total_estimated_cost_usd, 6),
        "total_estimated_minutes": plan.total_estimated_minutes,
        "high_risk_tasks": sorted(plan.high_risk_tasks),
        "task_estimates": sorted(
            (
                {
                    "task_id": e.task_id,
                    "model": e.model,
                    "estimated_tokens": e.estimated_tokens,
                    "estimated_cost_usd": round(e.estimated_cost_usd, 6),
                    "risk_level": e.risk_level,
                }
                for e in plan.task_estimates
            ),
            key=lambda row: row["task_id"],
        ),
    }
    return IntentCapsule(
        v=INTENT_CAPSULE_VERSION,
        task_id=task_id,
        plan_id=plan.id,
        goal_digest=_sha256_ref(plan.goal),
        allowed_action_classes=tuple(sorted(set(allowed_action_classes))),
        file_scope_globs=tuple(sorted(set(file_scope_globs))),
        permitted_adapters=tuple(sorted(set(permitted_adapters))),
        egress_classes=tuple(sorted(set(egress_classes))),
        cost_envelope_ref=_sha256_ref(cost_envelope),
        expiry_ts=int(expiry_ts),
    )


# ---------------------------------------------------------------------------
# On-disk capsule store (with run-id sidecar for offline journal lookup)
# ---------------------------------------------------------------------------


def _safe_task_id(task_id: str) -> str:
    if not task_id:
        raise IntentCapsuleError("empty task_id")
    if "/" in task_id or "\\" in task_id or "\x00" in task_id or task_id in {".", ".."}:
        raise IntentCapsuleError(f"unsafe task_id for capsule path: {task_id!r}")
    return task_id


def capsule_path(sdd_dir: Path, task_id: str) -> Path:
    """Return the on-disk capsule record path for ``task_id``."""
    return sdd_dir / "intent" / "capsules" / f"{_safe_task_id(task_id)}.json"


def write_capsule(sdd_dir: Path, capsule: IntentCapsule, *, run_id: str = "") -> Path:
    """Persist a capsule record (capsule + hash + run association) to disk."""
    path = capsule_path(sdd_dir, capsule.task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "capsule": capsule.to_dict(),
        "capsule_hash": capsule_hash(capsule),
        "run_id": run_id,
    }
    path.write_text(
        json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True),
        encoding="utf-8",
    )
    return path


def read_capsule_binding(sdd_dir: Path, task_id: str) -> tuple[IntentCapsule | None, str]:
    """Return ``(capsule, run_id)`` for ``task_id`` (``(None, "")`` if absent)."""
    path = capsule_path(sdd_dir, task_id)
    if not path.is_file():
        return None, ""
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        capsule = IntentCapsule.from_dict(record["capsule"])
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        return None, ""
    return capsule, str(record.get("run_id", ""))


def read_capsule(sdd_dir: Path, task_id: str) -> IntentCapsule | None:
    """Return the capsule for ``task_id`` (or ``None`` if absent/malformed)."""
    capsule, _ = read_capsule_binding(sdd_dir, task_id)
    return capsule


# ---------------------------------------------------------------------------
# Binding into the run journal + spawn record (Phase 2)
# ---------------------------------------------------------------------------


def bind_capsule_into_journal(journal: EventJournal, *, task_id: str, capsule_hash: str) -> None:
    """Record the capsule binding as a Merkle-chained journal event.

    Every subsequent journal step is then attributable to one approved capsule:
    replay carries the binding because the event is part of the run's Merkle
    chain (a reordered journal breaks the chain).
    """
    journal.record(CAPSULE_BOUND_EVENT, task_id=task_id, capsule_hash=capsule_hash)


def capsule_spawn_binding(*, task_id: str, capsule_hash: str) -> dict[str, str]:
    """Return the spawn-record fragment that binds a worker to its capsule.

    Merged into the worker's spawn record so the spawn is attributable to the
    approved capsule independently of the journal binding.
    """
    return {"intent_task_id": task_id, "intent_capsule_hash": capsule_hash}


# ---------------------------------------------------------------------------
# Drift policy (thresholds as reviewed data, not code)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DriftPolicy:
    """Divergence policy for the drift monitor.

    Thresholds live as reviewed data, not code, so a policy change is a config
    edit rather than a code change. The first release defaults to ``warn`` (drift
    is surfaced and escalated but not blocked); ``block`` is opt-in.

    Attributes:
        mode: ``"warn"`` (default) or ``"block"``.
        escalate_on_egress: When True, an allowed action class that carries
            outbound communication the capsule did not permit still diverges.
        allow_unclassified: When True, events that map to no action class are
            never counted as drift.
    """

    mode: str = "warn"
    escalate_on_egress: bool = True
    allow_unclassified: bool = True

    @classmethod
    def default(cls) -> DriftPolicy:
        return cls()

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> DriftPolicy:
        mode = str(row.get("mode", "warn"))
        if mode not in {"warn", "block"}:
            mode = "warn"
        return cls(
            mode=mode,
            escalate_on_egress=bool(row.get("escalate_on_egress", True)),
            allow_unclassified=bool(row.get("allow_unclassified", True)),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "escalate_on_egress": self.escalate_on_egress,
            "allow_unclassified": self.allow_unclassified,
        }


# ---------------------------------------------------------------------------
# Conformance verdict (a pure projection of journal + capsule)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Divergence:
    """One journal step whose action class left the approved capsule."""

    step_index: int
    event_hash: str
    action_class: str
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "step_index": self.step_index,
            "event_hash": self.event_hash,
            "action_class": self.action_class,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ConformanceVerdict:
    """The deterministic verdict of comparing a journal against a capsule.

    ``verdict_hash`` is a pure function of ``(capsule, journal, policy_mode)``:
    two verifiers on different machines recompute the byte-identical value.
    """

    conformant: bool
    capsule_hash: str
    policy_mode: str
    divergences: tuple[Divergence, ...]
    verdict_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "conformant": self.conformant,
            "capsule_hash": self.capsule_hash,
            "policy_mode": self.policy_mode,
            "divergences": [d.to_dict() for d in self.divergences],
            "verdict_hash": self.verdict_hash,
        }


def _assert_llm_free_runtime() -> None:
    """Runtime guard: refuse to run the drift path if an LLM module leaked in.

    Complements the static AST guard: if any denylisted module object is ever
    imported into this module's namespace, the drift-decision path raises rather
    than silently letting a model onto the loop.
    """
    if not LLM_FREE:  # pragma: no cover - defensive
        raise AssertionError("drift-decision path is not marked LLM-free")
    for value in globals().values():
        if isinstance(value, types.ModuleType):
            name = getattr(value, "__name__", "")
            if any(name == d or name.startswith(d + ".") for d in _LLM_IMPORT_DENYLIST):
                raise AssertionError(f"LLM module {name!r} is imported into the drift-decision module")


def evaluate_conformance(
    events: list[dict[str, Any]],
    capsule: IntentCapsule,
    *,
    policy: DriftPolicy | None = None,
) -> ConformanceVerdict:
    """Compare an observed journal against a capsule; return a pure verdict.

    Pure function of ``(events, capsule, policy)``: no clock, no socket, no
    model. Every observed action class outside the capsule's allow-list (or
    carrying egress the capsule did not permit) produces one :class:`Divergence`
    at its step index. The verdict is conformant iff there are no divergences.
    """
    _assert_llm_free_runtime()
    active = policy or DriftPolicy.default()
    allowed = set(capsule.allowed_action_classes)
    egress_ok = "external_comm" in set(capsule.egress_classes)

    divergences: list[Divergence] = []
    for index, event in enumerate(events):
        action_class = classify_journal_event(event)
        if action_class is None:
            continue
        event_hash = str(event.get("event_hash", ""))
        if action_class not in allowed:
            divergences.append(
                Divergence(
                    step_index=index,
                    event_hash=event_hash,
                    action_class=action_class,
                    reason="action_class_not_permitted",
                )
            )
            continue
        if active.escalate_on_egress and action_class in _EXTERNAL_COMM_ACTION_CLASSES and not egress_ok:
            divergences.append(
                Divergence(
                    step_index=index,
                    event_hash=event_hash,
                    action_class=action_class,
                    reason="egress_not_permitted",
                )
            )

    ch = capsule_hash(capsule)
    verdict_hash = _sha256_ref(
        {
            "capsule_hash": ch,
            "policy_mode": active.mode,
            "divergences": [d.to_dict() for d in divergences],
        }
    )
    return ConformanceVerdict(
        conformant=not divergences,
        capsule_hash=ch,
        policy_mode=active.mode,
        divergences=tuple(divergences),
        verdict_hash=verdict_hash,
    )


def project_conformance_verdict(verdict: ConformanceVerdict) -> dict[str, Any]:
    """Return a compact, operator-facing / evidence-bundle projection."""
    return {
        "conformant": verdict.conformant,
        "capsule_hash": verdict.capsule_hash,
        "policy_mode": verdict.policy_mode,
        "divergence_count": len(verdict.divergences),
        "divergent_action_classes": sorted({d.action_class for d in verdict.divergences}),
        "verdict_hash": verdict.verdict_hash,
    }


# ---------------------------------------------------------------------------
# Static LLM-free import guard (AC6)
# ---------------------------------------------------------------------------


def iter_module_import_names(source_path: str | Path) -> set[str]:
    """Return the set of fully-qualified module names imported by a source file."""
    from pathlib import Path as _Path

    tree = ast.parse(_Path(source_path).read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def assert_no_llm_imports(source_path: str | Path) -> None:
    """Raise ``AssertionError`` if a source file imports any LLM/model module.

    The static half of the no-LLM-on-the-drift-path guarantee (AC6). The runtime
    half is a profiler assertion in the test suite plus
    :func:`_assert_llm_free_runtime`.
    """
    names = iter_module_import_names(source_path)
    offenders = sorted(n for n in names if any(n == d or n.startswith(d + ".") for d in _LLM_IMPORT_DENYLIST))
    assert not offenders, f"llm imports found on the drift-decision path: {offenders}"


# ---------------------------------------------------------------------------
# Approval-time write to the audit chain (Phase 1)
# ---------------------------------------------------------------------------


def approve_and_capsule(
    *,
    chain: AuditChainStore,
    sdd_dir: Path,
    plan: TaskPlan,
    task_id: str,
    run_id: str,
    allowed_action_classes: list[str],
    file_scope_globs: list[str],
    permitted_adapters: list[str],
    egress_classes: list[str],
    expiry_ts: int,
) -> tuple[IntentCapsule, AuditEvent]:
    """Compile a capsule at approval time, persist it, and chain it (AC1).

    Returns the compiled capsule and the ``intent.capsule`` audit event whose
    details bind the capsule hash into the HMAC chain.
    """
    from bernstein.core.security.audit_chain import record_intent_capsule

    capsule = compile_capsule(
        plan=plan,
        task_id=task_id,
        allowed_action_classes=allowed_action_classes,
        file_scope_globs=file_scope_globs,
        permitted_adapters=permitted_adapters,
        egress_classes=egress_classes,
        expiry_ts=expiry_ts,
    )
    write_capsule(sdd_dir, capsule, run_id=run_id)
    event = record_intent_capsule(
        chain=chain,
        task_id=task_id,
        plan_id=plan.id,
        run_id=run_id,
        capsule_hash=capsule_hash(capsule),
        goal_digest=capsule.goal_digest,
        allowed_action_classes_hash=allowed_action_classes_hash(capsule),
        expiry_ts=capsule.expiry_ts,
    )
    return capsule, event


def record_intent_drift(
    *,
    chain: AuditChainStore,
    task_id: str,
    capsule_hash: str,
    verdict_hash: str,
    divergent_count: int,
    escalation_journal_entry_hash: str,
) -> AuditEvent:
    """Mirror a drift escalation's identity into the HMAC audit chain."""
    from bernstein.core.security.audit_chain import (
        record_intent_drift as _record_intent_drift,
    )

    return _record_intent_drift(
        chain=chain,
        task_id=task_id,
        capsule_hash=capsule_hash,
        verdict_hash=verdict_hash,
        divergent_count=divergent_count,
        escalation_journal_entry_hash=escalation_journal_entry_hash,
    )


# ---------------------------------------------------------------------------
# Drift escalation (reuses the stall-escalation shape; AC4)
# ---------------------------------------------------------------------------


def assemble_intent_drift_escalation(
    *,
    sdd_dir: Path,
    lineage_root: Path,
    hmac_key: bytes,
    private_key_pem: str,
    public_key_pem: str,
    run_id: str,
    capsule: IntentCapsule,
    verdict: ConformanceVerdict,
    worker_id: str = "",
    session_id: str = "",
    worktree_id: str = "",
    install_rev: str = "",
    timestamp: int,
    window: int | None = None,
) -> EscalationReceipt:
    """Emit a signed escalation receipt for a drift verdict (AC4).

    Reuses the journal-anchored stall-escalation shape: the receipt binds the
    trailing journal window by Merkle hash, is signed with the install identity,
    and is anchored in the escalation lineage spine, so it passes
    ``bernstein escalation verify``. The ``extra_binding`` carries the capsule
    hash, the verdict hash, and the divergent events, so the receipt names
    exactly which capsule was violated and where.
    """
    from bernstein.core.orchestration.escalation import (
        DEFAULT_ESCALATION_WINDOW,
        assemble_escalation_receipt,
    )
    from bernstein.core.orchestration.supervisor_receipt import StallReason

    extra_binding = {
        "kind": "intent_drift",
        "capsule_hash": capsule_hash(capsule),
        "verdict_hash": verdict.verdict_hash,
        "divergent_events": [d.to_dict() for d in verdict.divergences],
    }
    return assemble_escalation_receipt(
        sdd_dir=sdd_dir,
        lineage_root=lineage_root,
        hmac_key=hmac_key,
        private_key_pem=private_key_pem,
        public_key_pem=public_key_pem,
        run_id=run_id,
        worker_id=worker_id,
        session_id=session_id,
        worktree_id=worktree_id,
        stall_reason=StallReason.INTENT_DRIFT,
        respawn_budget_remaining=0,
        fork_step=None,
        window=window if window is not None else DEFAULT_ESCALATION_WINDOW,
        install_rev=install_rev,
        timestamp=timestamp,
        extra_binding=extra_binding,
    )


# ---------------------------------------------------------------------------
# Offline conformance verification (Phase 4 / AC3)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IntentVerifyResult:
    """Outcome of :func:`verify_intent_conformance`."""

    ok: bool
    conformant: bool
    reason: str
    verdict: ConformanceVerdict | None = None
    capsule: IntentCapsule | None = None
    run_id: str = ""


def verify_intent_conformance(
    *,
    sdd_dir: Path,
    chain: AuditChainStore,
    task_id: str,
    policy: DriftPolicy | None = None,
) -> IntentVerifyResult:
    """Recompute conformance offline from journal + capsule (AC2, AC3).

    Verifies, in order:

    * the capsule loads and its recomputed hash matches the ``intent.capsule``
      entry recorded in the HMAC audit chain (a tampered capsule diverges here);
    * the audit chain itself verifies;
    * the run journal's Merkle chain verifies (a reordered journal fails here);
    * the conformance verdict is recomputed as a pure function of the journal
      and the capsule.

    ``ok`` is True only when the chain and journal verify and the run is
    conformant. A drifted-but-untampered run returns ``ok=False`` with
    ``conformant=False`` and a divergence-naming reason.
    """
    from bernstein.core.replay.journal import load_events, verify_journal
    from bernstein.core.security.audit_chain import EVENT_INTENT_CAPSULE

    capsule, run_id = read_capsule_binding(sdd_dir, task_id)
    if capsule is None:
        return IntentVerifyResult(ok=False, conformant=False, reason="no intent capsule for task")

    ok_chain, chain_errors = chain.verify()
    if not ok_chain:
        detail = chain_errors[0] if chain_errors else "chain break"
        return IntentVerifyResult(
            ok=False,
            conformant=False,
            reason=f"audit chain fails verification ({detail})",
            capsule=capsule,
            run_id=run_id,
        )

    recorded = [e for e in chain.query(event_type=EVENT_INTENT_CAPSULE) if e.details.get("task_id") == task_id]
    if not recorded:
        return IntentVerifyResult(
            ok=False,
            conformant=False,
            reason="capsule is not recorded in the audit chain",
            capsule=capsule,
            run_id=run_id,
        )
    recomputed = capsule_hash(capsule)
    if str(recorded[-1].details.get("capsule_hash", "")) != recomputed:
        return IntentVerifyResult(
            ok=False,
            conformant=False,
            reason="capsule bytes do not match the audit-chain-recorded capsule hash (tampered)",
            capsule=capsule,
            run_id=run_id,
        )

    journal_path = sdd_dir / "runs" / run_id / "journal.jsonl"
    if not journal_path.exists():
        return IntentVerifyResult(
            ok=False,
            conformant=False,
            reason=f"run journal for {run_id!r} is missing; cannot recompute conformance",
            capsule=capsule,
            run_id=run_id,
        )
    jres = verify_journal(journal_path)
    if not jres.ok:
        detail = jres.errors[0] if jres.errors else "chain break"
        return IntentVerifyResult(
            ok=False,
            conformant=False,
            reason=f"run journal chain diverges ({detail}); steps were reordered or tampered",
            capsule=capsule,
            run_id=run_id,
        )

    verdict = evaluate_conformance(load_events(journal_path), capsule, policy=policy)
    if verdict.conformant:
        return IntentVerifyResult(
            ok=True,
            conformant=True,
            reason="",
            verdict=verdict,
            capsule=capsule,
            run_id=run_id,
        )
    classes = ", ".join(sorted({d.action_class for d in verdict.divergences}))
    return IntentVerifyResult(
        ok=False,
        conformant=False,
        reason=f"drift: {len(verdict.divergences)} action(s) outside the capsule ({classes})",
        verdict=verdict,
        capsule=capsule,
        run_id=run_id,
    )


__all__ = [
    "CAPSULE_BOUND_EVENT",
    "INTENT_CAPSULE_VERSION",
    "LLM_FREE",
    "ConformanceVerdict",
    "Divergence",
    "DriftPolicy",
    "IntentCapsule",
    "IntentCapsuleError",
    "IntentVerifyResult",
    "allowed_action_classes_hash",
    "approve_and_capsule",
    "assemble_intent_drift_escalation",
    "assert_no_llm_imports",
    "bind_capsule_into_journal",
    "canonicalise",
    "capsule_hash",
    "capsule_path",
    "capsule_spawn_binding",
    "classify_journal_event",
    "compile_capsule",
    "evaluate_conformance",
    "iter_module_import_names",
    "project_conformance_verdict",
    "read_capsule",
    "read_capsule_binding",
    "record_intent_drift",
    "verify_intent_conformance",
    "write_capsule",
]
