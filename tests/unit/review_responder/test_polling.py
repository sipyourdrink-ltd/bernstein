"""Tests for :mod:`bernstein.core.review_responder.polling`."""

from __future__ import annotations

import json
import subprocess

from bernstein.core.review_responder.polling import PollingListener


def _gh_response(stdout: str, returncode: int = 0) -> subprocess.CompletedProcess[str]:
    """Build a fake :class:`subprocess.CompletedProcess` with stub stderr."""
    return subprocess.CompletedProcess(
        args=["gh"],
        returncode=returncode,
        stdout=stdout,
        stderr="",
    )


def _fake_runner(payload_by_pr: dict[int, list[dict[str, object]]]):
    """Return a runner that maps ``pr/{n}/comments`` requests to canned data."""

    def runner(args: list[str]) -> subprocess.CompletedProcess[str]:
        joined = " ".join(args)
        for pr, comments in payload_by_pr.items():
            if f"pulls/{pr}/comments" in joined:
                return _gh_response(json.dumps(comments))
        return _gh_response("[]")

    return runner


def _comment(cid: int, updated_at: str, login: str = "alice") -> dict[str, object]:
    """Build a minimal REST-shape comment dict for the polling parser."""
    return {
        "id": cid,
        "body": f"comment {cid}",
        "path": "src/util.py",
        "line": 10,
        "commit_id": "abc",
        "original_commit_id": "abc",
        "diff_hunk": "",
        "user": {"login": login},
        "created_at": updated_at,
        "updated_at": updated_at,
    }


def test_polling_emits_new_comments_only_once() -> None:
    """Comments newer than the high-water mark are emitted exactly once."""
    payload = {314: [_comment(1, "2026-04-25T10:00:00Z")]}
    seen: list[int] = []

    listener = PollingListener(
        repo="o/r",
        pr_numbers=[314],
        on_comment=lambda c: seen.append(c.comment_id),
        gh_runner=_fake_runner(payload),
    )
    assert listener.tick() == 1
    # Re-tick with the same payload — high-water mark suppresses replays.
    assert listener.tick() == 0
    assert seen == [1]


def test_polling_picks_up_edits_via_updated_at() -> None:
    """An edited comment (newer ``updated_at``) re-emits."""
    state = {314: [_comment(1, "2026-04-25T10:00:00Z")]}
    seen: list[int] = []

    listener = PollingListener(
        repo="o/r",
        pr_numbers=[314],
        on_comment=lambda c: seen.append(c.comment_id),
        gh_runner=_fake_runner(state),
    )
    listener.tick()
    state[314] = [_comment(1, "2026-04-25T11:00:00Z")]
    assert listener.tick() == 1
    assert seen == [1, 1]


def test_polling_returns_zero_when_no_prs_configured_and_discovery_fails() -> None:
    """Empty discovery → zero dispatched, no callback invocations."""
    seen: list[int] = []

    def runner(args: list[str]) -> subprocess.CompletedProcess[str]:
        # Always return empty list for both discovery and comments.
        return _gh_response("[]")

    listener = PollingListener(
        repo="o/r",
        pr_numbers=None,
        on_comment=lambda c: seen.append(c.comment_id),
        gh_runner=runner,
    )
    assert listener.tick() == 0
    assert seen == []


def test_polling_reset_high_water_marks() -> None:
    """``reset()`` re-emits previously-seen comments on the next tick."""
    payload = {314: [_comment(1, "2026-04-25T10:00:00Z")]}
    seen: list[int] = []

    listener = PollingListener(
        repo="o/r",
        pr_numbers=[314],
        on_comment=lambda c: seen.append(c.comment_id),
        gh_runner=_fake_runner(payload),
    )
    listener.tick()
    listener.reset()
    assert listener.tick() == 1
    assert seen == [1, 1]
