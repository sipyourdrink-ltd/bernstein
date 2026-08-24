"""Unit tests for :mod:`bernstein.core.integrations.pr_gen`.

These tests deliberately avoid hitting the real filesystem except via
``tmp_path``; the ``gh`` and ``git`` subprocess calls live in the CLI
wrapper, not in the module under test.
"""

from __future__ import annotations

import json
import math
import time
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from bernstein.cli.commands.pr_cmd import pr_cmd
from bernstein.core.integrations.pr_gen import (
    CostBreakdown,
    GateResult,
    SessionSummary,
    _shape_outcome,
    build_pr_body,
    build_pr_title,
    load_session_summary,
)


def _fixture_summary(**overrides: object) -> SessionSummary:
    """Build a :class:`SessionSummary` with sensible defaults for tests."""
    base: dict[str, object] = {
        "session_id": "abcdef1234567890",
        "goal": "Add JWT authentication with refresh tokens",
        "branch": "agent/abcdef12",
        "base_branch": "main",
        "primary_role": "engineer",
        "diff_stat": " src/auth.py | 42 +++++\n 1 file changed, 42 insertions(+)",
        "gates": (
            GateResult(name="lint", passed=True, detail="ruff: 0 findings"),
            GateResult(name="types", passed=True),
            GateResult(name="tests", passed=False, detail="1 failing"),
        ),
        "cost": CostBreakdown(
            total_usd=1.2345,
            total_tokens=123_456,
            by_role={"manager": 0.20, "engineer": 1.00, "janitor": 0.03},
        ),
    }
    base.update(overrides)
    return SessionSummary(**base)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _shape_outcome acronym casing
# ---------------------------------------------------------------------------


def test_shape_outcome_preserves_leading_acronym() -> None:
    assert _shape_outcome("MCP server advertises no repo") == "MCP server advertises no repo"
    assert _shape_outcome("CLI tool for X") == "CLI tool for X"
    assert _shape_outcome("HMAC signing for webhooks") == "HMAC signing for webhooks"


def test_shape_outcome_lowercases_sentence_start() -> None:
    assert _shape_outcome("Fix broken login") == "fix broken login"
    assert _shape_outcome("Add JWT auth") == "add JWT auth"


# ---------------------------------------------------------------------------
# Title
# ---------------------------------------------------------------------------


def test_title_is_capped_and_outcome_shaped() -> None:
    goal = (
        "Add JWT authentication with refresh tokens and secure cookie storage "
        "plus revocation list and audit logging end to end"
    )
    title = build_pr_title(goal, role="engineer")
    assert len(title) <= 70
    assert title.startswith("feat:"), title
    # Outcome-shaped: no trailing period and cleaned whitespace.
    assert "  " not in title
    assert not title.endswith(".")


def test_title_uses_fix_prefix_for_bug_language() -> None:
    title = build_pr_title("Fix broken login flow on mobile", role="engineer")
    assert title.startswith("fix:"), title
    assert "broken login flow" in title


def test_title_respects_existing_conventional_prefix() -> None:
    title = build_pr_title("docs: document the new auth flow", role=None)
    # The "docs" prefix must be detected and not double-stamped.
    assert title.startswith("docs: ")
    assert title.count("docs:") == 1


# ---------------------------------------------------------------------------
# Title with changes_summary
# ---------------------------------------------------------------------------


def test_title_uses_changes_summary_when_provided() -> None:
    changes_summary = "- Add JWT auth: wired refresh tokens\n- Fix login: patched redirect"
    title = build_pr_title("Add JWT authentication with refresh tokens", role="engineer", changes_summary=changes_summary)
    # Outcome derived from changes_summary first line (task title before ": ")
    assert title.startswith("feat: add JWT auth")
    # Conventional type still from goal/role/labels (not changes_summary)
    assert "feat:" in title


def test_title_changes_summary_does_not_affect_classify() -> None:
    # Bug-labelled issue should still get fix: prefix even if changes_summary suggests feat
    changes_summary = "- Add new feature: implemented shiny thing"
    title = build_pr_title("Some goal", role="engineer", labels=("bug",), changes_summary=changes_summary)
    assert title.startswith("fix:")
    # Outcome from changes_summary
    assert "add new feature" in title


def test_title_falls_back_to_goal_when_changes_summary_empty() -> None:
    title = build_pr_title("Add JWT authentication", role="engineer", changes_summary="")
    assert "add JWT authentication" in title


# ---------------------------------------------------------------------------
# Body with changes_summary
# ---------------------------------------------------------------------------


def test_body_uses_changes_summary_for_change_section() -> None:
    summary = _fixture_summary(
        changes_summary="- Add JWT auth: wired refresh tokens\n- Fix login: patched redirect"
    )
    body = build_pr_body(summary)
    assert "## Change" in body
    assert "Add JWT auth: wired refresh tokens" in body
    assert "Fix login: patched redirect" in body


def test_body_falls_back_to_diff_stat_when_changes_summary_empty() -> None:
    summary = _fixture_summary(changes_summary="")
    body = build_pr_body(summary)
    assert "## Change" in body
    assert "src/auth.py" in body  # from diff_stat in code fence


def test_body_problem_is_single_line_from_goal() -> None:
    summary = _fixture_summary(
        goal="Add JWT authentication with refresh tokens. Also secure cookies and audit logs."
    )
    body = build_pr_body(summary)
    assert "## Problem" in body
    # Only first sentence, no trailing period in the line (since it's the first part)
    assert "Add JWT authentication with refresh tokens" in body
    assert "Also secure cookies" not in body


def test_body_problem_extracts_issue_title_from_goal() -> None:
    summary = _fixture_summary(
        goal="Resolve GitHub issue #42: Fix broken login on mobile"
    )
    body = build_pr_body(summary)
    assert "## Problem" in body
    assert "Fix broken login on mobile" in body
    assert "Resolve GitHub issue" not in body


# ---------------------------------------------------------------------------
# load_session_summary
# ---------------------------------------------------------------------------


def test_load_session_summary_picks_newest_wrapup(tmp_path: Path) -> None:
    sessions = tmp_path / ".sdd" / "sessions"
    sessions.mkdir(parents=True)

    older = sessions / "1000-older-wrapup.json"
    older.write_text(
        json.dumps(
            {
                "timestamp": 1000.0,
                "session_id": "older",
                "goal": "Older goal",
                "git_diff_stat": "old.py | 1 +",
            },
        ),
        encoding="utf-8",
    )

    newer = sessions / "2000-newer-wrapup.json"
    newer.write_text(
        json.dumps(
            {
                "timestamp": 2000.0,
                "session_id": "newer",
                "goal": "Newer goal",
                "git_diff_stat": "new.py | 2 +",
                "gates": [
                    {"name": "lint", "passed": True, "detail": "clean"},
                    {"name": "tests", "passed": False, "detail": "2 failing"},
                ],
                "cost": {
                    "total_usd": 0.42,
                    "total_tokens": 4200,
                    "by_role": {"engineer": 0.42},
                },
            },
        ),
        encoding="utf-8",
    )

    # Force the mtimes so the newer file sorts first regardless of test
    # execution speed.
    older_mtime = time.time() - 3600
    newer_mtime = time.time()
    import os

    os.utime(older, (older_mtime, older_mtime))
    os.utime(newer, (newer_mtime, newer_mtime))

    summary = load_session_summary(None, workdir=tmp_path)

    assert summary.session_id == "newer"
    assert summary.goal == "Newer goal"
    assert summary.diff_stat == "new.py | 2 +"
    assert len(summary.gates) == 2
    assert math.isclose(summary.cost.total_usd, 0.42)
    assert summary.cost.by_role == {"engineer": 0.42}


def test_load_session_summary_reads_changes_summary(tmp_path: Path) -> None:
    sessions = tmp_path / ".sdd" / "sessions"
    sessions.mkdir(parents=True)

    wrapup = sessions / "1000-wrapup.json"
    wrapup.write_text(
        json.dumps(
            {
                "timestamp": 1000.0,
                "session_id": "abc",
                "goal": "Add JWT auth",
                "changes_summary": "- Add JWT auth: wired refresh tokens\n- Fix login: patched redirect",
            },
        ),
        encoding="utf-8",
    )

    summary = load_session_summary(None, workdir=tmp_path)

    assert summary.changes_summary == (
        "- Add JWT auth: wired refresh tokens\n- Fix login: patched redirect"
    )


def test_load_session_summary_falls_back_to_live_session(tmp_path: Path) -> None:
    """With no wrap-up files, the live ``session.json`` should drive the summary."""
    runtime = tmp_path / ".sdd" / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "session.json").write_text(
        json.dumps(
            {
                "saved_at": 123.0,
                "run_id": "run-xyz",
                "goal": "Fallback goal",
                "cost_spent": 0.05,
            },
        ),
        encoding="utf-8",
    )

    summary = load_session_summary(None, workdir=tmp_path)

    assert summary.goal == "Fallback goal"
    assert summary.session_id == "run-xyz"
    assert math.isclose(summary.cost.total_usd, 0.05)


# ---------------------------------------------------------------------------
# CLI dry-run behaviour
# ---------------------------------------------------------------------------


def test_dry_run_does_not_invoke_gh(tmp_path: Path) -> None:
    """``--dry-run`` must return before touching git push or gh."""
    runner = CliRunner()

    fake_summary = _fixture_summary()

    with (
        patch(
            "bernstein.cli.commands.pr_cmd.load_session_summary",
            return_value=fake_summary,
        ),
        patch(
            "bernstein.cli.commands.pr_cmd._enrich_summary_with_git",
            side_effect=lambda s, _cwd: s,
        ),
        patch("bernstein.cli.commands.pr_cmd._push_branch") as push_mock,
        patch("bernstein.cli.commands.pr_cmd._gh_pr_create") as gh_mock,
        patch("bernstein.cli.commands.pr_cmd.shutil.which", return_value="/usr/bin/gh"),
    ):
        result = runner.invoke(pr_cmd, ["--dry-run"])

    assert result.exit_code == 0, result.output
    assert "Title: feat:" in result.output
    assert "## Problem" in result.output
    push_mock.assert_not_called()
    gh_mock.assert_not_called()
