"""Tests for `bernstein demo` CLI command."""

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

from click.testing import CliRunner
from rich.console import Console

from bernstein.cli.main import (
    DEMO_TASKS,
    cli,
    detect_available_adapter,
    setup_demo_project,
)


def test_demo_summary_handles_status_tasks_dict_shape(tmp_path):
    """GET /status returns tasks as {"count", "items"}; the summary must not crash.

    Regression for issue #2075: iterating the dict form yielded its string keys
    and raised ``AttributeError: 'str' object has no attribute 'get'``.
    """
    from bernstein.cli import run_confirm

    payload = {
        "total": 3,
        "done": 1,
        "failed": 1,
        "total_cost_usd": 0.5,
        "tasks": {
            "count": 3,
            "items": [
                {"status": "done", "title": "a"},
                {"status": "failed", "title": "b"},
                {"status": "open", "title": "c"},
            ],
        },
    }
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = payload

    buf = io.StringIO()
    rec_console = Console(file=buf, force_terminal=False, width=100)
    with (
        patch.object(run_confirm.httpx, "get", return_value=resp),
        patch.object(run_confirm, "console", rec_console),
    ):
        run_confirm._print_demo_summary(tmp_path, "http://127.0.0.1:9999", elapsed_secs=12.0)

    out = buf.getvalue()
    # 1 done out of 3 total, rendered without raising.
    assert "1" in out
    assert "/ 3" in out
    assert "$0.5000" in out


# ---------------------------------------------------------------------------
# detect_available_adapter
# ---------------------------------------------------------------------------


def testdetect_available_adapter_returns_first_found(tmp_path):
    """Returns the name of the first adapter whose CLI is in PATH."""
    with patch("shutil.which", side_effect=lambda cmd: "/usr/bin/" + cmd if cmd == "claude" else None):
        result = detect_available_adapter()
    assert result == "claude"


def testdetect_available_adapter_returns_none_when_nothing_found():
    """Returns None when no supported CLI tool is available."""
    with patch("shutil.which", return_value=None):
        result = detect_available_adapter()
    assert result is None


def testdetect_available_adapter_prefers_claude_over_codex():
    """claude is checked before codex in the discovery order."""

    def _which(cmd: str) -> str | None:
        return "/usr/bin/" + cmd if cmd in {"claude", "codex"} else None

    with patch("shutil.which", side_effect=_which):
        result = detect_available_adapter()
    # claude is first in _ADAPTER_COMMANDS so it should win
    assert result == "claude"


# ---------------------------------------------------------------------------
# setup_demo_project
# ---------------------------------------------------------------------------


def testsetup_demo_project_creates_sdd_dirs(tmp_path):
    """setup_demo_project must create the .sdd/ workspace directories."""
    setup_demo_project(tmp_path, "claude")
    assert (tmp_path / ".sdd" / "backlog" / "open").is_dir()
    assert (tmp_path / ".sdd" / "runtime").is_dir()


def testsetup_demo_project_seeds_three_tasks(tmp_path):
    """Three backlog .md files must exist after project setup."""
    setup_demo_project(tmp_path, "claude")
    backlog_files = list((tmp_path / ".sdd" / "backlog" / "open").glob("*.md"))
    assert len(backlog_files) == len(DEMO_TASKS)


def testsetup_demo_project_task_filenames_match(tmp_path):
    """Backlog filenames must match DEMO_TASKS definitions."""
    setup_demo_project(tmp_path, "claude")
    backlog_dir = tmp_path / ".sdd" / "backlog" / "open"
    for task in DEMO_TASKS:
        assert (backlog_dir / task["filename"]).exists()


def testsetup_demo_project_writes_config(tmp_path):
    """A .sdd/config.yaml with the correct adapter must be written."""
    setup_demo_project(tmp_path, "gemini")
    config_text = (tmp_path / ".sdd" / "config.yaml").read_text()
    assert "gemini" in config_text


def testsetup_demo_project_creates_app_py(tmp_path):
    """app.py should exist in the project root after setup."""
    setup_demo_project(tmp_path, "claude")
    assert (tmp_path / "app.py").exists()


# ---------------------------------------------------------------------------
# demo command - dry-run mode (no real agents spawned)
# ---------------------------------------------------------------------------


def test_demo_dry_run_exits_zero():
    """bernstein demo --dry-run must exit with code 0."""
    runner = CliRunner()
    with patch("bernstein.cli.run_cmd.detect_available_adapter", return_value="claude"):
        result = runner.invoke(cli, ["demo", "--dry-run"])
    assert result.exit_code == 0, result.output


def test_demo_dry_run_shows_task_table():
    """bernstein demo --dry-run must show the task plan table."""
    runner = CliRunner()
    with patch("bernstein.cli.run_cmd.detect_available_adapter", return_value="claude"):
        result = runner.invoke(cli, ["demo", "--dry-run"])
    assert "No agents were spawned" in result.output


def test_demo_dry_run_shows_dry_run_label():
    """bernstein demo --dry-run output must contain '[DRY RUN]'."""
    runner = CliRunner()
    with patch("bernstein.cli.run_cmd.detect_available_adapter", return_value="claude"):
        result = runner.invoke(cli, ["demo", "--dry-run"])
    assert "DRY RUN" in result.output


def test_demo_no_adapter_dry_run_still_works():
    """bernstein demo --dry-run works even without an adapter (just shows plan)."""
    runner = CliRunner()
    with patch("bernstein.cli.run_cmd.detect_available_adapter", return_value=None):
        result = runner.invoke(cli, ["demo", "--dry-run"])
    assert result.exit_code == 0


def test_demo_explicit_adapter_bypasses_detection():
    """--adapter flag must skip auto-detection."""
    runner = CliRunner()
    # No need to patch detect_available_adapter - explicit flag skips it
    with patch("bernstein.cli.run_cmd.detect_available_adapter") as mock_detect:
        result = runner.invoke(cli, ["demo", "--dry-run", "--adapter", "claude"])
    mock_detect.assert_not_called()
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# DEMO_TASKS sanity checks
# ---------------------------------------------------------------------------


def test_demo_tasks_have_required_fields():
    """Every entry in DEMO_TASKS must have 'filename' and 'content'."""
    for task in DEMO_TASKS:
        assert "filename" in task
        assert "content" in task


def test_demo_tasks_filenames_end_with_md():
    """Every demo task filename must end with '.md'."""
    for task in DEMO_TASKS:
        assert task["filename"].endswith(".md"), task["filename"]


def test_demo_tasks_content_includes_role():
    """Every demo task must specify a **Role:** field."""
    for task in DEMO_TASKS:
        assert "**Role:**" in task["content"], task["filename"]
