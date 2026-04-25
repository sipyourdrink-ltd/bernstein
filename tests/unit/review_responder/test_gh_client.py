"""Tests for :mod:`bernstein.core.review_responder.gh_client`."""

from __future__ import annotations

import json
import subprocess

from bernstein.core.review_responder.gh_client import GhClient, lines_in_patch


def _proc(rc: int, stdout: str = "") -> subprocess.CompletedProcess[str]:
    """Build a fake :class:`subprocess.CompletedProcess` for the runner stub."""
    return subprocess.CompletedProcess(args=["gh"], returncode=rc, stdout=stdout, stderr="")


def test_lines_in_patch_extracts_added_lines() -> None:
    """The diff parser tracks new-side line numbers across hunks and additions."""
    patch = "@@ -1,3 +1,4 @@\n context\n+added1\n+added2\n context2\n@@ -10,2 +11,3 @@\n more context\n+added3\n"
    lines = lines_in_patch(patch)
    assert {2, 3, 12} <= lines


def test_get_pr_diff_lines_returns_mapping() -> None:
    """The client merges several files into a path → line set mapping."""

    def runner(args: list[str], _stdin: str | None) -> subprocess.CompletedProcess[str]:
        if "pulls/1/files" in " ".join(args):
            return _proc(
                0,
                json.dumps(
                    [
                        {
                            "filename": "a.py",
                            "patch": "@@ -1,1 +1,2 @@\n+x\n+y\n",
                        }
                    ]
                ),
            )
        return _proc(404)

    client = GhClient(runner=runner)
    out = client.get_pr_diff_lines("o/r", 1)
    assert "a.py" in out
    assert {1, 2} <= out["a.py"]


def test_reply_to_comment_invokes_post() -> None:
    """The reply helper POSTs JSON to the inline-comments endpoint."""
    captured: list[tuple[list[str], str | None]] = []

    def runner(args: list[str], stdin: str | None) -> subprocess.CompletedProcess[str]:
        captured.append((args, stdin))
        return _proc(0)

    client = GhClient(runner=runner)
    assert client.reply_to_comment(repo="o/r", pr_number=42, comment_id=10, body="hello") is True
    assert captured, "runner was not invoked"
    args, stdin = captured[0]
    assert "POST" in args
    assert stdin is not None and "in_reply_to" in stdin


def test_patch_resolve_returns_false_on_failure() -> None:
    """A non-zero ``gh`` return code yields False so callers can fall back."""
    client = GhClient(runner=lambda args, stdin: _proc(1))
    assert client.patch_resolve_comment(repo="o/r", comment_id=10) is False
