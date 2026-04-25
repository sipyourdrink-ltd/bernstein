"""Tests for :mod:`bernstein.core.review_responder.dedup`."""

from __future__ import annotations

from pathlib import Path

from bernstein.core.review_responder.dedup import DedupQueue
from bernstein.core.review_responder.models import ReviewComment


def _comment(updated_at: str, comment_id: int = 1) -> ReviewComment:
    """Build a comment with the given ``updated_at`` for dedup tests."""
    return ReviewComment(
        comment_id=comment_id,
        pr_number=1,
        repo="o/r",
        reviewer="x",
        body="b",
        path="p.py",
        line_start=1,
        line_end=1,
        commit_id="c",
        original_commit_id="c",
        diff_hunk="",
        created_at="t",
        updated_at=updated_at,
    )


def test_offer_admits_first_then_suppresses_replay(tmp_path: Path) -> None:
    """Same ``(id, updated_at)`` is admitted once and suppressed on replay."""
    q = DedupQueue(state_path=tmp_path / "dedup.json")
    c = _comment("t1")
    assert q.offer(c) is True
    assert q.is_duplicate(c) is True
    assert q.offer(c) is False


def test_offer_admits_edited_comment(tmp_path: Path) -> None:
    """A newer ``updated_at`` for the same id re-enters the queue."""
    q = DedupQueue(state_path=tmp_path / "dedup.json")
    q.offer(_comment("t1"))
    c2 = _comment("t2")
    assert q.is_duplicate(c2) is False
    assert q.offer(c2) is True


def test_persistence_across_instances(tmp_path: Path) -> None:
    """A second :class:`DedupQueue` reading the same path sees prior records."""
    state = tmp_path / "dedup.json"
    q1 = DedupQueue(state_path=state)
    q1.offer(_comment("t1", comment_id=42))
    q1.mark_outcome(42, outcome="committed", round_id="r-1")

    q2 = DedupQueue(state_path=state)
    assert q2.is_duplicate(_comment("t1", comment_id=42)) is True
    rec = q2.known(42)
    assert rec is not None
    assert rec.outcome == "committed"
    assert rec.round_id == "r-1"


def test_mark_outcome_no_op_for_unknown_id(tmp_path: Path) -> None:
    """Marking an id we never admitted is a silent no-op."""
    q = DedupQueue(state_path=tmp_path / "dedup.json")
    q.mark_outcome(999, outcome="committed")  # must not raise
    assert q.known(999) is None
