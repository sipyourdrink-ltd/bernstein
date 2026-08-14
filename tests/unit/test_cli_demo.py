"""Tests for `bernstein demo` CLI command."""

from __future__ import annotations

import io
import subprocess
import sys
import textwrap
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
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


# ---------------------------------------------------------------------------
# demo command - default-branch merge escape hatch (issue #3431)
# ---------------------------------------------------------------------------


def test_demo_opts_into_default_branch_merges_for_the_run(tmp_path, monkeypatch):
    """Demo merges target the throwaway repo's default branch; without the
    escape hatch every merge is refused as ``target-is-default-branch`` and
    the summary can only ever report 0 fixed bugs (issue #3431). The opt-in
    must be live when bootstrap spawns and must not leak past the command.
    """
    import os

    from bernstein.core.agents.spawner_merge import ENV_ALLOW_MERGE_TO_DEFAULT_BRANCH

    monkeypatch.delenv(ENV_ALLOW_MERGE_TO_DEFAULT_BRANCH, raising=False)
    seen: dict[str, str | None] = {}

    def _capture_env(*args, **kwargs):
        seen["at_bootstrap"] = os.environ.get(ENV_ALLOW_MERGE_TO_DEFAULT_BRANCH)

    outcome = run_confirm._DemoOutcome(done=4, failed=0, total=4, cost_usd=0.0)
    runner = CliRunner()
    with (
        patch.object(run_confirm, "setup_demo_project"),
        patch.object(run_confirm, "_poll_demo_completion"),
        patch.object(run_confirm, "_stop_demo_processes"),
        patch.object(run_confirm, "_fetch_demo_outcome", return_value=outcome),
        patch("bernstein.core.bootstrap.bootstrap_from_goal", side_effect=_capture_env),
        patch("tempfile.mkdtemp", return_value=str(tmp_path)),
    ):
        result = runner.invoke(cli, ["demo", "--timeout", "1"])

    assert result.exit_code == 0, result.output
    assert seen["at_bootstrap"] == "1"
    assert ENV_ALLOW_MERGE_TO_DEFAULT_BRANCH not in os.environ


def test_demo_restores_the_operators_prior_merge_policy(tmp_path, monkeypatch):
    """An operator's explicit merge-policy setting must survive a demo run
    unchanged - the demo's opt-in is scoped to the run, not a global flip.
    """
    import os

    from bernstein.core.agents.spawner_merge import ENV_ALLOW_MERGE_TO_DEFAULT_BRANCH

    monkeypatch.setenv(ENV_ALLOW_MERGE_TO_DEFAULT_BRANCH, "0")

    outcome = run_confirm._DemoOutcome(done=4, failed=0, total=4, cost_usd=0.0)
    runner = CliRunner()
    with (
        patch.object(run_confirm, "setup_demo_project"),
        patch.object(run_confirm, "_poll_demo_completion"),
        patch.object(run_confirm, "_stop_demo_processes"),
        patch.object(run_confirm, "_fetch_demo_outcome", return_value=outcome),
        patch("bernstein.core.bootstrap.bootstrap_from_goal"),
        patch("tempfile.mkdtemp", return_value=str(tmp_path)),
    ):
        result = runner.invoke(cli, ["demo", "--timeout", "1"])

    assert result.exit_code == 0, result.output
    assert os.environ[ENV_ALLOW_MERGE_TO_DEFAULT_BRANCH] == "0"


# ---------------------------------------------------------------------------
# _poll_demo_completion - the /status shape and the early exit (issue #3433)
# ---------------------------------------------------------------------------


def test_poll_exits_early_on_the_dict_shaped_status_payload():
    """``/status`` returns tasks as ``{"count": N, "items": [...]}``. The
    poll used to iterate that dict, crash on its string keys inside
    ``_emit_task_events``, and have the crash eaten by ``suppress()`` on
    every tick - so the early-exit condition was unreachable and every
    demo run burned the full timeout with a blank spinner (issue #3433).
    """
    import time as _time

    payload = {
        "tasks": {
            "count": 2,
            "items": [
                {"id": "t1", "title": "a", "role": "backend", "status": "done"},
                {"id": "t2", "title": "b", "role": "qa", "status": "done"},
            ],
        },
    }
    resp = MagicMock(status_code=200)
    resp.json.return_value = payload
    with patch.object(run_confirm.httpx, "get", return_value=resp):
        start = _time.monotonic()
        run_confirm._poll_demo_completion("http://127.0.0.1:1", start + 6.0)
        elapsed = _time.monotonic() - start
    assert elapsed < 2.0, f"poll must exit on the first tick when all tasks are done; took {elapsed:.1f}s"


def test_poll_processing_crashes_are_loud():
    """The suppress covers only the HTTP fetch. A crash while processing
    rows must propagate: silently eating it on every tick is exactly how
    the dead early-exit hid inside a green-looking demo (issue #3433).
    """
    import time as _time

    import pytest as _pytest

    resp = MagicMock(status_code=200)
    resp.json.return_value = {"tasks": {"count": 1, "items": [{"id": "t", "status": "done"}]}}
    with (
        patch.object(run_confirm.httpx, "get", return_value=resp),
        patch.object(run_confirm, "_emit_task_events", side_effect=RuntimeError("boom")),
        _pytest.raises(RuntimeError, match="boom"),
    ):
        run_confirm._poll_demo_completion("http://127.0.0.1:1", _time.monotonic() + 4.0)


def test_poll_keeps_waiting_while_a_failed_task_may_still_be_retried():
    """A failed task is not terminal: the lifecycle schedules a retry with a
    fresh task id after ``retry_delay_s``. Exiting on ``done + failed``
    tears the demo down while the retry that would have fixed the bug is
    still pending (observed live: 3/4 at 21 s instead of 4/4). Only an
    all-done snapshot may end the poll early.
    """
    import time as _time

    payload = {
        "tasks": {
            "count": 2,
            "items": [
                {"id": "t1", "title": "a", "role": "backend", "status": "done"},
                {"id": "t2", "title": "b", "role": "qa", "status": "failed"},
            ],
        },
    }
    resp = MagicMock(status_code=200)
    resp.json.return_value = payload
    with patch.object(run_confirm.httpx, "get", return_value=resp):
        start = _time.monotonic()
        run_confirm._poll_demo_completion("http://127.0.0.1:1", start + 3.0, expected_total=2)
        elapsed = _time.monotonic() - start
    assert elapsed >= 2.9, f"a failed task must keep the poll alive for retries; exited after {elapsed:.1f}s"


def test_poll_exits_once_the_seeded_count_is_done_despite_failed_rows():
    """A retried run can never have all rows done - the failed original
    keeps its status forever while its retry (fresh id) succeeds - so the
    exit must compare done rows against the seeded count, the same
    denominator the summary clamps to. Observed live pre-fix: a retried
    4/4 run still burned the full deadline.
    """
    import time as _time

    payload = {
        "tasks": {
            "count": 3,
            "items": [
                {"id": "t1", "title": "a", "role": "backend", "status": "done"},
                {"id": "t2", "title": "b", "role": "qa", "status": "failed"},
                {"id": "t2r", "title": "b", "role": "qa", "status": "done"},
            ],
        },
    }
    resp = MagicMock(status_code=200)
    resp.json.return_value = payload
    with patch.object(run_confirm.httpx, "get", return_value=resp):
        start = _time.monotonic()
        run_confirm._poll_demo_completion("http://127.0.0.1:1", start + 6.0, expected_total=2)
        elapsed = _time.monotonic() - start
    assert elapsed < 2.0, f"seeded count reached: poll must exit despite the failed original; took {elapsed:.1f}s"


def test_status_rows_unwrap_tolerates_all_known_shapes():
    """One shared unwrap for both /status consumers: the wrapped dict form,
    the bare-list form, and garbage all resolve without raising."""
    rows = [{"id": "t"}]
    assert run_confirm._status_task_rows({"tasks": {"count": 1, "items": rows}}) == rows
    assert run_confirm._status_task_rows({"tasks": rows}) == rows
    assert run_confirm._status_task_rows({"tasks": {"count": 1, "items": "bad"}}) == []
    assert run_confirm._status_task_rows({"tasks": "bad"}) == []
    assert run_confirm._status_task_rows(["not-a-dict"]) == []


def test_poll_counts_done_lineages_not_done_rows():
    """Retry/orphan recovery produces several done rows for one seeded
    lineage (a "6/8 bugs fixed" frame was observed live on a 4-bug demo).
    Counting rows lets duplicates satisfy the seeded total while another
    bug has no successful attempt - the poll must count distinct done
    lineages (finding 3723321829).
    """
    import time as _time

    payload = {
        "tasks": {
            "count": 3,
            "items": [
                {"id": "a", "lineage_id": "a", "title": "bug a", "role": "backend", "status": "done"},
                {"id": "a2", "lineage_id": "a", "title": "bug a", "role": "backend", "status": "done"},
                {"id": "b", "lineage_id": "b", "title": "bug b", "role": "qa", "status": "open"},
            ],
        },
    }
    resp = MagicMock(status_code=200)
    resp.json.return_value = payload
    with patch.object(run_confirm.httpx, "get", return_value=resp):
        start = _time.monotonic()
        run_confirm._poll_demo_completion("http://127.0.0.1:1", start + 3.0, expected_total=2)
        elapsed = _time.monotonic() - start
    assert elapsed >= 2.9, (
        f"two done rows of ONE lineage must not satisfy a seeded total of 2; exited after {elapsed:.1f}s"
    )


def test_fetch_outcome_counts_done_lineages_not_done_rows():
    """The summary's all-fixed verdict must not be reachable by duplicate
    done rows for one lineage while another seeded bug never succeeded
    (finding 3723321829).
    """
    payload = {
        "tasks": {
            "count": 3,
            "items": [
                {"id": "a", "lineage_id": "a", "title": "bug a", "status": "done"},
                {"id": "a2", "lineage_id": "a", "title": "bug a", "status": "done"},
                {"id": "b", "lineage_id": "b", "title": "bug b", "status": "failed"},
            ],
        },
        "total_cost_usd": 0.0,
    }
    resp = MagicMock(status_code=200)
    resp.json.return_value = payload
    with patch.object(run_confirm.httpx, "get", return_value=resp):
        outcome = run_confirm._fetch_demo_outcome("http://127.0.0.1:1", expected_total=2)
    assert outcome.done == 1
    assert outcome.failed == 1
    assert not outcome.all_fixed


# ---------------------------------------------------------------------------
# _stop_demo_processes - wait for finalization marker before reaping (#3627)
# ---------------------------------------------------------------------------


def test_stop_demo_processes_waits_for_finalization_before_reap(tmp_path):
    """Teardown must not kill the orchestrator before finalization completes.

    Fails by construction against the current (no-wait) teardown: the child
    blocks on a .proceed file before writing seal/receipt/.finalized, and
    .proceed is only written after teardown has started. Without the wait,
    the child is SIGTERM'd while blocked, so the completion files are never
    written — regardless of machine speed.
    """
    project_dir = tmp_path / "demo_project"
    runtime_dir = project_dir / ".sdd" / "runtime"
    runtime_dir.mkdir(parents=True)

    child_script = textwrap.dedent(
        f"""
        import sys, time
        from pathlib import Path

        PROJECT_DIR = Path({str(project_dir)!r})

        # Entry marker: finalization has started.
        (PROJECT_DIR / ".sdd" / "runtime" / ".finalizing").touch()

        # Block until the test signals us to proceed. This is the
        # fail-by-construction hinge: the child cannot write the
        # completion files until .proceed appears, and .proceed only
        # appears after teardown has started. If teardown doesn't wait,
        # the child is killed here and the files are never written.
        while not (PROJECT_DIR / ".proceed").exists():
            time.sleep(0.02)

        # Simulate finalization completing
        (PROJECT_DIR / "spine.sealed").write_text("sealed")
        (PROJECT_DIR / "run.receipt").write_text("signed")
        (PROJECT_DIR / ".sdd" / "runtime" / ".finalized").write_text("done")

        # Idle until reaped
        time.sleep(60)
        """
    )

    # start_new_session=True is CRITICAL here, otherwise the child is in
    # the pytest process group and reap_process_group will kill pytest!
    proc = subprocess.Popen(
        [sys.executable, "-c", child_script],
        start_new_session=True,
    )
    # Write the PID file where _stop_demo_processes expects it
    (runtime_dir / "spawner.pid").write_text(str(proc.pid))

    # Wait for child to write the entry marker (proves it's running)
    while not (runtime_dir / ".finalizing").exists():
        if proc.poll() is not None:
            stdout, stderr = proc.communicate()
            pytest.fail(f"child exited before writing .finalizing\\nSTDOUT:\\n{stdout}\\nSTDERR:\\n{stderr}")
        time.sleep(0.01)

    # Call teardown in a thread
    teardown_done = threading.Event()

    def teardown():
        try:
            run_confirm._stop_demo_processes(project_dir)
        finally:
            teardown_done.set()

    t = threading.Thread(target=teardown)
    t.start()

    # Give teardown time to start and (with the fix) begin waiting
    # for .finalized. Then signal the child to proceed.
    time.sleep(0.2)
    (project_dir / ".proceed").touch()

    # Teardown should complete (bounded)
    assert teardown_done.wait(timeout=15.0), "teardown did not complete"

    # Cleanup
    if proc.poll() is None:
        proc.kill()
        proc.wait(timeout=5)
    t.join(timeout=5)

    # These fail on current teardown (child killed before .proceed)
    # and pass after the fix (teardown waited for .finalized).
    assert (runtime_dir / ".finalized").exists(), (
        "finalization did not complete before teardown — teardown killed the orchestrator mid-finalization"
    )
    assert (project_dir / "spine.sealed").exists(), "lineage spine was not sealed"
    assert (project_dir / "run.receipt").exists(), "run receipt was not written"
