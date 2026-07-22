"""Tests for `bernstein demo` CLI command."""

from __future__ import annotations

import io
from unittest.mock import MagicMock, patch

from click.testing import CliRunner
from rich.console import Console

from bernstein.cli import run_confirm
from bernstein.cli.main import (
    DEMO_TASKS,
    cli,
    detect_available_adapter,
    setup_demo_project,
)


def _status_response(payload: dict) -> MagicMock:
    """Build a fake httpx response returning ``payload`` from ``/status``."""
    resp = MagicMock()
    resp.status_code = 200
    resp.json.return_value = payload
    return resp


# ---------------------------------------------------------------------------
# _fetch_demo_outcome - one source of truth for the task count (issue #2799)
# ---------------------------------------------------------------------------


def test_fetch_demo_outcome_denominator_is_seeded_count():
    """Retries balloon the server list; the outcome still reports 4 as total.

    Regression for issue #2799: failed tasks spawn retry tasks with fresh ids,
    inflating ``/status`` to 12 rows, so a summary that counted the live list
    printed ``0 / 12`` under a banner that promised 4.
    """
    payload = {
        "total_cost_usd": 0.0,
        "tasks": {"count": 12, "items": [{"status": "failed", "title": f"t{i}"} for i in range(12)]},
    }
    with patch.object(run_confirm.httpx, "get", return_value=_status_response(payload)):
        outcome = run_confirm._fetch_demo_outcome("http://127.0.0.1:9999", expected_total=4)

    assert outcome.total == 4
    assert outcome.done == 0
    assert outcome.failed == 4
    assert not outcome.all_fixed


def test_fetch_demo_outcome_all_done():
    """Four done tasks yield done=4, failed=0, all_fixed True."""
    payload = {"total_cost_usd": 0.25, "tasks": [{"status": "done"} for _ in range(4)]}
    with patch.object(run_confirm.httpx, "get", return_value=_status_response(payload)):
        outcome = run_confirm._fetch_demo_outcome("http://x", expected_total=4)

    assert outcome.done == 4
    assert outcome.failed == 0
    assert outcome.total == 4
    assert outcome.cost_usd == 0.25
    assert outcome.all_fixed


def test_fetch_demo_outcome_handles_status_tasks_dict_shape():
    """GET /status returns tasks as {"count", "items"}; parsing must not crash.

    Regression for issue #2075: iterating the dict form yielded its string keys
    and raised ``AttributeError: 'str' object has no attribute 'get'``.
    """
    payload = {
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
    with patch.object(run_confirm.httpx, "get", return_value=_status_response(payload)):
        outcome = run_confirm._fetch_demo_outcome("http://127.0.0.1:9999", expected_total=4)

    assert outcome.done == 1
    assert outcome.cost_usd == 0.5


# ---------------------------------------------------------------------------
# _print_demo_summary - rendering + follow-up hint (issue #2799)
# ---------------------------------------------------------------------------


def _render_summary(outcome, tmp_path) -> str:
    """Render the demo summary to a string buffer and return the output."""
    buf = io.StringIO()
    rec_console = Console(file=buf, force_terminal=False, width=200)
    with patch.object(run_confirm, "console", rec_console):
        run_confirm._print_demo_summary(tmp_path, outcome, elapsed_secs=12.0)
    return buf.getvalue()


def test_demo_summary_hint_uses_real_flag(tmp_path):
    """The follow-up hint must name a flag that exists (``--merkle-only``).

    Regression for issue #2799: the summary suggested ``audit verify --merkle``,
    which errors with "No such option '--merkle'".
    """
    outcome = run_confirm._DemoOutcome(done=4, failed=0, total=4, cost_usd=0.0)
    out = _render_summary(outcome, tmp_path)

    assert "audit verify --merkle-only" in out
    # No bare/broken --merkle: every occurrence must be the --merkle-only flag.
    assert out.count("--merkle") == out.count("--merkle-only")


def test_demo_summary_renders_consistent_count(tmp_path):
    """Bugs-fixed row reports done/total against the seeded denominator."""
    outcome = run_confirm._DemoOutcome(done=1, failed=3, total=4, cost_usd=0.5)
    out = _render_summary(outcome, tmp_path)

    assert "/ 4" in out
    assert "$0.5000" in out


# ---------------------------------------------------------------------------
# demo command - exit code reflects task outcomes (issue #2799)
# ---------------------------------------------------------------------------


def _invoke_demo_with_outcome(outcome, tmp_path):
    """Invoke ``bernstein demo`` in mock mode with the heavy seams stubbed.

    The bootstrap, poll, cleanup and outcome fetch are all patched so the
    command exercises only its own control flow (banner, summary, exit code)
    against a controlled ``outcome``.
    """
    runner = CliRunner()
    with (
        patch.object(run_confirm, "setup_demo_project"),
        patch.object(run_confirm, "_poll_demo_completion"),
        patch.object(run_confirm, "_stop_demo_processes"),
        patch.object(run_confirm, "_fetch_demo_outcome", return_value=outcome),
        patch("bernstein.core.bootstrap.bootstrap_from_goal"),
        patch("tempfile.mkdtemp", return_value=str(tmp_path)),
    ):
        return runner.invoke(cli, ["demo", "--timeout", "1"])


def test_demo_exits_nonzero_when_all_tasks_fail(tmp_path):
    """100% task failure must not report success: exit code is nonzero."""
    outcome = run_confirm._DemoOutcome(done=0, failed=4, total=4, cost_usd=0.0)
    result = _invoke_demo_with_outcome(outcome, tmp_path)
    assert result.exit_code != 0, result.output


def test_demo_exits_nonzero_on_partial_failure(tmp_path):
    """Any failed task exits nonzero, even when some tasks succeeded."""
    outcome = run_confirm._DemoOutcome(done=3, failed=1, total=4, cost_usd=0.0)
    result = _invoke_demo_with_outcome(outcome, tmp_path)
    assert result.exit_code != 0, result.output


def test_demo_exits_zero_when_all_tasks_succeed(tmp_path):
    """Exit code is 0 only when every seeded task reached done."""
    outcome = run_confirm._DemoOutcome(done=4, failed=0, total=4, cost_usd=0.0)
    result = _invoke_demo_with_outcome(outcome, tmp_path)
    assert result.exit_code == 0, result.output


def test_demo_exits_nonzero_when_bootstrap_crashes(tmp_path):
    """A crashed bootstrap must exit nonzero, not be swallowed to exit 0."""
    runner = CliRunner()
    with (
        patch.object(run_confirm, "setup_demo_project"),
        patch.object(run_confirm, "_poll_demo_completion"),
        patch.object(run_confirm, "_stop_demo_processes"),
        patch("bernstein.core.bootstrap.bootstrap_from_goal", side_effect=RuntimeError("boom")),
        patch("tempfile.mkdtemp", return_value=str(tmp_path)),
    ):
        result = runner.invoke(cli, ["demo", "--timeout", "1"])
    assert result.exit_code != 0, result.output


# ---------------------------------------------------------------------------
# _stop_demo_processes - reap the whole process group (issue #2799)
# ---------------------------------------------------------------------------


def test_stop_demo_processes_reaps_process_group(tmp_path):
    """A tracked demo process (session leader) is reaped, its pid file removed.

    Regression for issue #2799: a lone SIGTERM to the tracked leader left the
    orchestrator and its mock-agent children alive after the demo returned.
    """
    import subprocess
    import sys
    import time

    runtime = tmp_path / ".sdd" / "runtime"
    runtime.mkdir(parents=True)
    proc = subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(30)"],
        start_new_session=True,
    )
    (runtime / "server.pid").write_text(str(proc.pid))
    try:
        run_confirm._stop_demo_processes(tmp_path)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and proc.poll() is None:
            time.sleep(0.1)
        assert proc.poll() is not None, "demo process was not reaped"
        assert not (runtime / "server.pid").exists()
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


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


def test_setup_demo_project_initializes_git_repo_with_head(tmp_path):
    """The demo project must be a git repo with a HEAD commit.

    Per-task worktree isolation is always on (orchestrator hardcodes
    use_worktrees=True), and ``git worktree add`` requires a repository with a
    valid HEAD. Without this the mock demo failed every task (issue #2799).
    """
    import subprocess

    setup_demo_project(tmp_path, "mock")

    assert (tmp_path / ".git").is_dir()
    head = subprocess.run(
        ["git", "rev-parse", "--verify", "HEAD"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
    )
    assert head.returncode == 0, head.stderr

    tracked = subprocess.run(
        ["git", "ls-files"],
        cwd=str(tmp_path),
        capture_output=True,
        text=True,
    )
    # The project files are tracked; bernstein runtime state is not, so the
    # per-agent worktrees under .sdd/worktrees/ never pollute git.
    assert "app.py" in tracked.stdout
    assert ".sdd/" not in tracked.stdout


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
