"""Shared fixtures for review-responder unit tests."""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from bernstein.core.review_responder.models import (
    ResponderConfig,
    ReviewComment,
    ReviewRound,
)


@pytest.fixture
def sample_comment() -> ReviewComment:
    """Return a representative inline review comment."""
    return ReviewComment(
        comment_id=12345,
        pr_number=314,
        repo="chernistry/bernstein",
        reviewer="alice",
        body="rename foo to bar in src/util.py:42",
        path="src/util.py",
        line_start=42,
        line_end=42,
        commit_id="abc123",
        original_commit_id="abc123",
        diff_hunk="@@ -40,3 +40,3 @@\n def foo():",
        created_at="2026-04-25T10:00:00Z",
        updated_at="2026-04-25T10:00:00Z",
    )


@pytest.fixture
def question_comment() -> ReviewComment:
    """Return a discussion-style comment that should be dismissed."""
    return ReviewComment(
        comment_id=999,
        pr_number=314,
        repo="chernistry/bernstein",
        reviewer="bob",
        body="Could you explain why this uses a tuple here?",
        path="src/util.py",
        line_start=10,
        line_end=10,
        commit_id="abc123",
        original_commit_id="abc123",
        diff_hunk="",
        created_at="2026-04-25T10:00:00Z",
        updated_at="2026-04-25T10:00:00Z",
    )


@pytest.fixture
def stale_comment() -> ReviewComment:
    """Return a comment whose cited line range is no longer in the diff."""
    return ReviewComment(
        comment_id=42,
        pr_number=314,
        repo="chernistry/bernstein",
        reviewer="carol",
        body="Use a list comprehension here.",
        path="src/util.py",
        line_start=999,
        line_end=999,
        commit_id="abc123",
        original_commit_id="abc123",
        diff_hunk="",
        created_at="2026-04-25T10:00:00Z",
        updated_at="2026-04-25T10:00:00Z",
    )


@pytest.fixture
def default_config() -> ResponderConfig:
    """Return a quick-cycle :class:`ResponderConfig` for tests."""
    return ResponderConfig(
        repo="chernistry/bernstein",
        quiet_window_s=0.5,
        per_round_cost_cap_usd=1.0,
    )


@pytest.fixture
def temp_dedup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Redirect dedup state to ``tmp_path`` for the duration of the test."""
    state = tmp_path / "dedup.json"
    monkeypatch.chdir(tmp_path)
    yield state


def make_round(*comments: ReviewComment, round_id: str = "rnd-test") -> ReviewRound:
    """Build a :class:`ReviewRound` from one or more comments."""
    if not comments:
        raise ValueError("make_round requires at least one comment")
    return ReviewRound(
        round_id=round_id,
        repo=comments[0].repo,
        pr_number=comments[0].pr_number,
        comments=tuple(comments),
        opened_at=1.0,
        sealed_at=2.0,
    )


class FakeGhRunner:
    """Drop-in replacement for the ``gh`` subprocess runner.

    Records every call and returns canned ``CompletedProcess`` instances
    keyed by the first argument after ``gh``.

    Args:
        responses: Mapping from a substring of ``args[0]`` to a tuple of
            ``(returncode, stdout)``.  Looked up via ``in`` so callers can
            provide partial matches like ``"pulls/314/comments"``.
    """

    def __init__(self, responses: dict[str, tuple[int, str]] | None = None) -> None:
        """Capture canned responses; default to a generic 200 OK."""
        self.responses = responses or {}
        self.calls: list[tuple[list[str], str | None]] = []

    def __call__(
        self,
        args: list[str],
        stdin: str | None = None,
    ) -> subprocess.CompletedProcess[str]:
        """Record the call and return the canned response."""
        self.calls.append((list(args), stdin))
        joined = " ".join(args)
        for needle, (rc, stdout) in self.responses.items():
            if needle in joined:
                return subprocess.CompletedProcess(
                    args=["gh", *args],
                    returncode=rc,
                    stdout=stdout,
                    stderr="",
                )
        return subprocess.CompletedProcess(
            args=["gh", *args],
            returncode=0,
            stdout="{}",
            stderr="",
        )

    def call_args_for(self, substring: str) -> list[tuple[list[str], str | None]]:
        """Return every recorded call whose joined argv contains ``substring``."""
        return [(args, stdin) for args, stdin in self.calls if substring in " ".join(args)]


@pytest.fixture
def fake_gh() -> FakeGhRunner:
    """Return a fresh :class:`FakeGhRunner` for each test."""
    return FakeGhRunner()


@pytest.fixture
def fake_audit(tmp_path: Path) -> Any:
    """Return a real :class:`AuditLog` whose key + dir live in ``tmp_path``."""
    from bernstein.core.security.audit import AuditLog

    return AuditLog(tmp_path / "audit", key=b"test-key-bytes")
