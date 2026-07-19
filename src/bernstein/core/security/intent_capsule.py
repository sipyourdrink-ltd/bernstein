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
import operator
import posixpath
import re
import types
from dataclasses import dataclass
from functools import lru_cache
from typing import TYPE_CHECKING, Any, cast

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

#: A run journal whose end is committed to the audit chain: truncation and
#: rewriting are both detectable, so a conformant verdict covers the whole run.
SEAL_SEALED = "sealed"

#: A run journal with no seal yet -- the ordinary state of a run still in
#: progress. Conformance is still computed and drift is still reported, because
#: deleting rows can only *hide* drift, never manufacture it. What cannot be
#: attested is completeness: a clean verdict here means "no drift in the rows
#: that exist", not "no drift happened".
SEAL_UNSEALED = "unsealed"

#: A seal exists but disagrees with the journal (or two seals disagree with each
#: other). Unlike the other two this is never benign.
SEAL_MISMATCH = "mismatch"

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
    "sh": "shell.exec",
    "zsh": "shell.exec",
    "fish": "shell.exec",
    "pwsh": "shell.exec",
    "powershell": "shell.exec",
    "cmd": "shell.exec",
    "shell": "shell.exec",
    "shell_command": "shell.exec",
    "run_command": "shell.exec",
    "run_shell_command": "shell.exec",
    "subprocess": "shell.exec",
    "terminal": "shell.exec",
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

#: Action classes whose events are checked against the capsule's
#: ``file_scope_globs``. The capsule scopes the worker's *mutations*: a read
#: outside the declared write scope is not itself a scope violation, so
#: enforcing reads here would flag ordinary config/context lookups as drift.
_FILE_SCOPED_ACTION_CLASSES: frozenset[str] = frozenset({"fs.write", "fs.delete"})

#: Journal payload keys carrying a single path, in deterministic probe order.
_PATH_FIELDS: tuple[str, ...] = ("path", "file_path", "file")

#: Journal payload keys carrying a list of paths, in deterministic probe order.
_PATH_LIST_FIELDS: tuple[str, ...] = ("paths", "files")

#: Journal payload keys naming the adapter that executed an event.
_ADAPTER_FIELDS: tuple[str, ...] = ("adapter", "adapter_name")

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


def normalise_tool_name(tool: str) -> str:
    """Return the lookup key for a worker-reported tool name.

    Whitespace is stripped and case is folded before the reviewed map is
    consulted. Without this the anti-relabelling guard is defeated by one
    character: ``"Bash "`` misses both ``"Bash"`` and ``"bash"``, falls through
    to the worker-stamped label, and a shell call classifies as whatever the
    worker claimed.
    """
    return " ".join(tool.split()).casefold()


def claimed_action_classes(event: dict[str, Any]) -> tuple[str, ...]:
    """Return every action class an event claims, most-authoritative first.

    Both the ``tool`` name and the stamped ``action_class`` are written by the
    same worker, so preferring either one alone just moves which field is
    authoritative rather than removing worker control. When they disagree the
    honest reading is that we do not know which is true, so conformance is
    evaluated against **both**: whichever is disallowed produces the divergence.

    That closes the relabelling hole in both directions. A ``Bash`` call stamped
    ``git.commit`` is still shell execution, and a ``Read`` call that honestly
    stamps ``shell.exec`` is no longer silenced by the reviewed map.

    Order is deterministic (mapped class first, stamped second, event type last)
    so ``verdict_hash`` is stable.
    """
    classes: list[str] = []
    tool = event.get("tool")
    if isinstance(tool, str) and tool.strip():
        mapped = _TOOL_TO_ACTION_CLASS.get(normalise_tool_name(tool))
        if mapped:
            classes.append(mapped)
    stamped = event.get("action_class")
    if isinstance(stamped, str) and stamped and stamped not in classes:
        classes.append(stamped)
    if not classes:
        event_type = event.get("event")
        if isinstance(event_type, str) and event_type:
            mapped_event = _EVENT_TO_ACTION_CLASS.get(event_type)
            if mapped_event:
                classes.append(mapped_event)
    return tuple(classes)


def _is_action_attempt(event: dict[str, Any]) -> bool:
    """Return True when an event claims to be a worker action at all.

    An event that carries neither a ``tool`` nor an ``action_class`` field, and
    whose type is not in the reviewed action-bearing vocabulary, is structural
    bookkeeping (ticks, snapshots, retry decisions, capsule bindings, worktree
    reaps). Such events are not "unclassified actions" and must not be counted
    as drift under ``allow_unclassified=False`` -- doing so flags every real run
    and makes the policy knob unusable.
    """
    for key in ("tool", "action_class"):
        value = event.get(key)
        if isinstance(value, str) and value.strip():
            return True
    event_type = event.get("event")
    return isinstance(event_type, str) and event_type in _EVENT_TO_ACTION_CLASS


def classify_journal_event(event: dict[str, Any]) -> str | None:
    """Return the action class an observed journal event maps to, or ``None``.

    Resolution order (deterministic, no inference):

    1. the ``tool`` field, normalised by :func:`normalise_tool_name` and mapped
       through the reviewed :data:`_TOOL_TO_ACTION_CLASS`,
    2. an explicit truthy ``action_class`` field, **only** for a tool the
       reviewed map does not recognise,
    3. the ``event`` type mapped through :data:`_EVENT_TO_ACTION_CLASS`.

    The reviewed map wins over the worker-stamped label by design. The
    ``action_class`` field arrives from the same worker whose conformance is
    being judged, so honouring it first would let a drifting worker relabel its
    own actions: a ``Bash`` call stamped ``git.commit`` would launder shell
    execution past a capsule that never permitted it.

    Scope of the guarantee: relabelling is prevented for every tool name the
    reviewed map resolves, including case and whitespace variants. A tool name
    the map does not know at all still falls back to the worker-stamped label,
    so a worker that invents an unmapped name can still self-declare a benign
    class. That residual is bounded -- the stamped class must still be one the
    capsule allows, and ``allow_unclassified=False`` surfaces unmapped tools --
    but closing it fully needs a registry of admissible tool names, which does
    not exist yet. Extend :data:`_TOOL_TO_ACTION_CLASS` as tools are adopted.

    Ticks, snapshots, and capsule-binding events carry no action class and
    return ``None`` so they never count as drift.
    """
    tool = event.get("tool")
    if isinstance(tool, str) and tool.strip():
        mapped = _TOOL_TO_ACTION_CLASS.get(normalise_tool_name(tool))
        if mapped:
            return mapped
    explicit = event.get("action_class")
    if isinstance(explicit, str) and explicit:
        return explicit
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

    def __post_init__(self) -> None:
        """Normalise field types so construction and reload agree byte for byte.

        :meth:`from_dict` coerces every field on the way in (``int``, ``str``,
        ``tuple``). Without the identical coercion here, an in-memory capsule
        built from a caller's loosely-typed value canonicalises differently from
        the same capsule reloaded from disk: a float ``expiry_ts`` of
        ``1700000000.0`` hashes as ``1700000000.0`` in memory and as
        ``1700000000`` after a round trip. Because verification compares a
        recomputed hash against the persisted one, that asymmetry rejects an
        honest capsule as "tampered or never approved". Type hints are not
        enforced at runtime, and an upstream ``time.time() + ttl`` is a float,
        so the normalisation has to be real rather than assumed.
        """
        object.__setattr__(self, "v", int(self.v))
        object.__setattr__(self, "task_id", str(self.task_id))
        object.__setattr__(self, "plan_id", str(self.plan_id))
        object.__setattr__(self, "goal_digest", str(self.goal_digest))
        object.__setattr__(self, "cost_envelope_ref", str(self.cost_envelope_ref))
        object.__setattr__(self, "expiry_ts", int(self.expiry_ts))
        for field_name in ("allowed_action_classes", "file_scope_globs", "permitted_adapters", "egress_classes"):
            object.__setattr__(self, field_name, tuple(str(x) for x in getattr(self, field_name)))

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
            key=operator.itemgetter("task_id"),
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
        expiry_ts=expiry_ts,
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


#: A run_id names exactly one journal directory and must be a single safe path
#: segment. Mirrors the allowlist in ``core/replay/journal.py``: an anchored
#: match then a return of the checked value, so no attacker-controlled character
#: reaches the filesystem sink below.
_RUN_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _run_journal_path(sdd_dir: Path, run_id: str) -> Path:
    """Return the journal path for ``run_id``, rejecting unsafe segments."""
    if run_id in {".", ".."} or not _RUN_ID_RE.match(run_id):
        raise IntentCapsuleError(f"unsafe run_id for journal path: {run_id!r}")
    return sdd_dir / "runs" / run_id / "journal.jsonl"


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


@lru_cache(maxsize=512)
def _compiled_glob(pattern: str) -> re.Pattern[str]:
    """Compile one path glob to an anchored regex (cached, pure).

    ``fnmatch`` is deliberately not used: its ``*`` also matches ``/``, so
    ``src/*.py`` would accept ``src/nested/deep.py`` and silently widen the
    approved scope. Here ``*`` and ``?`` stop at a separator, ``**`` spans them,
    and everything else is escaped literally.
    """
    out: list[str] = []
    i = 0
    n = len(pattern)
    while i < n:
        char = pattern[i]
        if char == "*":
            if i + 1 < n and pattern[i + 1] == "*":
                # ``**/`` spans zero or more directories; a trailing ``**``
                # spans the rest of the path.
                if i + 2 < n and pattern[i + 2] == "/":
                    out.append("(?:.*/)?")
                    i += 3
                    continue
                out.append(".*")
                i += 2
                continue
            out.append("[^/]*")
        elif char == "?":
            out.append("[^/]")
        else:
            out.append(re.escape(char))
        i += 1
    return re.compile("".join(out) + r"\Z")


def _normalise_path(raw: str) -> str:
    """Return a comparable posix-style relative path for glob matching.

    ``..`` and ``.`` segments are collapsed lexically first. Without that, an
    in-scope prefix is a free pass: ``src/pricing/../../etc/passwd`` would match
    ``src/pricing/**`` while landing well outside the approved scope. The
    collapse is purely textual (no filesystem access, no symlink resolution), so
    the verdict stays a pure function of the journal bytes.
    """
    return posixpath.normpath(raw.replace("\\", "/").strip())


def path_in_scope(path: str, globs: tuple[str, ...]) -> bool:
    """Return True when ``path`` matches at least one glob in ``globs``.

    An empty ``globs`` declares no file scope and constrains nothing.

    Two shapes are never in scope, whatever the globs say, because both mean the
    path is not the workspace-relative path the scope was approved for:

    * a path that still escapes upward after normalisation (``../secrets``);
    * an absolute path. Stripping the leading ``/`` would reinterpret the input
      into the shape that passes -- ``/tmp/evil`` would satisfy ``tmp/**`` -- so
      a containment check must reject it rather than rewrite it.
    """
    if not globs:
        return True
    candidate = _normalise_path(path)
    if candidate.startswith("/"):
        return False
    return not (candidate == ".." or candidate.startswith("../")) and any(
        _compiled_glob(g).match(candidate) is not None for g in globs
    )


def _event_paths(event: dict[str, Any]) -> list[str]:
    """Return every path an event records, in deterministic probe order."""
    found: list[str] = []
    for key in _PATH_FIELDS:
        value = event.get(key)
        if isinstance(value, str) and value:
            found.append(value)
    for key in _PATH_LIST_FIELDS:
        value = event.get(key)
        if not isinstance(value, list):
            continue
        items = cast("list[Any]", value)
        found.extend(item for item in items if isinstance(item, str) and item)
    return found


def _event_adapter(event: dict[str, Any]) -> str:
    for key in _ADAPTER_FIELDS:
        value = event.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def _divergence_reason(
    event: dict[str, Any],
    action_class: str,
    capsule: IntentCapsule,
    *,
    policy: DriftPolicy,
    allowed: set[str],
    permitted_adapters: set[str],
    egress_ok: bool,
) -> str | None:
    """Return the first constraint ``event`` violates, or ``None`` if conformant.

    Checks run in a fixed order so one event yields at most one divergence and
    ``verdict_hash`` stays stable across verifiers.

    Capsule expiry is deliberately **not** checked here. A journal row's ``ts``
    is excluded from the payload hash (``_NON_DETERMINISTIC_FIELDS`` in
    ``core/replay/journal.py``) so that a faithful replay, which differs only in
    timing, hashes identically. Feeding ``ts`` into ``verdict_hash`` would sign
    an unauthenticated value that anyone can edit in place without breaking the
    Merkle chain, and would make the same run produce different verdicts on
    replay. Expiry is enforced against authenticated audit-chain timestamps in
    :func:`chain_expiry_violation` instead.
    """
    if action_class not in allowed:
        return "action_class_not_permitted"
    if policy.escalate_on_egress and action_class in _EXTERNAL_COMM_ACTION_CLASSES and not egress_ok:
        return "egress_not_permitted"
    if permitted_adapters:
        adapter = _event_adapter(event)
        if not adapter:
            # Fail closed, exactly as the path check below does. An allowlist
            # that applies only when the caller volunteers the field being
            # checked is not an allowlist: the journal is written by the same
            # worker whose conformance is being judged, so omitting one key
            # would retire the control.
            return "adapter_unrecorded"
        if adapter not in permitted_adapters:
            return "adapter_not_permitted"
    if action_class in _FILE_SCOPED_ACTION_CLASSES and capsule.file_scope_globs:
        paths = _event_paths(event)
        if not paths:
            # Fail closed: a declared scope that cannot be checked is not a
            # control. A mutating action must name what it mutated.
            return "path_unrecorded"
        if any(not path_in_scope(p, capsule.file_scope_globs) for p in paths):
            return "file_scope_violation"
    return None


def evaluate_conformance(
    events: list[dict[str, Any]],
    capsule: IntentCapsule,
    *,
    policy: DriftPolicy | None = None,
) -> ConformanceVerdict:
    """Compare an observed journal against a capsule; return a pure verdict.

    Pure function of ``(events, capsule, policy)``: no clock, no socket, no
    model. Every constraint the capsule declares is enforced here, because a
    declared-but-unenforced scope is not a control:

    * the observed action class must be in ``allowed_action_classes``;
    * an action class carrying outbound communication requires
      ``external_comm`` in ``egress_classes`` (when ``escalate_on_egress``);
    * a recorded adapter must be in ``permitted_adapters``;
    * a mutating file action must name paths, and they must match
      ``file_scope_globs`` when the capsule declares one;
    * an event that claims to be an action but maps to no action class is a
      divergence unless ``allow_unclassified`` is set. Structural bookkeeping
      events are never counted, whatever the policy.

    Capsule expiry is enforced separately by :func:`chain_expiry_violation`,
    against authenticated audit-chain timestamps rather than the journal's
    unauthenticated ``ts`` field.

    Each violated constraint produces one :class:`Divergence` at the event's step
    index; the checks are ordered so the strongest signal is reported first and
    one event yields at most one divergence, keeping ``verdict_hash`` stable.
    The verdict is conformant iff there are no divergences.
    """
    _assert_llm_free_runtime()
    active = policy or DriftPolicy.default()
    allowed = set(capsule.allowed_action_classes)
    permitted_adapters = set(capsule.permitted_adapters)
    egress_ok = "external_comm" in set(capsule.egress_classes)

    divergences: list[Divergence] = []
    for index, event in enumerate(events):
        classes = claimed_action_classes(event)

        if not classes:
            if active.allow_unclassified or not _is_action_attempt(event):
                continue
            reason = "unclassified_event"
            action_class = ""
        else:
            # Every class the event claims must clear the capsule: a worker
            # cannot exonerate one claim by making a second, quieter one.
            offender = next(
                (
                    (candidate, why)
                    for candidate in classes
                    for why in (
                        _divergence_reason(
                            event,
                            candidate,
                            capsule,
                            policy=active,
                            allowed=allowed,
                            permitted_adapters=permitted_adapters,
                            egress_ok=egress_ok,
                        ),
                    )
                    if why is not None
                ),
                None,
            )
            if offender is None:
                continue
            action_class, reason = offender

        divergences.append(
            Divergence(
                step_index=index,
                event_hash=str(event.get("event_hash", "")),
                action_class=action_class,
                reason=reason,
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


def _capsule_lifecycle_entries(chain: AuditChainStore, task_id: str) -> list[AuditEvent]:
    """Return the chain entries that evidence *this capsule* being acted on.

    Deliberately narrow. Filtering on ``task_id`` alone matches 27 unrelated
    ``record_*`` event types -- evidence bundles, task suspend/resume, review
    board actions -- none of which mean the capsule was in use. Because the
    chain is append-only, letting any of those decide expiry means one ordinary
    later entry condemns an honest run forever, with no repair path.
    """
    from bernstein.core.security.audit_chain import (
        EVENT_INTENT_CAPSULE,
        EVENT_INTENT_DRIFT,
        EVENT_INTENT_JOURNAL_SEAL,
    )

    wanted = (EVENT_INTENT_CAPSULE, EVENT_INTENT_JOURNAL_SEAL, EVENT_INTENT_DRIFT)
    entries = [e for t in wanted for e in chain.query(event_type=t) if e.details.get("task_id") == task_id]
    return sorted(entries, key=lambda e: str(e.timestamp))


def chain_expiry_violation(entries: list[AuditEvent], capsule: IntentCapsule) -> str:
    """Return the first authenticated chain timestamp past ``expiry_ts``, or "".

    Expiry is enforced here rather than inside :func:`evaluate_conformance`
    because an :class:`AuditEvent` timestamp is covered by the entry's HMAC:
    editing it breaks the chain, so it is signed state. The journal's per-row
    ``ts`` is not -- it is excluded from the payload hash so faithful replays
    hash identically -- so enforcing expiry there would both sign an
    unauthenticated value and make replay non-deterministic.

    The trade-off is coverage: this detects a capsule still being acted on past
    its expiry only as far as the chain records events for it, not per journal
    step. Making per-step expiry authenticated would require a chained step
    clock inside the hashed payload.

    Pass the capsule *lifecycle* entries from :func:`_capsule_lifecycle_entries`.
    Approval entries alone cannot enforce anything -- an approval is by
    construction at or before the expiry it declares -- so the signal is the
    journal seal written when the run finishes, which evidences the capsule
    still being acted on. Passing every task-scoped entry instead is the failure
    in the other direction: unrelated later activity would condemn an honest run
    permanently on an append-only chain.

    Args:
        entries: Capsule-lifecycle chain entries for the task.
        capsule: The capsule whose ``expiry_ts`` governs.

    Returns:
        The offending ISO 8601 timestamp, or an empty string when none is past
        expiry (including when the capsule declares no expiry).
    """
    if not capsule.expiry_ts:
        return ""
    from datetime import UTC, datetime

    for entry in entries:
        raw = str(entry.timestamp or "")
        if not raw:
            continue
        try:
            parsed = datetime.fromisoformat(raw)
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        # Float comparison: truncating to whole seconds would silently forgive
        # any overrun shorter than a second.
        if parsed.timestamp() > capsule.expiry_ts:
            return raw
    return ""


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
            names.update(alias.name for alias in node.names)
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


def find_journal_seals(
    *,
    chain: AuditChainStore,
    task_id: str,
    run_id: str,
    capsule_hash_value: str,
) -> list[AuditEvent]:
    """Return every ``intent.journal_seal`` entry for this run and capsule."""
    from bernstein.core.security.audit_chain import EVENT_INTENT_JOURNAL_SEAL

    return [
        e
        for e in chain.query(event_type=EVENT_INTENT_JOURNAL_SEAL)
        if e.details.get("task_id") == task_id
        and str(e.details.get("run_id", "")) == run_id
        and str(e.details.get("capsule_hash", "")) == capsule_hash_value
    ]


def seal_run_journal(
    *,
    chain: AuditChainStore,
    sdd_dir: Path,
    task_id: str,
    run_id: str,
    capsule: IntentCapsule,
) -> AuditEvent:
    """Commit a finished run journal's head and length to the audit chain (#2649).

    Called when a capsule-governed run ends. The journal's Merkle chain
    recomputes from genesis, so any prefix of it verifies as a valid journal on
    its own and a worker that drifted could delete its trailing rows. Sealing
    records the head hash and the event count, giving the verifier an
    independent commitment to compare the surviving journal against.

    **Idempotent.** Sealing the same journal twice returns the existing entry
    and writes nothing. A retry is not an attack, and the process that writes an
    attestation can die between the write and the acknowledgement, so the
    at-least-once shape has to be safe -- especially on an append-only chain,
    where a duplicate could never be withdrawn.

    Raises:
        IntentCapsuleError: If the journal is missing or its chain diverges, or
            if a seal already exists that disagrees with the journal now on
            disk (the journal changed after it was sealed).
    """
    from bernstein.core.replay.journal import load_events, verify_journal
    from bernstein.core.security.audit_chain import record_intent_journal_seal

    journal_path = _run_journal_path(sdd_dir, run_id)
    if not journal_path.exists():
        raise IntentCapsuleError(f"run journal for {run_id!r} is missing; nothing to seal")
    jres = verify_journal(journal_path)
    if not jres.ok:
        detail = jres.errors[0] if jres.errors else "chain break"
        raise IntentCapsuleError(f"refusing to seal a journal whose chain diverges ({detail})")

    events = load_events(journal_path)
    head = journal_head(events)
    count = len(events)
    ch = capsule_hash(capsule)

    existing = find_journal_seals(chain=chain, task_id=task_id, run_id=run_id, capsule_hash_value=ch)
    for entry in existing:
        if str(entry.details.get("journal_head", "")) == head and entry.details.get("event_count") == count:
            return entry
    if existing:
        raise IntentCapsuleError(
            f"run {run_id!r} is already sealed at a different journal state "
            f"(sealed head {existing[-1].details.get('journal_head', '')!r} / "
            f"{existing[-1].details.get('event_count')!r} events, on-disk head {head!r} / {count} events)"
        )

    return record_intent_journal_seal(
        chain=chain,
        task_id=task_id,
        run_id=run_id,
        capsule_hash=ch,
        journal_head=head,
        event_count=count,
    )


def seal_capsules_bound_to_run(*, chain: AuditChainStore, sdd_dir: Path, run_id: str) -> list[AuditEvent]:
    """Seal every capsule the run's journal is bound to (the production entry point).

    The bindings are read from the journal's own ``intent.capsule_bound``
    anchors, so a run seals exactly the capsules it actually declared and needs
    no separate bookkeeping. Idempotent, since :func:`seal_run_journal` is.
    """
    from bernstein.core.replay.journal import load_events

    journal_path = _run_journal_path(sdd_dir, run_id)
    if not journal_path.exists():
        return []

    seen: list[str] = []
    for event in load_events(journal_path):
        if str(event.get("event", "")) != CAPSULE_BOUND_EVENT:
            continue
        task_id = str(event.get("task_id", ""))
        if task_id and task_id not in seen:
            seen.append(task_id)

    sealed: list[AuditEvent] = []
    for task_id in seen:
        capsule = read_capsule(sdd_dir, task_id)
        if capsule is None:
            continue
        sealed.append(seal_run_journal(chain=chain, sdd_dir=sdd_dir, task_id=task_id, run_id=run_id, capsule=capsule))
    return sealed


def journal_head(events: list[dict[str, Any]]) -> str:
    """Return the Merkle head of a loaded journal (its last ``event_hash``)."""
    if not events:
        return ""
    return str(events[-1].get("event_hash", ""))


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
    chain: AuditChainStore,
    run_id: str,
    capsule: IntentCapsule,
    verdict: ConformanceVerdict,
    policy: DriftPolicy | None = None,
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

    Both the ``capsule`` and the ``verdict`` are treated as claims, never as
    fact. Recomputing the verdict alone is not enough: a verdict is only ever
    meaningful relative to a capsule, so a caller who supplies a fabricated
    capsule that permits nothing can hand in a self-consistent verdict and have
    a fully conformant run attested as drift. The capsule is therefore resolved
    through :func:`_resolve_chained_binding` -- the same authority the offline
    verifier uses -- which requires it to match the ``intent.capsule`` entry in
    the HMAC chain, pins the run to the signed ``run_id``, and requires the
    journal to carry the matching capsule-bound anchor. Only then is the verdict
    recomputed from ``(journal, capsule, policy)`` and that recomputed verdict
    signed.

    A signature is what turns a receipt into evidence, so every input it commits
    to has to come from chained state.

    Works on an in-flight run: a drift receipt must be signable the moment the
    drift happens, not only after the run seals, or ``DriftPolicy(mode="block")``
    could never fire in the window it exists for. The receipt records the seal
    state it was cut under.

    Raises:
        IntentCapsuleError: If the capsule is not the chain-approved one for its
            task, the run_id is not the signed one, the journal is missing or
            its chain diverges, the capsule-bound anchor is absent or ambiguous,
            a seal exists that disagrees with the journal, the run is actually
            conformant, or the supplied verdict does not match the recomputed
            one.
    """
    from bernstein.core.orchestration.escalation import (
        DEFAULT_ESCALATION_WINDOW,
        assemble_escalation_receipt,
    )
    from bernstein.core.orchestration.supervisor_receipt import StallReason

    binding, reason, _ = _resolve_chained_binding(
        sdd_dir=sdd_dir,
        chain=chain,
        task_id=capsule.task_id,
        capsule=capsule,
        expected_run_id=run_id,
    )
    if binding is None:
        raise IntentCapsuleError(f"refusing to sign a drift receipt: {reason}")

    recomputed = evaluate_conformance(binding.events, capsule, policy=policy)
    if recomputed.conformant:
        raise IntentCapsuleError("recomputed verdict is conformant; there is no drift to escalate")
    if recomputed.verdict_hash != verdict.verdict_hash:
        raise IntentCapsuleError(
            "supplied verdict does not match the verdict recomputed from the journal "
            f"({verdict.verdict_hash} != {recomputed.verdict_hash})"
        )

    extra_binding = {
        "kind": "intent_drift",
        "capsule_hash": capsule_hash(capsule),
        "verdict_hash": recomputed.verdict_hash,
        "divergent_events": [d.to_dict() for d in recomputed.divergences],
        # Named so a reader knows the attestation level of the journal the
        # receipt was cut from. Live drift on an unsealed run is still real
        # drift; what an unsealed receipt cannot claim is that the run held
        # nothing else.
        "seal_state": binding.seal_state,
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
    #: One of :data:`SEAL_SEALED`, :data:`SEAL_UNSEALED`, :data:`SEAL_MISMATCH`.
    #: ``ok`` is True only when the run is sealed *and* conformant: an unsealed
    #: run can be reported conformant-so-far, never attested.
    seal_state: str = SEAL_UNSEALED


@dataclass(frozen=True)
class _ChainedBinding:
    """A capsule binding resolved entirely from signed / chained state."""

    capsule: IntentCapsule
    run_id: str
    events: list[dict[str, Any]]
    seal_state: str


def _resolve_chained_binding(
    *,
    sdd_dir: Path,
    chain: AuditChainStore,
    task_id: str,
    capsule: IntentCapsule,
    sidecar_run_id: str = "",
    expected_run_id: str = "",
) -> tuple[_ChainedBinding | None, str, str]:
    """Resolve and authenticate a capsule binding, or return why it failed.

    The single place that decides whether a capsule may be treated as approved
    and which run it governs. Both the offline verifier and the drift escalation
    go through it, so neither can be hardened without the other: the two paths
    diverging is exactly how a caller-supplied capsule reached a signed receipt.

    Checks, in order: the chain verifies; an ``intent.capsule`` entry exists for
    the task; the supplied capsule's recomputed hash matches the recorded one;
    the run is taken from the signed entry (an unsigned sidecar or a caller
    claim that disagrees is rejected); the capsule is not past its expiry by
    authenticated chain timestamps; the journal exists and its Merkle chain
    verifies; and the journal carries exactly one matching capsule-bound anchor.

    Returns:
        ``(binding, "", run_id)`` when every check passes, else
        ``(None, reason, run_id)`` where ``run_id`` is the authoritative value
        when it could be resolved and the caller's claim otherwise.
    """
    from bernstein.core.replay.journal import load_events, verify_journal
    from bernstein.core.security.audit_chain import EVENT_INTENT_CAPSULE

    claimed = expected_run_id or sidecar_run_id

    ok_chain, chain_errors = chain.verify()
    if not ok_chain:
        detail = chain_errors[0] if chain_errors else "chain break"
        return None, f"audit chain fails verification ({detail})", claimed

    recorded = [e for e in chain.query(event_type=EVENT_INTENT_CAPSULE) if e.details.get("task_id") == task_id]
    if not recorded:
        return None, "capsule is not recorded in the audit chain", claimed
    entry = recorded[-1]
    recomputed = capsule_hash(capsule)
    if str(entry.details.get("capsule_hash", "")) != recomputed:
        return (
            None,
            "capsule bytes do not match the audit-chain-recorded capsule hash (tampered or never approved)",
            claimed,
        )

    # The signed entry is the only authority on which run this capsule governs.
    run_id = str(entry.details.get("run_id", ""))
    if not run_id:
        return None, "audit-chain capsule entry records no run_id; cannot locate the run journal", claimed
    if sidecar_run_id and sidecar_run_id != run_id:
        return (
            None,
            f"capsule record run_id {sidecar_run_id!r} does not match the audit-chain-signed "
            f"run_id {run_id!r} (tampered)",
            run_id,
        )
    if expected_run_id and expected_run_id != run_id:
        return (
            None,
            f"supplied run_id {expected_run_id!r} does not match the audit-chain-signed run_id {run_id!r}",
            run_id,
        )

    lifecycle = _capsule_lifecycle_entries(chain, task_id)
    expired_at = chain_expiry_violation(lifecycle, capsule)
    if expired_at:
        return None, f"capsule expired at {capsule.expiry_ts}; audit chain records activity at {expired_at}", run_id

    try:
        journal_path = _run_journal_path(sdd_dir, run_id)
    except IntentCapsuleError as exc:
        return None, str(exc), run_id
    if not journal_path.exists():
        return None, f"run journal for {run_id!r} is missing; cannot recompute conformance", run_id
    jres = verify_journal(journal_path)
    if not jres.ok:
        detail = jres.errors[0] if jres.errors else "chain break"
        return None, f"run journal chain diverges ({detail}); steps were reordered or tampered", run_id

    events = load_events(journal_path)
    anchors = [
        e
        for e in events
        if str(e.get("event", "")) == CAPSULE_BOUND_EVENT
        and str(e.get("task_id", "")) == task_id
        and str(e.get("capsule_hash", "")) == recomputed
    ]
    if len(anchors) != 1:
        return (
            None,
            f"run journal carries {len(anchors)} matching {CAPSULE_BOUND_EVENT} anchors for this capsule, "
            f"expected exactly 1; the run is not attributable to the approved capsule",
            run_id,
        )

    # The journal's Merkle chain recomputes from genesis, so every prefix of it
    # verifies on its own: without an independent commitment to the END, a
    # worker deletes the rows that convict it and the remainder looks clean.
    # A missing seal is therefore a *coverage* statement, not a failure -- an
    # in-progress run has not sealed yet, and refusing here would suppress the
    # drift the caller came to find.
    seals = find_journal_seals(chain=chain, task_id=task_id, run_id=run_id, capsule_hash_value=recomputed)
    seal_state = SEAL_UNSEALED
    if seals:
        actual_head = journal_head(events)
        agreed = {(str(e.details.get("journal_head", "")), e.details.get("event_count")) for e in seals}
        if len(agreed) != 1:
            return (
                None,
                f"run {run_id!r} carries {len(agreed)} disagreeing journal seals; the chain cannot say "
                f"which journal state was attested",
                run_id,
            )
        sealed_head, sealed_count = next(iter(agreed))
        if not isinstance(sealed_count, int) or isinstance(sealed_count, bool):
            return None, "journal seal records no usable event_count; cannot bound the journal", run_id
        # Subsumed by the head comparison below -- the head is chained over
        # every event, so no count change can leave it intact -- and kept only
        # because "3 events, sealed 4" is a far clearer diagnostic than two
        # opaque hashes. It is not independent defence, and no test can isolate
        # it; the head check is the real guard.
        if len(events) != sealed_count:
            return (
                None,
                f"run journal holds {len(events)} events but the chain sealed {sealed_count}; "
                f"steps were added or removed after the run",
                run_id,
            )
        if actual_head != sealed_head:
            return (
                None,
                f"run journal head {actual_head or '(empty)'} does not match the chain-sealed head "
                f"{sealed_head or '(empty)'}; the journal was rewritten with the same number of events",
                run_id,
            )
        seal_state = SEAL_SEALED

    return _ChainedBinding(capsule=capsule, run_id=run_id, events=events, seal_state=seal_state), "", run_id


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
    * the run the capsule governs is taken from the **signed** audit entry, and
      an unsigned sidecar that names a different run is rejected as tampering;
    * the run journal's Merkle chain verifies (a reordered journal fails here);
    * that journal carries exactly one ``intent.capsule_bound`` anchor for this
      task and capsule hash;
    * the conformance verdict is recomputed as a pure function of the journal
      and the capsule.

    The run_id deliberately comes from the chain rather than from the on-disk
    capsule record. The sidecar is unsigned: were it authoritative, repointing
    it at any clean run would launder a drifted run into a clean verdict, and
    the capsule-bound anchor is what stops a clean journal from being replayed
    under a capsule it never governed.

    ``ok`` is True only when the chain and journal verify and the run is
    conformant. A drifted-but-untampered run returns ``ok=False`` with
    ``conformant=False`` and a divergence-naming reason.
    """
    from bernstein.core.replay.journal import JournalPathError, run_journal_path

    capsule, sidecar_run_id = read_capsule_binding(sdd_dir, task_id)
    if capsule is None:
        return IntentVerifyResult(ok=False, conformant=False, reason="no intent capsule for task")

    binding, reason, resolved_run_id = _resolve_chained_binding(
        sdd_dir=sdd_dir,
        chain=chain,
        task_id=task_id,
        capsule=capsule,
        sidecar_run_id=sidecar_run_id,
    )
    if binding is None:
        return IntentVerifyResult(
            ok=False,
            conformant=False,
            reason=reason,
            capsule=capsule,
            run_id=resolved_run_id,
            seal_state=SEAL_MISMATCH,
        )

    run_id = binding.run_id
    verdict = evaluate_conformance(binding.events, capsule, policy=policy)

    # Drift is reported whatever the seal state. Removing rows can only hide
    # drift, never invent it, so a divergence found in an unsealed journal is
    # real evidence and must not be withheld pending a seal.
    if not verdict.conformant:
        classes = ", ".join(sorted({d.action_class for d in verdict.divergences}))
        return IntentVerifyResult(
            ok=False,
            conformant=False,
            reason=f"drift: {len(verdict.divergences)} action(s) outside the capsule ({classes})",
            verdict=verdict,
            capsule=capsule,
            run_id=run_id,
            seal_state=binding.seal_state,
        )

    # A clean verdict is only worth honouring when the journal it was read from
    # sits inside the runs root and is still present. The binding resolved the
    # journal through a plain path join; re-derive it here with the containment
    # guard so an escaped or vanished journal cannot pass a truncated run off as
    # conformant. This runs only after drift is ruled out, so a genuine
    # divergence is never suppressed by a path defect.
    try:
        journal_path = run_journal_path(sdd_dir, run_id)
    except JournalPathError as exc:
        return IntentVerifyResult(
            ok=False,
            conformant=False,
            reason=f"invalid run id: {exc}",
            capsule=capsule,
            run_id=run_id,
            seal_state=binding.seal_state,
        )
    if not journal_path.exists():
        return IntentVerifyResult(
            ok=False,
            conformant=False,
            reason=f"run journal for {run_id!r} is missing; cannot recompute conformance",
            capsule=capsule,
            run_id=run_id,
            seal_state=binding.seal_state,
        )

    if binding.seal_state != SEAL_SEALED:
        return IntentVerifyResult(
            ok=False,
            conformant=True,
            reason=(
                f"no drift in the {len(binding.events)} recorded step(s), but run {run_id!r} is unsealed: "
                f"its end is not committed to the audit chain, so a truncated journal would look identical. "
                f"Completeness is not attested."
            ),
            verdict=verdict,
            capsule=capsule,
            run_id=run_id,
            seal_state=binding.seal_state,
        )

    return IntentVerifyResult(
        ok=True,
        conformant=True,
        reason="",
        verdict=verdict,
        capsule=capsule,
        run_id=run_id,
        seal_state=SEAL_SEALED,
    )


__all__ = [
    "CAPSULE_BOUND_EVENT",
    "INTENT_CAPSULE_VERSION",
    "LLM_FREE",
    "SEAL_MISMATCH",
    "SEAL_SEALED",
    "SEAL_UNSEALED",
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
    "chain_expiry_violation",
    "claimed_action_classes",
    "classify_journal_event",
    "compile_capsule",
    "evaluate_conformance",
    "find_journal_seals",
    "iter_module_import_names",
    "journal_head",
    "normalise_tool_name",
    "path_in_scope",
    "project_conformance_verdict",
    "read_capsule",
    "read_capsule_binding",
    "record_intent_drift",
    "seal_capsules_bound_to_run",
    "seal_run_journal",
    "verify_intent_conformance",
    "write_capsule",
]
