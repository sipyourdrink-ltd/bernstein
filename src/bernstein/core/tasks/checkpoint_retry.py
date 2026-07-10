"""Checkpointed retries: journal-anchored warm/fork/cold retry decisions.

Issue #2359. When a worker fails a gate or crashes, the scheduler restarts
the task from zero: full re-prompt, full cost, all in-context progress
discarded -- even though most primary CLI adapters expose native session
continuation and already plumb session identifiers. This module makes the
retry decision itself a deterministic, journal-anchored record:

* **Checkpoint references live in the task's event journal.** At every
  checkpoint the adapter's native session id and a workspace hash are
  appended as a Merkle-chained row to the task's existing
  :class:`~bernstein.core.replay.journal.EventJournal` (no second ledger).
  The row's ``event_hash`` is the checkpoint's identity.
* **The decision is a pure projection.** :func:`decide_retry` maps
  ``(requested mode, adapter capability, checkpoint reference, workspace
  hash comparison)`` onto an effective mode plus a downgrade reason, and
  the result carries a stable ``decision_hash`` -- two operators with the
  same inputs derive the byte-identical decision.
* **The safety valve is a hash comparison.** Before any warm resume the
  recorded workspace hash is compared against the live worktree; on
  mismatch the retry downgrades to cold, because provider-side session
  state can no longer be trusted to match the workspace. The downgrade is
  itself recorded (never silent in the ledger).
* **Corrective instructions are templates.** A warm/fork retry sends a
  template parameterized by the failed gate's name and output, never
  freeform text, so the warm prompt is auditable and token-bounded.
* **Every decision is recorded.** :func:`record_retry_decision` appends the
  decision to the task's event journal, seals its canonical bytes into the
  run lineage spine, and mirrors the identity into the HMAC audit chain via
  :func:`bernstein.core.security.audit_chain.record_checkpoint_retry`, so
  replay distinguishes warm from cold retries from the chain alone.

Tamper posture: :func:`latest_checkpoint` re-verifies the journal's Merkle
chain before trusting any row; a tampered checkpoint reference can only ever
produce a cold restart, never a warm resume of an attacker-chosen session.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any

from bernstein.adapters._contract import (
    CheckpointRetryCapability,
    checkpoint_retry_capability,
)
from bernstein.core.replay.journal import EventJournal, load_events, verify_journal

if TYPE_CHECKING:
    from bernstein.core.security.audit_chain import AuditChainStore

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Event-journal row type for a recorded checkpoint reference.
JOURNAL_EVENT_CHECKPOINT = "retry.checkpoint"

#: Event-journal row type for a recorded retry decision.
JOURNAL_EVENT_RETRY_DECISION = "retry.decision"

#: Directory names never folded into the workspace hash: VCS internals and
#: Bernstein runtime state change without the checked-out tree changing.
_WORKSPACE_HASH_EXCLUDED_DIRS = frozenset({".git", ".sdd"})

_RUN_ID_SAFE_RE = re.compile(r"[^A-Za-z0-9._-]")


class RetryMode(StrEnum):
    """How a failed task's retry continues the prior attempt."""

    #: Resume the recorded native session with a templated corrective
    #: instruction; in-context progress is preserved.
    WARM = "warm"
    #: Branch a fresh native session off the recorded checkpoint, leaving
    #: the original session intact.
    FORK = "fork"
    #: Restart from zero (the historical behavior).
    COLD = "cold"


#: Templated corrective instructions, parameterized by the failed gate's
#: name and output. Warm/fork retries send exactly one of these -- never
#: freeform text -- so the resumed session's new input is auditable.
CORRECTIVE_INSTRUCTION_TEMPLATES: dict[str, str] = {
    "gate_failure": (
        "Your previous attempt on this task failed the {gate_name} gate.\n"
        "Gate output:\n{gate_output}\n\n"
        "Fix that specific failure, re-run the gate, and finish the task. "
        "Do not redo work that already passed."
    ),
    "crash": (
        "Your previous attempt on this task terminated unexpectedly during {gate_name}.\n"
        "Last recorded output:\n{gate_output}\n\n"
        "Verify the state of your last step and continue from where you stopped."
    ),
    "timeout": (
        "Your previous attempt on this task ran out of budget during {gate_name}.\n"
        "Last recorded output:\n{gate_output}\n\n"
        "Continue from your last completed step and prioritize finishing."
    ),
}


def render_corrective_instruction(template_id: str, *, gate_name: str, gate_output: str) -> str:
    """Render the corrective instruction for a warm/fork retry.

    Args:
        template_id: Key into :data:`CORRECTIVE_INSTRUCTION_TEMPLATES`.
        gate_name: Name of the failed gate (e.g. ``"pytest"``).
        gate_output: The failed gate's output, injected verbatim into the
            template body.

    Returns:
        The rendered instruction text.

    Raises:
        ValueError: ``template_id`` is not a known template. Freeform
            corrective text is refused by design.
    """
    template = CORRECTIVE_INSTRUCTION_TEMPLATES.get(template_id)
    if template is None:
        known = ", ".join(sorted(CORRECTIVE_INSTRUCTION_TEMPLATES))
        msg = f"unknown corrective template {template_id!r}; known templates: {known}"
        raise ValueError(msg)
    return template.format(
        gate_name=gate_name or "an unnamed gate",
        gate_output=gate_output or "<no output recorded>",
    )


# ---------------------------------------------------------------------------
# Task journal + workspace hash
# ---------------------------------------------------------------------------


def task_run_id(task_id: str) -> str:
    """Return the deterministic event-journal run id for ``task_id``.

    The id is filesystem-safe (path separators and other unsafe characters
    are replaced) and stable, so every process that touches the task appends
    to the same journal.
    """
    safe = _RUN_ID_SAFE_RE.sub("-", task_id) or "unknown"
    return f"task-{safe}"


def workspace_hash(worktree: Path) -> str:
    """Return a deterministic content hash of the worktree's file tree.

    The hash folds in every regular file's repo-relative POSIX path and
    content SHA-256, sorted by path, excluding VCS internals (``.git``) and
    Bernstein runtime state (``.sdd``). Two directories with identical
    trees hash identically regardless of location or process CWD; any
    content change flips the digest.

    Symlinks and unreadable files are skipped (a broken link must not make
    the guard non-deterministic across hosts). A missing directory hashes
    to the empty string, which never matches a recorded hash.
    """
    root = Path(worktree)
    if not root.is_dir():
        return ""
    root = root.resolve()
    digest = hashlib.sha256()
    entries: list[tuple[str, Path]] = []
    for candidate in root.rglob("*"):
        try:
            relative = candidate.relative_to(root)
        except ValueError:  # pragma: no cover -- rglob stays under root
            continue
        if any(part in _WORKSPACE_HASH_EXCLUDED_DIRS for part in relative.parts):
            continue
        if candidate.is_symlink() or not candidate.is_file():
            continue
        entries.append((relative.as_posix(), candidate))
    for rel_posix, path in sorted(entries):
        try:
            content = path.read_bytes()
        except OSError:
            continue
        digest.update(rel_posix.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(hashlib.sha256(content).hexdigest().encode("ascii"))
        digest.update(b"\n")
    return digest.hexdigest()


@dataclass(frozen=True)
class CheckpointRef:
    """A journal-anchored reference to a resumable native session.

    Attributes:
        task_id: The task the checkpoint belongs to.
        adapter: Registry name of the adapter that owns the session.
        session_id: The native session identifier the adapter handed back.
        workspace_hash: :func:`workspace_hash` of the worktree at
            checkpoint time (the safety-valve baseline).
        worktree_path: Absolute path of the worktree the hash was taken
            over (local-only scope, mirroring the resume checkpoint).
        journal_index: 0-based index of the checkpoint row in the task's
            event journal.
        event_hash: Merkle hash of that row -- the checkpoint's identity.
    """

    task_id: str
    adapter: str
    session_id: str
    workspace_hash: str
    worktree_path: str
    journal_index: int
    event_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "adapter": self.adapter,
            "session_id": self.session_id,
            "workspace_hash": self.workspace_hash,
            "worktree_path": self.worktree_path,
            "journal_index": self.journal_index,
            "event_hash": self.event_hash,
        }


def record_task_checkpoint(
    *,
    sdd_dir: Path,
    task_id: str,
    adapter: str,
    session_id: str,
    workspace_hash: str,
    worktree_path: str = "",
) -> CheckpointRef:
    """Append a checkpoint reference to the task's event journal.

    The row extends the task journal's Merkle chain; its ``event_hash`` is
    the checkpoint identity later bound into the retry decision and the
    audit chain. Appends are chain-continuing across processes (the journal
    is opened via :meth:`EventJournal.resume`).

    Args:
        sdd_dir: Project ``.sdd`` directory.
        task_id: Task the session belongs to.
        adapter: Registry name of the adapter that owns the session.
        session_id: The native session identifier to record.
        workspace_hash: :func:`workspace_hash` of the worktree right now.
        worktree_path: Absolute worktree path the hash was computed over.

    Returns:
        The anchored :class:`CheckpointRef`.

    Raises:
        ValueError: The existing journal fails chain verification.
        RuntimeError: The journal append did not extend the chain.
    """
    journal = EventJournal.resume(task_run_id(task_id), sdd_dir)
    head_before = journal.head()
    journal.record(
        JOURNAL_EVENT_CHECKPOINT,
        task_id=task_id,
        adapter=adapter,
        session_id=session_id,
        workspace_hash=workspace_hash,
        worktree_path=worktree_path,
    )
    if journal.head() == head_before:
        msg = f"checkpoint journal append failed for task {task_id!r}"
        raise RuntimeError(msg)
    return CheckpointRef(
        task_id=task_id,
        adapter=adapter,
        session_id=session_id,
        workspace_hash=workspace_hash,
        worktree_path=worktree_path,
        journal_index=journal.event_count() - 1,
        event_hash=journal.head(),
    )


def latest_checkpoint(sdd_dir: Path, task_id: str) -> CheckpointRef | None:
    """Return the most recent verified checkpoint for ``task_id``.

    Fail-closed: the journal's Merkle chain is re-verified before any row
    is trusted. A missing journal, a chain that does not recompute, or the
    absence of any checkpoint row all return ``None`` -- the caller then
    restarts cold. A tampered checkpoint reference can therefore never fuel
    a warm resume.
    """
    path = sdd_dir / "runs" / task_run_id(task_id) / "journal.jsonl"
    if not path.exists():
        return None
    result = verify_journal(path)
    if not result.ok:
        logger.warning(
            "checkpoint journal for task %s failed chain verification at index %s; forcing cold retry",
            task_id,
            result.divergent_index,
        )
        return None
    for row in reversed(load_events(path)):
        if row.get("event") != JOURNAL_EVENT_CHECKPOINT:
            continue
        if str(row.get("task_id", "")) != task_id:
            continue
        try:
            journal_index = int(row.get("index", -1))
        except (TypeError, ValueError):
            continue
        return CheckpointRef(
            task_id=task_id,
            adapter=str(row.get("adapter", "")),
            session_id=str(row.get("session_id", "")),
            workspace_hash=str(row.get("workspace_hash", "")),
            worktree_path=str(row.get("worktree_path", "")),
            journal_index=journal_index,
            event_hash=str(row.get("event_hash", "")),
        )
    return None


# ---------------------------------------------------------------------------
# The retry decision (deterministic projection)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetryDecision:
    """A deterministic warm/fork/cold retry decision.

    Every field is a pure function of the decision inputs; two operators
    with identical inputs derive the byte-identical record including
    :attr:`decision_hash`.
    """

    task_id: str
    adapter: str
    capability: str
    requested_mode: RetryMode
    effective_mode: RetryMode
    checkpoint_session_id: str
    checkpoint_event_hash: str
    checkpoint_journal_index: int
    recorded_workspace_hash: str
    actual_workspace_hash: str
    workspace_match: bool
    downgrade_reason: str
    corrective_template_id: str
    corrective_instruction: str
    decision_hash: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "adapter": self.adapter,
            "capability": self.capability,
            "requested_mode": str(self.requested_mode),
            "effective_mode": str(self.effective_mode),
            "checkpoint_session_id": self.checkpoint_session_id,
            "checkpoint_event_hash": self.checkpoint_event_hash,
            "checkpoint_journal_index": self.checkpoint_journal_index,
            "recorded_workspace_hash": self.recorded_workspace_hash,
            "actual_workspace_hash": self.actual_workspace_hash,
            "workspace_match": self.workspace_match,
            "downgrade_reason": self.downgrade_reason,
            "corrective_template_id": self.corrective_template_id,
            "corrective_instruction": self.corrective_instruction,
            "decision_hash": self.decision_hash,
        }

    def canonical_bytes(self) -> bytes:
        """Canonical JSON bytes of the full decision (sealed into lineage)."""
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")


def _decision_hash(fields: dict[str, Any]) -> str:
    """SHA-256 over the canonical projection of every decision field."""
    canonical = json.dumps(fields, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def decide_retry(
    *,
    task_id: str,
    requested_mode: RetryMode | str,
    checkpoint: CheckpointRef | None,
    actual_workspace_hash: str,
    template_id: str = "gate_failure",
    gate_name: str = "",
    gate_output: str = "",
    force_cold: bool = False,
) -> RetryDecision:
    """Decide warm/fork/cold for a failed task's retry.

    A pure function of its inputs -- no clock, no filesystem, no network --
    so the decision replays byte-identically. Resolution order:

    1. ``force_cold`` (a task pinned to fresh-context retries) wins.
    2. A requested cold stays cold.
    3. No verified checkpoint, or a checkpoint without a session id,
       falls back to cold.
    4. An adapter without the capability falls back to cold.
    5. A workspace-hash mismatch (or an uncomputable live hash) downgrades
       to cold: provider-side session state can no longer be trusted to
       match the workspace.
    6. A fork request against a resume-only adapter downgrades to warm.
    7. Otherwise the requested mode is honored.

    Args:
        task_id: The failed task being retried.
        requested_mode: The retry policy's requested mode.
        checkpoint: The latest verified checkpoint, or ``None``.
        actual_workspace_hash: :func:`workspace_hash` of the live worktree
            (empty when it could not be computed).
        template_id: Corrective-instruction template for warm/fork.
        gate_name: Failed gate's name, folded into the template.
        gate_output: Failed gate's output, folded into the template.
        force_cold: Pin the decision to cold regardless of capability
            (recorded as ``fresh_context_restart``).

    Returns:
        The deterministic :class:`RetryDecision`.

    Raises:
        ValueError: ``requested_mode`` is not a known mode, or
            ``template_id`` is not a known template.
    """
    requested = RetryMode(requested_mode)
    adapter = checkpoint.adapter if checkpoint is not None else ""
    capability = checkpoint_retry_capability(adapter) if adapter else CheckpointRetryCapability.NONE
    workspace_match = (
        checkpoint is not None and bool(actual_workspace_hash) and actual_workspace_hash == checkpoint.workspace_hash
    )

    downgrade_reason = ""
    if force_cold:
        effective = RetryMode.COLD
        downgrade_reason = "fresh_context_restart"
    elif requested is RetryMode.COLD:
        effective = RetryMode.COLD
    elif checkpoint is None:
        effective = RetryMode.COLD
        downgrade_reason = "no_checkpoint"
    elif not checkpoint.session_id:
        effective = RetryMode.COLD
        downgrade_reason = "no_session_id"
    elif capability is CheckpointRetryCapability.NONE:
        effective = RetryMode.COLD
        downgrade_reason = "adapter_capability_none"
    elif not workspace_match:
        effective = RetryMode.COLD
        downgrade_reason = "workspace_hash_mismatch"
    elif requested is RetryMode.FORK and capability is not CheckpointRetryCapability.FORK:
        effective = RetryMode.WARM
        downgrade_reason = "fork_downgraded_to_warm"
    else:
        effective = requested

    corrective_instruction = ""
    if effective is not RetryMode.COLD:
        corrective_instruction = render_corrective_instruction(
            template_id, gate_name=gate_name, gate_output=gate_output
        )

    fields: dict[str, Any] = {
        "task_id": task_id,
        "adapter": adapter,
        "capability": str(capability),
        "requested_mode": str(requested),
        "effective_mode": str(effective),
        "checkpoint_session_id": checkpoint.session_id if checkpoint else "",
        "checkpoint_event_hash": checkpoint.event_hash if checkpoint else "",
        "checkpoint_journal_index": checkpoint.journal_index if checkpoint else -1,
        "recorded_workspace_hash": checkpoint.workspace_hash if checkpoint else "",
        "actual_workspace_hash": actual_workspace_hash,
        "workspace_match": workspace_match,
        "downgrade_reason": downgrade_reason,
        "corrective_template_id": template_id if effective is not RetryMode.COLD else "",
        "corrective_instruction": corrective_instruction,
    }
    return RetryDecision(
        task_id=task_id,
        adapter=adapter,
        capability=str(capability),
        requested_mode=requested,
        effective_mode=effective,
        checkpoint_session_id=str(fields["checkpoint_session_id"]),
        checkpoint_event_hash=str(fields["checkpoint_event_hash"]),
        checkpoint_journal_index=int(fields["checkpoint_journal_index"]),
        recorded_workspace_hash=str(fields["recorded_workspace_hash"]),
        actual_workspace_hash=actual_workspace_hash,
        workspace_match=workspace_match,
        downgrade_reason=downgrade_reason,
        corrective_template_id=str(fields["corrective_template_id"]),
        corrective_instruction=corrective_instruction,
        decision_hash=_decision_hash(fields),
    )


def build_retry_prompt(decision: RetryDecision, *, cold_prompt: str) -> str:
    """Return the input the retry attempt sends to the agent.

    A cold retry replays the full prompt; a warm/fork retry resumes the
    native session, so the only new input is the templated corrective
    instruction -- the source of the warm-vs-cold input-token delta.
    """
    if decision.effective_mode is RetryMode.COLD:
        return cold_prompt
    return decision.corrective_instruction


# ---------------------------------------------------------------------------
# Recording: journal + lineage spine + audit chain
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RetryDecisionRecord:
    """Anchors produced by :func:`record_retry_decision`.

    Attributes:
        journal_index: 0-based index of the decision row in the task's
            event journal.
        journal_event_hash: Merkle hash of that row.
        spine_entry_hash: Lineage-spine entry hash of the sealed decision
            bytes (empty when lineage recording is disabled).
    """

    journal_index: int
    journal_event_hash: str
    spine_entry_hash: str


def record_retry_decision(
    *,
    sdd_dir: Path,
    decision: RetryDecision,
    audit_chain: AuditChainStore | None = None,
    hmac_key: bytes | None = None,
) -> RetryDecisionRecord:
    """Record *decision* into journal, lineage spine, and audit chain.

    1. Appends a ``retry.decision`` row to the task's event journal (the
       same Merkle chain that carries the checkpoint references).
    2. Seals the decision's canonical bytes into the run lineage spine with
       a deterministic timestamp (the journal index), so two identical
       decisions seal byte-identically.
    3. When *audit_chain* is supplied, mirrors the decision's identity into
       the HMAC chain via
       :func:`bernstein.core.security.audit_chain.record_checkpoint_retry`.

    Args:
        sdd_dir: Project ``.sdd`` directory.
        decision: The decision to record.
        audit_chain: Optional :class:`AuditChainStore` accepting the mirror.
        hmac_key: Audit-chain HMAC key for the lineage seal. Loaded via the
            canonical resolver when omitted.

    Returns:
        A :class:`RetryDecisionRecord` with the journal and spine anchors.

    Raises:
        ValueError: The existing task journal fails chain verification.
        RuntimeError: The journal append did not extend the chain.
    """
    run_id = task_run_id(decision.task_id)
    journal = EventJournal.resume(run_id, sdd_dir)
    head_before = journal.head()
    journal.record(JOURNAL_EVENT_RETRY_DECISION, **decision.to_dict())
    if journal.head() == head_before:
        msg = f"retry-decision journal append failed for task {decision.task_id!r}"
        raise RuntimeError(msg)
    journal_index = journal.event_count() - 1
    journal_event_hash = journal.head()

    if hmac_key is None:
        from bernstein.core.security.audit import load_or_create_audit_key

        hmac_key = load_or_create_audit_key()

    from bernstein.adapters.base import record_artifact_write

    spine_entry_hash = record_artifact_write(
        artifact_path=f".sdd/runs/{run_id}/retry-decision-{journal_index}.json",
        content=decision.canonical_bytes(),
        actor="checkpoint_retry",
        step_id=f"retry-decision:{decision.decision_hash}",
        model="",
        lineage_root=sdd_dir / "lineage",
        run_id=run_id,
        hmac_key=hmac_key,
        timestamp=journal_index,
    )

    if audit_chain is not None:
        from bernstein.core.security.audit_chain import record_checkpoint_retry

        record_checkpoint_retry(
            chain=audit_chain,
            task_id=decision.task_id,
            retry_mode=str(decision.effective_mode),
            requested_mode=str(decision.requested_mode),
            capability=decision.capability,
            checkpoint_event_hash=decision.checkpoint_event_hash,
            checkpoint_journal_index=decision.checkpoint_journal_index,
            workspace_match=decision.workspace_match,
            downgrade_reason=decision.downgrade_reason,
            decision_hash=decision.decision_hash,
            journal_event_hash=journal_event_hash,
            journal_entry_hash=spine_entry_hash or "",
        )

    return RetryDecisionRecord(
        journal_index=journal_index,
        journal_event_hash=journal_event_hash,
        spine_entry_hash=spine_entry_hash or "",
    )


# ---------------------------------------------------------------------------
# Retry-path integration surface
# ---------------------------------------------------------------------------

_VALID_REQUESTED_MODES = frozenset(str(mode) for mode in RetryMode)


def stamp_checkpoint_retry_metadata(
    *,
    metadata: dict[str, Any],
    task_id: str,
    workdir: Path,
    requested_mode: str = "warm",
    gate_name: str = "",
    gate_output: str = "",
    force_cold: bool = False,
) -> dict[str, Any]:
    """Decide, record, and stamp the checkpointed-retry mode for a retry task.

    Called from the retry path with the metadata about to be posted on the
    retried task. Loads the latest verified checkpoint, computes the live
    workspace hash over the recorded worktree, derives the deterministic
    decision, records it (journal + lineage spine + audit chain, best
    effort), and returns a copy of *metadata* with additive ``retry_*``
    keys:

    * ``retry_mode`` / ``retry_requested_mode`` / ``retry_decision_hash``
      -- always stamped.
    * ``retry_downgrade_reason`` -- stamped when a non-cold request was
      downgraded.
    * ``retry_checkpoint_session_id`` / ``retry_checkpoint_event_hash`` /
      ``retry_corrective_instruction`` -- stamped only for warm/fork, the
      exact inputs a session-resuming spawn needs.

    Recording failures never mask the decision: the stamp still lands so
    the retry proceeds (cold-safe), and the recording error is logged by
    exception type only.
    """
    stamped = dict(metadata)
    requested = requested_mode if requested_mode in _VALID_REQUESTED_MODES else str(RetryMode.WARM)
    sdd_dir = Path(workdir) / ".sdd"

    checkpoint = latest_checkpoint(sdd_dir, task_id)
    actual_hash = ""
    if checkpoint is not None and checkpoint.worktree_path:
        actual_hash = workspace_hash(Path(checkpoint.worktree_path))

    decision = decide_retry(
        task_id=task_id,
        requested_mode=requested,
        checkpoint=checkpoint,
        actual_workspace_hash=actual_hash,
        gate_name=gate_name,
        gate_output=gate_output,
        force_cold=force_cold,
    )

    try:
        from bernstein.core.security.audit_chain import AuditChainStore

        chain = AuditChainStore(sdd_dir / "audit")
        record_retry_decision(sdd_dir=sdd_dir, decision=decision, audit_chain=chain)
    except Exception as exc:
        logger.warning(
            "checkpoint-retry decision recording failed for task %s (%s); stamping metadata anyway",
            task_id,
            type(exc).__name__,
        )

    stamped["retry_mode"] = str(decision.effective_mode)
    stamped["retry_requested_mode"] = str(decision.requested_mode)
    stamped["retry_decision_hash"] = decision.decision_hash
    if decision.downgrade_reason:
        stamped["retry_downgrade_reason"] = decision.downgrade_reason
    if decision.effective_mode is not RetryMode.COLD:
        stamped["retry_checkpoint_session_id"] = decision.checkpoint_session_id
        stamped["retry_checkpoint_event_hash"] = decision.checkpoint_event_hash
        stamped["retry_corrective_instruction"] = decision.corrective_instruction
    return stamped


__all__ = [
    "CORRECTIVE_INSTRUCTION_TEMPLATES",
    "JOURNAL_EVENT_CHECKPOINT",
    "JOURNAL_EVENT_RETRY_DECISION",
    "CheckpointRef",
    "RetryDecision",
    "RetryDecisionRecord",
    "RetryMode",
    "build_retry_prompt",
    "decide_retry",
    "latest_checkpoint",
    "record_retry_decision",
    "record_task_checkpoint",
    "render_corrective_instruction",
    "stamp_checkpoint_retry_metadata",
    "task_run_id",
    "workspace_hash",
]
