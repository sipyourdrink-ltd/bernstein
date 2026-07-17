"""Chain-computed task progress projection (#2553).

Progress is a pure fold of journaled work. These tests assert the three
guarantees the ticket calls out: determinism (byte-identical bytes + hash for
the same rows), non-assertability (posting artifacts never moves the vector),
and monotone advancement as real work lands.
"""

from __future__ import annotations

from bernstein.core.replay.progress import (
    EVENT_ARTIFACT_POSTED,
    EVENT_CHECKPOINT,
    EVENT_DIFF_CAPTURED,
    EVENT_REVIEW_DECISION,
    ProgressVector,
    canonical_progress_bytes,
    fold_progress,
)


def _rows(*events: str) -> list[dict[str, object]]:
    return [{"event": e, "ts": 123.4, "elapsed_s": 1.0, "index": i} for i, e in enumerate(events)]


class TestConstantsInSync:
    """The duplicated wire strings must equal their source-of-truth constants."""

    def test_checkpoint_constant_matches(self) -> None:
        from bernstein.core.tasks.checkpoint_retry import JOURNAL_EVENT_CHECKPOINT

        assert EVENT_CHECKPOINT == JOURNAL_EVENT_CHECKPOINT

    def test_review_board_constants_match(self) -> None:
        from bernstein.core.replay.review_board import (
            EVENT_TASK_DIFF_CAPTURED,
            EVENT_TASK_REVIEW_DECISION,
        )

        assert EVENT_DIFF_CAPTURED == EVENT_TASK_DIFF_CAPTURED
        assert EVENT_REVIEW_DECISION == EVENT_TASK_REVIEW_DECISION

    def test_artifact_constant_matches(self) -> None:
        from bernstein.core.evidence.run_artifacts import JOURNAL_EVENT_ARTIFACT_POSTED

        assert EVENT_ARTIFACT_POSTED == JOURNAL_EVENT_ARTIFACT_POSTED


class TestDeterminism:
    def test_same_rows_yield_identical_bytes_and_hash(self) -> None:
        rows = _rows(EVENT_CHECKPOINT, EVENT_CHECKPOINT, EVENT_DIFF_CAPTURED)
        a = fold_progress(task_id="t1", journal_rows=rows, ledger_phase="started", ledger_attempts=1)
        b = fold_progress(task_id="t1", journal_rows=rows, ledger_phase="started", ledger_attempts=1)
        assert canonical_progress_bytes(a) == canonical_progress_bytes(b)
        assert a.vector_hash() == b.vector_hash()

    def test_wall_clock_never_enters_the_fold(self) -> None:
        # Two runs with wildly different timestamps but identical events must
        # project byte-identical vectors: no clock in the canonical bytes.
        early = [{"event": EVENT_CHECKPOINT, "ts": 1.0, "elapsed_s": 0.0, "index": 0}]
        late = [{"event": EVENT_CHECKPOINT, "ts": 9e9, "elapsed_s": 5000.0, "index": 0}]
        a = fold_progress(task_id="t1", journal_rows=early)
        b = fold_progress(task_id="t1", journal_rows=late)
        assert canonical_progress_bytes(a) == canonical_progress_bytes(b)

    def test_golden_bytes(self) -> None:
        vec = fold_progress(
            task_id="task-golden",
            journal_rows=_rows(EVENT_CHECKPOINT, EVENT_REVIEW_DECISION),
            ledger_phase="completed",
            ledger_attempts=2,
            evidence_declared=3,
            evidence_passed=2,
        )
        assert canonical_progress_bytes(vec) == (
            b'{"checkpoints":1,"diffs_captured":0,"evidence_declared":3,'
            b'"evidence_passed":2,"gate_attempts":1,"ledger_attempts":2,'
            b'"ledger_phase":"completed","schema_version":1,"task_id":"task-golden",'
            b'"terminal":true}'
        )


class TestNonAssertable:
    def test_posting_artifacts_does_not_move_progress(self) -> None:
        # Fifty artifact_posted rows and zero checkpoints => unchanged vector.
        baseline = fold_progress(task_id="t1", journal_rows=[])
        spammed = fold_progress(task_id="t1", journal_rows=_rows(*[EVENT_ARTIFACT_POSTED] * 50))
        assert canonical_progress_bytes(spammed) == canonical_progress_bytes(baseline)
        assert spammed.earned_steps == 0

    def test_unknown_events_are_ignored(self) -> None:
        baseline = fold_progress(task_id="t1", journal_rows=[])
        noise = fold_progress(task_id="t1", journal_rows=_rows("some_future_event", "another"))
        assert canonical_progress_bytes(noise) == canonical_progress_bytes(baseline)


class TestMonotone:
    def test_checkpoints_strictly_advance_the_vector(self) -> None:
        prev = fold_progress(task_id="t1", journal_rows=[])
        for n in range(1, 12):
            cur = fold_progress(task_id="t1", journal_rows=_rows(*[EVENT_CHECKPOINT] * n))
            assert cur.checkpoints == n
            assert cur.strictly_advances(prev)
            prev = cur

    def test_dominates_is_reflexive(self) -> None:
        vec = fold_progress(task_id="t1", journal_rows=_rows(EVENT_CHECKPOINT))
        assert vec.dominates(vec)
        assert not vec.strictly_advances(vec)


class TestVectorShape:
    def test_terminal_phase_latches(self) -> None:
        assert fold_progress(task_id="t1", ledger_phase="completed").terminal
        assert fold_progress(task_id="t1", ledger_phase="failed").terminal
        assert not fold_progress(task_id="t1", ledger_phase="started").terminal

    def test_to_wire_carries_hash(self) -> None:
        vec = fold_progress(task_id="t1", journal_rows=_rows(EVENT_CHECKPOINT))
        wire = vec.to_wire()
        assert wire["vector_hash"] == vec.vector_hash()
        assert wire["checkpoints"] == 1
        assert wire["earned_steps"] == 1
        assert isinstance(ProgressVector(task_id="t1"), ProgressVector)
