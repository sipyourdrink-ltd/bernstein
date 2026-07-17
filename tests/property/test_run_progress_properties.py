"""Property tests for the chain-computed progress projection (#2553).

The ticket demands two properties empirically: a synthetic journal with N
checkpoint rows projects a monotonically advancing vector, and posting artifacts
never moves the vector. Hypothesis drives both across random shapes.
"""

from __future__ import annotations

from hypothesis import given
from hypothesis import strategies as st

from bernstein.core.replay.progress import (
    EVENT_ARTIFACT_POSTED,
    EVENT_CHECKPOINT,
    canonical_progress_bytes,
    fold_progress,
)


def _checkpoint_rows(n: int) -> list[dict[str, object]]:
    return [{"event": EVENT_CHECKPOINT, "index": i} for i in range(n)]


@given(st.integers(min_value=0, max_value=200))
def test_more_checkpoints_strictly_advance(n: int) -> None:
    """N and N+1 checkpoint rows project a strictly advancing vector."""
    a = fold_progress(task_id="t", journal_rows=_checkpoint_rows(n))
    b = fold_progress(task_id="t", journal_rows=_checkpoint_rows(n + 1))
    assert a.checkpoints == n
    assert b.checkpoints == n + 1
    assert b.strictly_advances(a)


@given(
    checkpoints=st.integers(min_value=0, max_value=50),
    artifacts=st.integers(min_value=0, max_value=50),
)
def test_artifacts_never_change_the_vector(checkpoints: int, artifacts: int) -> None:
    """Interleaving any number of artifact_posted rows leaves the vector fixed."""
    base = fold_progress(task_id="t", journal_rows=_checkpoint_rows(checkpoints))
    rows: list[dict[str, object]] = _checkpoint_rows(checkpoints)
    rows.extend({"event": EVENT_ARTIFACT_POSTED, "index": 1000 + i} for i in range(artifacts))
    with_artifacts = fold_progress(task_id="t", journal_rows=rows)
    assert canonical_progress_bytes(with_artifacts) == canonical_progress_bytes(base)


@given(st.lists(st.sampled_from([EVENT_CHECKPOINT, EVENT_ARTIFACT_POSTED, "noise"]), max_size=100))
def test_fold_is_deterministic_over_arbitrary_rows(events: list[str]) -> None:
    """The fold is a pure function: two folds of the same rows are byte-equal."""
    rows = [{"event": e, "index": i} for i, e in enumerate(events)]
    a = fold_progress(task_id="t", journal_rows=rows)
    b = fold_progress(task_id="t", journal_rows=rows)
    assert canonical_progress_bytes(a) == canonical_progress_bytes(b)
    assert a.vector_hash() == b.vector_hash()
