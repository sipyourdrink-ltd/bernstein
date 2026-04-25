"""Tests for :mod:`bernstein.core.review_responder.bundling`."""

from __future__ import annotations

from dataclasses import replace

from bernstein.core.review_responder.bundling import RoundBundler
from bernstein.core.review_responder.models import ResponderConfig, ReviewComment


def _c(comment_id: int, updated_at: str = "t") -> ReviewComment:
    """Build a stub comment for the bundler tests."""
    return ReviewComment(
        comment_id=comment_id,
        pr_number=314,
        repo="o/r",
        reviewer="alice",
        body="b",
        path="p.py",
        line_start=1,
        line_end=1,
        commit_id="c",
        original_commit_id="c",
        diff_hunk="",
        created_at=updated_at,
        updated_at=updated_at,
    )


def test_quiet_window_keeps_bundle_open() -> None:
    """While comments keep arriving inside the quiet window the bundle is held."""
    cfg = ResponderConfig(repo="o/r", quiet_window_s=10.0)
    now = [100.0]
    b = RoundBundler(config=cfg, clock=lambda: now[0])
    b.add(_c(1))
    now[0] += 5
    b.add(_c(2))
    # Quiet window has not elapsed yet.
    assert b.drain(now=now[0] + 5) == []


def test_quiet_window_seals_bundle_into_one_round() -> None:
    """Once the quiet window elapses the bundle becomes one round."""
    cfg = ResponderConfig(repo="o/r", quiet_window_s=5.0)
    now = [100.0]
    b = RoundBundler(config=cfg, clock=lambda: now[0])
    b.add(_c(1))
    b.add(_c(2))
    rounds = b.drain(now=now[0] + 10.0)
    assert len(rounds) == 1
    assert [c.comment_id for c in rounds[0].comments] == [1, 2]


def test_force_drain_seals_immediately() -> None:
    """``force=True`` flushes a bundle even inside the quiet window."""
    cfg = ResponderConfig(repo="o/r", quiet_window_s=120.0)
    b = RoundBundler(config=cfg, clock=lambda: 100.0)
    b.add(_c(1))
    rounds = b.drain(force=True)
    assert len(rounds) == 1


def test_max_comments_per_round_chunks_bundle() -> None:
    """Bundles larger than the cap split into consecutive rounds."""
    cfg = ResponderConfig(repo="o/r", quiet_window_s=1.0, max_comments_per_round=2)
    b = RoundBundler(config=cfg, clock=lambda: 100.0)
    for i in range(5):
        b.add(_c(i + 1))
    rounds = b.drain(now=200.0)
    assert [len(r.comments) for r in rounds] == [2, 2, 1]


def test_distinct_prs_get_distinct_bundles() -> None:
    """Two PRs in flight produce two independent rounds."""
    cfg = ResponderConfig(repo="o/r", quiet_window_s=1.0)
    b = RoundBundler(config=cfg, clock=lambda: 100.0)
    b.add(_c(1))
    b.add(replace(_c(2), pr_number=315))
    rounds = b.drain(now=200.0)
    assert len(rounds) == 2
    assert {r.pr_number for r in rounds} == {314, 315}
