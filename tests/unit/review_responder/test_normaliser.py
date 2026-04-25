"""Tests for :mod:`bernstein.core.review_responder.normaliser`."""

from __future__ import annotations

import pytest

from bernstein.core.review_responder.normaliser import (
    EventParseError,
    normalise_polling_payload,
    normalise_webhook_payload,
)


def _webhook_envelope() -> dict[str, object]:
    """Build a representative GitHub webhook envelope for tests."""
    return {
        "action": "created",
        "comment": {
            "id": 12345,
            "body": "rename foo to bar",
            "path": "src/util.py",
            "line": 42,
            "start_line": 40,
            "commit_id": "abc",
            "original_commit_id": "abc",
            "diff_hunk": "@@",
            "user": {"login": "alice"},
            "created_at": "2026-04-25T10:00:00Z",
            "updated_at": "2026-04-25T10:00:00Z",
            "in_reply_to_id": None,
        },
        "pull_request": {"number": 314},
        "repository": {"full_name": "chernistry/bernstein"},
    }


def test_normalise_webhook_happy_path() -> None:
    """A well-formed envelope produces every comment field."""
    c = normalise_webhook_payload(_webhook_envelope())
    assert c.comment_id == 12345
    assert c.repo == "chernistry/bernstein"
    assert c.pr_number == 314
    assert c.reviewer == "alice"
    assert (c.line_start, c.line_end) == (40, 42)


def test_normalise_webhook_uses_original_line_when_line_missing() -> None:
    """When ``line`` is missing the parser falls back to ``original_line``."""
    env = _webhook_envelope()
    inner = env["comment"]
    assert isinstance(inner, dict)
    inner.pop("line")
    inner.pop("start_line")
    inner["original_line"] = 99
    c = normalise_webhook_payload(env)
    assert c.line_end == 99
    assert c.line_start == 99


def test_normalise_webhook_missing_comment_block_raises() -> None:
    """No ``comment`` key → :class:`EventParseError`."""
    with pytest.raises(EventParseError):
        normalise_webhook_payload({"action": "created"})


def test_normalise_webhook_missing_repo_raises() -> None:
    """Missing ``repository.full_name`` is rejected."""
    env = _webhook_envelope()
    env.pop("repository")
    with pytest.raises(EventParseError):
        normalise_webhook_payload(env)


def test_normalise_webhook_missing_login_raises() -> None:
    """Missing user login is rejected — we need a reviewer to mention."""
    env = _webhook_envelope()
    inner = env["comment"]
    assert isinstance(inner, dict)
    inner["user"] = {"login": ""}
    with pytest.raises(EventParseError):
        normalise_webhook_payload(env)


def test_normalise_polling_skips_malformed_items() -> None:
    """REST list parser keeps good items and silently drops bad ones."""
    items: list[dict[str, object]] = [
        {
            "id": 1,
            "user": {"login": "u1"},
            "body": "x",
            "path": "f.py",
            "line": 1,
            "commit_id": "c",
            "diff_hunk": "",
            "created_at": "t",
            "updated_at": "t",
        },
        # Missing user
        {
            "id": 2,
            "body": "x",
            "path": "f.py",
            "line": 2,
            "commit_id": "c",
            "created_at": "t",
            "updated_at": "t",
        },
    ]
    out = normalise_polling_payload(repo="o/r", pr_number=7, comments=items)
    assert [c.comment_id for c in out] == [1]
