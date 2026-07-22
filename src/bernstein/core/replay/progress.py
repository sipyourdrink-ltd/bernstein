"""Chain-computed task progress (#2553).

Progress is a **pure projection** folded from rows that only real work
produces: checkpoint references in the task journal
(:mod:`bernstein.core.tasks.checkpoint_retry`), evidence producers declared
versus passed (:mod:`bernstein.core.evidence.bundle`), diff-capture and gate
attempts already folded by the review board projection
(:mod:`bernstein.core.replay.review_board`), and task transitions in the work
ledger (:mod:`bernstein.core.persistence.work_ledger`).

There is deliberately **no way to set progress**: no artifact type, no MCP
input, and no API field assigns a number. A worker that posts fifty report
artifacts while checkpointing nothing leaves the vector unchanged, because
``artifact_posted`` rows never enter the fold. A worker advances the vector
only by doing journaled work.

Wall clock never enters the fold. Two projections of the same rows -- including
on a second host after a ledger resume -- yield byte-identical canonical bytes
under :func:`canonical_progress_bytes` and the same
:meth:`ProgressVector.vector_hash`. That byte-identity is the determinism
witness the golden test asserts.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.defaults import JOURNAL_EVENT_ARTIFACT_POSTED as EVENT_ARTIFACT_POSTED

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

#: Schema version of the canonical progress vector. Bump on any change to the
#: canonical field set so a stale reader never silently misreads a new vector.
PROGRESS_SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# Journal event wire strings the fold recognises.
#
# These are stable wire contracts owned by other modules. The artifact event
# comes from the central defaults module; tests assert the remaining local
# names equal their source-of-truth constants so drift is caught.
# ---------------------------------------------------------------------------

#: A worker checkpoint row, from ``checkpoint_retry.JOURNAL_EVENT_CHECKPOINT``.
EVENT_CHECKPOINT = "retry.checkpoint"

#: A captured task diff, from ``review_board.EVENT_TASK_DIFF_CAPTURED``.
EVENT_DIFF_CAPTURED = "task_diff_captured"

#: A gate/review decision, from ``review_board.EVENT_TASK_REVIEW_DECISION``.
EVENT_REVIEW_DECISION = "task_review_decision"

#: The imported agent-posted artifact row is named here only so the fold can
#: prove it is ignored -- posting artifacts must never move progress (#2553 AC4).

#: Work-ledger phases considered terminal. Once a task reaches one, its run is
#: over: the vector's ``terminal`` flag latches True.
_TERMINAL_PHASES: frozenset[str] = frozenset({"completed", "failed", "abandoned"})

#: Monotone ordinal of a ledger phase, so two vectors can be compared for
#: advancement. Unknown phases sort as 0 (not started).
_PHASE_ORDINAL: dict[str, int] = {
    "": 0,
    "scheduled": 1,
    "started": 2,
    "suspended": 2,
    "resumed": 2,
    "completed": 3,
    "failed": 3,
    "abandoned": 3,
}


@dataclass(frozen=True, slots=True)
class ProgressVector:
    """A task's chain-computed progress, bound to nothing a worker can assert.

    Every field is a count or a projected phase folded from journaled work.
    The dataclass is frozen with only hashable primitive fields, so it is
    itself hashable and safe to embed in a frozen run handle.

    Attributes:
        task_id: The task this vector projects.
        schema_version: :data:`PROGRESS_SCHEMA_VERSION`.
        checkpoints: Count of ``retry.checkpoint`` rows -- real resumable work.
        diffs_captured: Count of ``task_diff_captured`` rows.
        gate_attempts: Count of ``task_review_decision`` rows.
        evidence_declared: Evidence producers declared for the task.
        evidence_passed: Evidence producers that passed.
        ledger_phase: The work-ledger phase (``scheduled`` .. ``completed``).
        ledger_attempts: Number of ``task.started`` transitions (retries).
        terminal: Whether the ledger phase is terminal.
    """

    task_id: str
    schema_version: int = PROGRESS_SCHEMA_VERSION
    checkpoints: int = 0
    diffs_captured: int = 0
    gate_attempts: int = 0
    evidence_declared: int = 0
    evidence_passed: int = 0
    ledger_phase: str = ""
    ledger_attempts: int = 0
    terminal: bool = False

    @property
    def phase_ordinal(self) -> int:
        """Monotone ordinal of :attr:`ledger_phase` (unknown phases -> 0)."""
        return _PHASE_ORDINAL.get(self.ledger_phase, 0)

    @property
    def earned_steps(self) -> int:
        """Coarse count of earned work units (checkpoints, diffs, gates, passes).

        A single monotone scalar convenient for a progress strip. It rises only
        when journaled work lands; posting artifacts never moves it.
        """
        return self.checkpoints + self.diffs_captured + self.gate_attempts + self.evidence_passed

    def to_canonical_dict(self) -> dict[str, Any]:
        """Return the canonical, order-stable dict the hash is computed over."""
        return {
            "task_id": self.task_id,
            "schema_version": self.schema_version,
            "checkpoints": self.checkpoints,
            "diffs_captured": self.diffs_captured,
            "gate_attempts": self.gate_attempts,
            "evidence_declared": self.evidence_declared,
            "evidence_passed": self.evidence_passed,
            "ledger_phase": self.ledger_phase,
            "ledger_attempts": self.ledger_attempts,
            "terminal": self.terminal,
        }

    def vector_hash(self) -> str:
        """Return the SHA-256 hex digest of :func:`canonical_progress_bytes`."""
        return hashlib.sha256(canonical_progress_bytes(self)).hexdigest()

    def to_wire(self) -> dict[str, Any]:
        """Return the API/SSE body: the canonical vector plus its hash."""
        return self.to_canonical_dict() | {
            "earned_steps": self.earned_steps,
            "phase_ordinal": self.phase_ordinal,
            "vector_hash": self.vector_hash(),
        }

    def dominates(self, other: ProgressVector) -> bool:
        """Whether every component is >= the corresponding component of *other*."""
        return (
            self.checkpoints >= other.checkpoints
            and self.diffs_captured >= other.diffs_captured
            and self.gate_attempts >= other.gate_attempts
            and self.evidence_declared >= other.evidence_declared
            and self.evidence_passed >= other.evidence_passed
            and self.ledger_attempts >= other.ledger_attempts
            and self.phase_ordinal >= other.phase_ordinal
        )

    def strictly_advances(self, other: ProgressVector) -> bool:
        """Whether this vector dominates *other* and is strictly greater somewhere."""
        return self.dominates(other) and self.to_canonical_dict() != other.to_canonical_dict()


def canonical_progress_bytes(vector: ProgressVector) -> bytes:
    """Return the canonical UTF-8 JSON bytes of a progress vector.

    Sorted keys, compact separators -- the exact bytes
    :meth:`ProgressVector.vector_hash` signs, and the byte-identity witness for
    the determinism guarantee. Wall clock is never a field, so the bytes depend
    only on journaled work.
    """
    return json.dumps(vector.to_canonical_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")


def fold_progress(
    *,
    task_id: str,
    journal_rows: Sequence[Mapping[str, Any]] = (),
    ledger_phase: str = "",
    ledger_attempts: int = 0,
    evidence_declared: int = 0,
    evidence_passed: int = 0,
) -> ProgressVector:
    """Fold journaled work into a :class:`ProgressVector` (pure function).

    Only rows that record real work move the vector. ``artifact_posted`` rows
    are counted by no branch, so a worker cannot inflate progress by posting
    artifacts. The wall-clock envelope on each row (``ts`` / ``elapsed_s``) is
    never read, so two folds over the same inputs are byte-identical.

    Args:
        task_id: The task being projected.
        journal_rows: Ordered task-journal rows (mappings with an ``event``
            key, as produced by
            :func:`bernstein.core.replay.journal.load_events`).
        ledger_phase: Projected work-ledger phase for the task, if known.
        ledger_attempts: Number of ``task.started`` transitions.
        evidence_declared: Evidence producers declared for the task.
        evidence_passed: Evidence producers that passed.

    Returns:
        The projected :class:`ProgressVector`.
    """
    checkpoints = 0
    diffs_captured = 0
    gate_attempts = 0
    for row in journal_rows:
        event = str(row.get("event", ""))
        if event == EVENT_CHECKPOINT:
            checkpoints += 1
        elif event == EVENT_DIFF_CAPTURED:
            diffs_captured += 1
        elif event == EVENT_REVIEW_DECISION:
            gate_attempts += 1
        # EVENT_ARTIFACT_POSTED and every other event: intentionally ignored.
    return ProgressVector(
        task_id=task_id,
        checkpoints=checkpoints,
        diffs_captured=diffs_captured,
        gate_attempts=gate_attempts,
        evidence_declared=max(0, evidence_declared),
        evidence_passed=max(0, evidence_passed),
        ledger_phase=ledger_phase,
        ledger_attempts=max(0, ledger_attempts),
        terminal=ledger_phase in _TERMINAL_PHASES,
    )


def project_task_progress(sdd_dir: Path, task_id: str, *, run_id: str = "") -> ProgressVector:
    """Gather the fold inputs from disk and project a task's progress vector.

    IO-bearing convenience over :func:`fold_progress`. Reads the task journal
    (fail-closed: a journal that fails Merkle verification contributes no
    checkpoint rows), the work ledger for ``run_id`` when supplied, and the
    sealed evidence bundle when one exists. The fold itself stays pure; this
    function only decides which rows and counts to feed it.

    Args:
        sdd_dir: The ``.sdd`` directory of the run.
        task_id: The task to project.
        run_id: The run whose work ledger records the task's transitions. When
            empty, ledger phase/attempts are left at their defaults.

    Returns:
        The projected :class:`ProgressVector`.
    """
    journal_rows = _load_task_journal_rows(sdd_dir, task_id)
    ledger_phase, ledger_attempts = _project_ledger_phase(sdd_dir, task_id, run_id)
    declared, passed = _project_evidence_counts(sdd_dir, task_id)
    return fold_progress(
        task_id=task_id,
        journal_rows=journal_rows,
        ledger_phase=ledger_phase,
        ledger_attempts=ledger_attempts,
        evidence_declared=declared,
        evidence_passed=passed,
    )


def _load_task_journal_rows(sdd_dir: Path, task_id: str) -> list[dict[str, Any]]:
    """Return the task journal rows, or ``[]`` when absent or tampered."""
    from bernstein.core.replay.journal import load_events, verify_journal
    from bernstein.core.security.path_containment import PathTooLongError
    from bernstein.core.tasks.checkpoint_retry import task_journal_path

    try:
        path = task_journal_path(sdd_dir, task_id)
    except PathTooLongError:
        # Cannot name a file here, so there is no journal to read. A
        # containment failure is not caught: an escape must surface.
        return []
    if not path.is_file():
        return []
    # Fail-closed: a journal whose Merkle chain does not verify is not a
    # trustworthy source of checkpoint counts.
    if not verify_journal(path).ok:
        return []
    return load_events(path)


def _project_ledger_phase(sdd_dir: Path, task_id: str, run_id: str) -> tuple[str, int]:
    """Return ``(phase, attempts)`` from the work ledger, or ``("", 0)``."""
    if not run_id:
        return "", 0
    try:
        from bernstein.core.persistence.work_ledger import (
            LedgerReader,
            replay_state,
            run_ledger_dir,
        )

        reader = LedgerReader(run_ledger_dir(sdd_dir, run_id))
        state = replay_state(reader.entries(), run_id=run_id)
        task_state = state.tasks.get(task_id)
        if task_state is None:
            return "", 0
        return str(task_state.state), int(task_state.attempts)
    except (OSError, ValueError):
        return "", 0


def _project_evidence_counts(sdd_dir: Path, task_id: str) -> tuple[int, int]:
    """Return ``(declared, passed)`` producer counts from the evidence bundle."""
    try:
        from bernstein.core.evidence.bundle import read_evidence_bundle

        bundle = read_evidence_bundle(sdd_dir.parent, task_id)
    except (OSError, ValueError):
        return 0, 0
    if bundle is None:
        return 0, 0
    return len(bundle.items), int(bundle.passed_count)


__all__ = [
    "EVENT_ARTIFACT_POSTED",
    "EVENT_CHECKPOINT",
    "EVENT_DIFF_CAPTURED",
    "EVENT_REVIEW_DECISION",
    "PROGRESS_SCHEMA_VERSION",
    "ProgressVector",
    "canonical_progress_bytes",
    "fold_progress",
    "project_task_progress",
]
