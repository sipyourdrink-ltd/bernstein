"""Unit tests for the PR ownership gate."""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.autofix.ownership import (
    PullRequestMetadata,
    decide_ownership,
    extract_session_id,
    render_session_trailer,
    session_id_known,
)

# ---------------------------------------------------------------------------
# Trailer parsing
# ---------------------------------------------------------------------------


def test_extract_session_id_finds_trailer() -> None:
    """The trailer parser handles a body with markdown surrounding it."""
    body = (
        "## Summary\n"
        "- did the work\n\n"
        "---\n"
        "_Generated from Bernstein session `abc12345`._\n\n"
        "bernstein-session-id: abc12345"
    )
    assert extract_session_id(body) == "abc12345"


def test_extract_session_id_handles_quoted_lines() -> None:
    """Trailers reproduced inside markdown quotes still match."""
    body = "> bernstein-session-id: deadbeef"
    assert extract_session_id(body) == "deadbeef"


def test_extract_session_id_returns_none_when_absent() -> None:
    """No trailer means the daemon must skip the PR."""
    assert extract_session_id("just some text") is None


def test_render_trailer_round_trips() -> None:
    """The renderer's output must parse back to the same id."""
    rendered = render_session_trailer("xyz999")
    assert extract_session_id(rendered) == "xyz999"


# ---------------------------------------------------------------------------
# Session lookup
# ---------------------------------------------------------------------------


def test_session_id_known_matches_filename(tmp_path: Path) -> None:
    """A wrap-up file containing the id satisfies the lookup."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    (sessions / "1736000000-abcd1234-wrapup.json").write_text("{}", encoding="utf-8")
    assert session_id_known("abcd1234", sessions) is True


def test_session_id_known_returns_false_for_unknown(tmp_path: Path) -> None:
    """Ids not present on disk are rejected."""
    sessions = tmp_path / "sessions"
    sessions.mkdir()
    assert session_id_known("missing", sessions) is False


def test_session_id_known_handles_missing_dir(tmp_path: Path) -> None:
    """An absent directory is treated as 'no sessions known'."""
    assert session_id_known("anything", tmp_path / "missing") is False


# ---------------------------------------------------------------------------
# Ownership gate
# ---------------------------------------------------------------------------


def _pr(**overrides: object) -> PullRequestMetadata:
    """Build a typed PR with sane defaults for tests."""
    base: dict[str, object] = {
        "repo": "owner/name",
        "number": 142,
        "title": "feat: example",
        "body": "feat: example\n\nbernstein-session-id: abc12345",
        "labels": ("bernstein-autofix",),
        "head_sha": "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef",
        "head_branch": "feat/example",
        "head_repo_full_name": "owner/name",
        "is_fork": False,
    }
    base.update(overrides)
    return PullRequestMetadata(**base)  # type: ignore[arg-type]


def test_eligible_pr_is_claimed() -> None:
    """All gates pass: label present, trailer + known session."""
    decision = decide_ownership(
        _pr(),
        expected_label="bernstein-autofix",
        session_lookup=lambda sid: sid == "abc12345",
    )
    assert decision.eligible is True
    assert decision.session_id == "abc12345"


def test_missing_label_rejects() -> None:
    """Removing the label aborts ownership immediately."""
    decision = decide_ownership(
        _pr(labels=("ready-for-review",)),
        expected_label="bernstein-autofix",
        session_lookup=lambda sid: True,
    )
    assert decision.eligible is False
    assert "label" in decision.reason


def test_missing_trailer_rejects() -> None:
    """A PR without the trailer is skipped."""
    decision = decide_ownership(
        _pr(body="no trailer here"),
        expected_label="bernstein-autofix",
        session_lookup=lambda sid: True,
    )
    assert decision.eligible is False
    assert "trailer" in decision.reason


def test_unknown_session_rejects() -> None:
    """Trailer present but session not in local store → skip."""
    decision = decide_ownership(
        _pr(),
        expected_label="bernstein-autofix",
        session_lookup=lambda sid: False,
    )
    assert decision.eligible is False
    assert decision.session_id == "abc12345"
    assert "not present" in decision.reason or "store" in decision.reason


def test_fork_prs_are_skipped_outright() -> None:
    """Cross-fork PRs are rejected before any other gate runs."""
    decision = decide_ownership(
        _pr(is_fork=True),
        expected_label="bernstein-autofix",
        session_lookup=lambda sid: True,
    )
    assert decision.eligible is False
    assert "fork" in decision.reason


def test_label_check_is_case_insensitive() -> None:
    """Label comparison is case-insensitive (GitHub stores them as-is)."""
    decision = decide_ownership(
        _pr(labels=("Bernstein-AutoFix",)),
        expected_label="bernstein-autofix",
        session_lookup=lambda sid: sid == "abc12345",
    )
    assert decision.eligible is True


@pytest.mark.parametrize("body_value", ["", None])
def test_blank_body_rejects(body_value: str | None) -> None:
    """Blank PR bodies fall through to the missing-trailer branch."""
    decision = decide_ownership(
        _pr(body=body_value or ""),
        expected_label="bernstein-autofix",
        session_lookup=lambda sid: True,
    )
    assert decision.eligible is False
