"""Tests for orphaned-task recovery in agent_lifecycle."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bernstein.core.agent_reaping import _has_git_commits_on_branch, handle_orphaned_task
from bernstein.core.cascade import CascadeDecision, CascadeExhausted
from bernstein.core.models import AgentSession, Complexity, ModelConfig, Scope, Task, TaskStatus, TaskType

from bernstein.core.agents.agent_lifecycle import _probe_fast_exit


def _make_task(task_id: str = "T-1") -> Task:
    return Task(
        id=task_id,
        title="Implement feature",
        description="Write the code",
        role="backend",
        status=TaskStatus.OPEN,
        scope=Scope.MEDIUM,
        complexity=Complexity.MEDIUM,
        task_type=TaskType.STANDARD,
    )


def _ok_response() -> MagicMock:
    response = MagicMock()
    response.raise_for_status.return_value = None
    return response


def _make_orch(tmp_path, cascade_result) -> SimpleNamespace:  # type: ignore[no-untyped-def]
    tracker = MagicMock()
    tracker.detect_failure_type.return_value = "rate_limit"
    tracker.throttle_summary.return_value = {"claude": {"until": 999}}
    tracker.is_throttled.side_effect = lambda provider: provider == "claude"

    orch = SimpleNamespace()
    orch._config = SimpleNamespace(server_url="http://server")
    orch._client = MagicMock()
    orch._client.patch.return_value = _ok_response()
    orch._client.post.return_value = _ok_response()
    orch._workdir = tmp_path
    orch._rate_limit_tracker = tracker
    orch._router = None
    orch._cascade_manager = MagicMock()
    orch._cascade_manager.find_fallback.return_value = cascade_result
    orch._retried_task_ids = set()
    orch._record_provider_health = MagicMock()
    orch._evolution = None
    orch._wal_writer = None
    return orch


def test_handle_orphaned_task_force_claims_rate_limited_task_with_fallback(tmp_path) -> None:  # type: ignore[no-untyped-def]
    task = _make_task()
    session = AgentSession(
        id="sess-1",
        role="backend",
        provider="claude",
        model_config=ModelConfig("sonnet", "high"),
        task_ids=[task.id],
    )
    orch = _make_orch(
        tmp_path,
        CascadeDecision(
            original_provider="claude",
            fallback_provider="codex",
            fallback_model="gpt-5.4-mini",
            reason="rate limit",
            capability_met=True,
            budget_ok=True,
        ),
    )

    with patch("bernstein.core.agents.agent_lifecycle.retry_or_fail_task") as retry_or_fail_task:
        handle_orphaned_task(orch, task.id, session, {"open": [task], "claimed": [], "in_progress": [], "done": []})

    orch._client.patch.assert_called_once_with(
        "http://server/tasks/T-1",
        json={"model": "gpt-5.4-mini"},
    )
    orch._client.post.assert_called_once_with("http://server/tasks/T-1/force-claim")
    retry_or_fail_task.assert_not_called()
    orch._record_provider_health.assert_called_once_with(session, success=False)


def test_handle_orphaned_task_force_claims_rate_limited_task_without_fallback(tmp_path) -> None:  # type: ignore[no-untyped-def]
    task = _make_task()
    session = AgentSession(
        id="sess-1",
        role="backend",
        provider="claude",
        model_config=ModelConfig("sonnet", "high"),
        task_ids=[task.id],
    )
    orch = _make_orch(
        tmp_path,
        CascadeExhausted(excluded_providers=frozenset({"claude"}), reason="all alternates throttled"),
    )

    with patch("bernstein.core.agents.agent_lifecycle.retry_or_fail_task") as retry_or_fail_task:
        handle_orphaned_task(orch, task.id, session, {"open": [task], "claimed": [], "in_progress": [], "done": []})

    orch._client.patch.assert_not_called()
    orch._client.post.assert_called_once_with("http://server/tasks/T-1/force-claim")
    retry_or_fail_task.assert_not_called()


def _make_orch_no_ratelimit(tmp_path: Path) -> SimpleNamespace:  # type: ignore[no-untyped-def]
    """Create a minimal orch mock without rate-limit tracking.

    This lets the code fall through to the no-completion-signals branch.
    """
    orch = SimpleNamespace()
    orch._config = SimpleNamespace(
        server_url="http://server",
        recovery="restart",
        max_crash_retries=3,
        max_task_retries=3,
    )
    orch._client = MagicMock()
    orch._client.post.return_value = _ok_response()
    orch._workdir = tmp_path
    orch._rate_limit_tracker = None
    orch._crash_counts = {}
    orch._retried_task_ids = set()  # type: ignore[var-annotated]
    orch._record_provider_health = MagicMock()
    orch._evolution = None
    orch._wal_writer = None
    orch._spawner = MagicMock()
    orch._spawner.get_worktree_path.return_value = None
    return orch


# ---------------------------------------------------------------------------
# Git commit detection
# ---------------------------------------------------------------------------


def test_has_git_commits_on_branch_returns_true_when_commits_exist(tmp_path: Path) -> None:
    """_has_git_commits_on_branch returns True when subprocess reports commits."""
    with patch("bernstein.core.agents.agent_lifecycle.subprocess") as mock_subprocess:
        mock_result = MagicMock()
        mock_result.stdout = "abc1234 Add feature\ndef5678 Fix tests\n"
        mock_subprocess.run.return_value = mock_result
        mock_subprocess.TimeoutExpired = TimeoutError
        mock_subprocess.SubprocessError = Exception

        assert _has_git_commits_on_branch(tmp_path) is True
        mock_subprocess.run.assert_called_once_with(
            ["git", "log", "--oneline", "main..HEAD"],
            cwd=str(tmp_path),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )


def test_has_git_commits_on_branch_returns_false_when_no_commits(tmp_path: Path) -> None:
    """_has_git_commits_on_branch returns False when stdout is empty."""
    with patch("bernstein.core.agents.agent_lifecycle.subprocess") as mock_subprocess:
        mock_result = MagicMock()
        mock_result.stdout = ""
        mock_subprocess.run.return_value = mock_result
        mock_subprocess.TimeoutExpired = TimeoutError
        mock_subprocess.SubprocessError = Exception

        assert _has_git_commits_on_branch(tmp_path) is False


def test_has_git_commits_on_branch_returns_false_on_error(tmp_path: Path) -> None:
    """_has_git_commits_on_branch returns False when git command fails."""
    with patch("bernstein.core.agents.agent_lifecycle.subprocess") as mock_subprocess:
        mock_subprocess.run.side_effect = OSError("git not found")
        mock_subprocess.TimeoutExpired = TimeoutError
        mock_subprocess.SubprocessError = Exception

        assert _has_git_commits_on_branch(tmp_path) is False


# ---------------------------------------------------------------------------
# Orphaned task: git commits trigger completion
# ---------------------------------------------------------------------------


def test_orphaned_task_completes_on_git_commits(tmp_path: Path) -> None:
    """Task is auto-completed when agent made git commits on its branch."""
    task = _make_task()
    task.status = TaskStatus.CLAIMED
    session = AgentSession(
        id="sess-git",
        role="backend",
        provider="claude",
        model_config=ModelConfig("sonnet", "high"),
        task_ids=[task.id],
        exit_code=1,  # Non-zero exit, but has commits
    )
    orch = _make_orch_no_ratelimit(tmp_path)
    orch._spawner.get_worktree_path.return_value = tmp_path / "worktree"

    with (
        patch("bernstein.core.agents.agent_lifecycle.collect_completion_data", return_value={"files_modified": []}),
        patch("bernstein.core.agents.agent_lifecycle._has_git_commits_on_branch", return_value=True),
        patch("bernstein.core.agents.agent_lifecycle.complete_task") as mock_complete,
        patch("bernstein.core.agents.agent_lifecycle.retry_or_fail_task") as mock_retry,
    ):
        handle_orphaned_task(orch, task.id, session, {"claimed": [task], "open": [], "in_progress": [], "done": []})

    mock_complete.assert_called_once_with(
        orch._client,
        "http://server",
        task.id,
        f"Auto-completed: agent {session.id} made git commits on branch (no signals to verify)",
    )
    mock_retry.assert_not_called()


# ---------------------------------------------------------------------------
# Orphaned task: clean exit (exit code 0) triggers completion
# ---------------------------------------------------------------------------


def test_orphaned_task_completes_on_long_lived_clean_exit(tmp_path: Path) -> None:
    """A long-lived agent that exits cleanly with an empty diff is a genuine
    "no changes needed" completion and is auto-completed.

    The distinction from a suspicious fast exit is runtime: an agent that ran
    well past the fast-exit threshold before exiting 0 had time to do real
    work and decide nothing was needed.
    """
    import time as _time

    task = _make_task()
    task.status = TaskStatus.CLAIMED
    session = AgentSession(
        id="sess-clean",
        role="backend",
        provider="claude",
        model_config=ModelConfig("sonnet", "high"),
        task_ids=[task.id],
        exit_code=0,  # Clean exit
        spawn_ts=_time.time() - 300.0,  # long-lived -> not a suspicious fast exit
    )
    orch = _make_orch_no_ratelimit(tmp_path)

    with (
        patch("bernstein.core.agents.agent_lifecycle.collect_completion_data", return_value={"files_modified": []}),
        patch("bernstein.core.agents.agent_lifecycle._has_git_commits_on_branch", return_value=False),
        patch("bernstein.core.agents.agent_lifecycle.complete_task") as mock_complete,
        patch("bernstein.core.agents.agent_lifecycle.retry_or_fail_task") as mock_retry,
    ):
        handle_orphaned_task(orch, task.id, session, {"claimed": [task], "open": [], "in_progress": [], "done": []})

    mock_complete.assert_called_once_with(
        orch._client,
        "http://server",
        task.id,
        f"Auto-completed (no changes needed): agent {session.id} "
        f"exited cleanly with empty diff (exit code 0, no signals to verify)",
    )
    mock_retry.assert_not_called()


def test_orphaned_task_fails_on_suspicious_fast_clean_exit(tmp_path: Path) -> None:
    """A short-lived clean exit with an empty diff must NOT be marked done.

    Regression for #2810 / #2806: a fast clean exit (below the fast-exit
    threshold) with no files, no commits, and no completion signals is a
    defect signal, not a real completion. It must route to fail/unverified
    so the run surfaces UNHEALTHY instead of self-declaring healthy with a
    deliverable that exists in no ref.
    """
    task = _make_task()
    task.status = TaskStatus.CLAIMED
    session = AgentSession(
        id="sess-fast",
        role="backend",
        provider="claude",
        model_config=ModelConfig("sonnet", "high"),
        task_ids=[task.id],
        exit_code=0,  # Clean exit, but fresh spawn_ts -> suspicious fast exit
    )
    orch = _make_orch_no_ratelimit(tmp_path)

    with (
        patch("bernstein.core.agents.agent_lifecycle.collect_completion_data", return_value={"files_modified": []}),
        patch("bernstein.core.agents.agent_lifecycle._has_git_commits_on_branch", return_value=False),
        patch("bernstein.core.agents.agent_lifecycle.complete_task") as mock_complete,
        patch("bernstein.core.agents.agent_lifecycle.retry_or_fail_task") as mock_retry,
    ):
        handle_orphaned_task(orch, task.id, session, {"claimed": [task], "open": [], "in_progress": [], "done": []})

    mock_complete.assert_not_called()
    mock_retry.assert_called_once()


# ---------------------------------------------------------------------------
# Orphaned task: non-zero exit + no commits + no files = retry/fail
# ---------------------------------------------------------------------------


def test_orphaned_task_fails_when_no_signals_no_files_no_commits_nonzero_exit(tmp_path: Path) -> None:
    """Task is retried/failed when agent produced no output and exited non-zero."""
    task = _make_task()
    task.status = TaskStatus.CLAIMED
    session = AgentSession(
        id="sess-fail",
        role="backend",
        provider="claude",
        model_config=ModelConfig("sonnet", "high"),
        task_ids=[task.id],
        exit_code=1,  # Non-zero exit
    )
    orch = _make_orch_no_ratelimit(tmp_path)

    with (
        patch("bernstein.core.agents.agent_lifecycle.collect_completion_data", return_value={"files_modified": []}),
        patch("bernstein.core.agents.agent_lifecycle._has_git_commits_on_branch", return_value=False),
        patch("bernstein.core.agents.agent_lifecycle.complete_task") as mock_complete,
        patch("bernstein.core.agents.agent_lifecycle.retry_or_fail_task") as mock_retry,
    ):
        handle_orphaned_task(orch, task.id, session, {"claimed": [task], "open": [], "in_progress": [], "done": []})

    mock_complete.assert_not_called()
    mock_retry.assert_called_once()


# ---------------------------------------------------------------------------
# Orphaned task: files modified still takes priority over git/exit checks
# ---------------------------------------------------------------------------


def test_orphaned_task_files_modified_takes_priority(tmp_path: Path) -> None:
    """Files-modified check takes priority over git commits and exit code."""
    task = _make_task()
    task.status = TaskStatus.CLAIMED
    session = AgentSession(
        id="sess-files",
        role="backend",
        provider="claude",
        model_config=ModelConfig("sonnet", "high"),
        task_ids=[task.id],
        exit_code=0,
    )
    orch = _make_orch_no_ratelimit(tmp_path)
    orch._spawner.get_worktree_path.return_value = tmp_path / "worktree"

    with (
        patch(
            "bernstein.core.agents.agent_lifecycle.collect_completion_data",
            return_value={"files_modified": ["src/foo.py"]},
        ),
        patch("bernstein.core.agents.agent_lifecycle._has_git_commits_on_branch", return_value=True),
        patch("bernstein.core.agents.agent_lifecycle.complete_task") as mock_complete,
        patch("bernstein.core.agents.agent_lifecycle.retry_or_fail_task") as mock_retry,
    ):
        handle_orphaned_task(orch, task.id, session, {"claimed": [task], "open": [], "in_progress": [], "done": []})

    # Should complete with "files modified" message, not git-commits message
    mock_complete.assert_called_once()
    call_args = mock_complete.call_args
    assert "modified 1 files" in call_args[0][3]
    mock_retry.assert_not_called()


# ---------------------------------------------------------------------------
# Defect 8 (D2 claude leg, attempt4-meridian-fixed FAIL-NOTE): dead-agent
# misjudgment on double-forked runners. A tracked launcher PID can exit in
# ~3s while the real worker keeps running untracked; the death judgment must
# not fire while a fresher liveness signal (heartbeat file / runner log /
# worktree git activity) says otherwise.
# ---------------------------------------------------------------------------


def test_orphaned_task_not_judged_dead_when_heartbeat_fresh_despite_dead_pid(tmp_path: Path, caplog) -> None:  # type: ignore[no-untyped-def]
    """Regression for defect 8: fresh heartbeat file must block the death verdict.

    Ground truth: manager-48832613 was declared dead ("PID 77, 3s runtime ...
    died without output") after ~109s of real work because the tracked
    launcher PID had already exited (double-fork). A fresh heartbeat file
    is exactly the signal that must override a dead-looking/wrong pid.
    """
    import logging

    task = _make_task()
    task.status = TaskStatus.CLAIMED
    session = AgentSession(
        id="sess-doubleforked",
        role="manager",
        provider="claude",
        model_config=ModelConfig("sonnet", "high"),
        task_ids=[task.id],
        pid=77,  # tracked launcher pid -- already exited (double-fork)
        exit_code=1,
        spawn_ts=1000.0,
    )
    orch = _make_orch_no_ratelimit(tmp_path)

    heartbeat_dir = tmp_path / ".sdd" / "runtime" / "heartbeats"
    heartbeat_dir.mkdir(parents=True)
    (heartbeat_dir / f"{session.id}.json").write_text("{}")  # freshly written -- mtime is "now"

    with (
        patch("bernstein.core.agents.agent_lifecycle.collect_completion_data", return_value={"files_modified": []}),
        patch("bernstein.core.agents.agent_lifecycle._has_git_commits_on_branch", return_value=False),
        patch("bernstein.core.agents.agent_lifecycle._is_process_alive", return_value=False),
        patch("bernstein.core.agents.agent_lifecycle.complete_task") as mock_complete,
        patch("bernstein.core.agents.agent_lifecycle.retry_or_fail_task") as mock_retry,
        caplog.at_level(logging.INFO, logger="bernstein.core.agents.agent_lifecycle"),
    ):
        handle_orphaned_task(orch, task.id, session, {"claimed": [task], "open": [], "in_progress": [], "done": []})

    # The agent must NOT be judged dead: no fail/retry and no auto-complete
    # (the task is simply deferred to the next tick).
    mock_retry.assert_not_called()
    mock_complete.assert_not_called()
    assert any("Deferring death judgment" in r.message for r in caplog.records)


def test_orphaned_task_judged_dead_when_all_signals_stale(tmp_path: Path) -> None:
    """Case (b): genuinely dead agent (stale/missing heartbeat, dead pid, no output) IS judged dead."""
    task = _make_task()
    task.status = TaskStatus.CLAIMED
    session = AgentSession(
        id="sess-genuinely-dead",
        role="backend",
        provider="claude",
        model_config=ModelConfig("sonnet", "high"),
        task_ids=[task.id],
        pid=99999999,
        exit_code=1,
        spawn_ts=1000.0,
    )
    orch = _make_orch_no_ratelimit(tmp_path)
    # No heartbeat/log/git files created at all -- every signal is "missing".

    with (
        patch("bernstein.core.agents.agent_lifecycle.collect_completion_data", return_value={"files_modified": []}),
        patch("bernstein.core.agents.agent_lifecycle._has_git_commits_on_branch", return_value=False),
        patch("bernstein.core.agents.agent_lifecycle._is_process_alive", return_value=False),
        patch("bernstein.core.agents.agent_lifecycle.complete_task") as mock_complete,
        patch("bernstein.core.agents.agent_lifecycle.retry_or_fail_task") as mock_retry,
    ):
        handle_orphaned_task(orch, task.id, session, {"claimed": [task], "open": [], "in_progress": [], "done": []})

    mock_complete.assert_not_called()
    mock_retry.assert_called_once()


def test_liveness_judgment_log_line_contains_all_inputs(tmp_path: Path, caplog) -> None:
    """The liveness_judgment log line must carry every input plus the verdict and why.

    A future misjudgment must be diagnosable from this log line alone, per
    house logging rules -- never a truncated payload.
    """
    import logging

    from bernstein.core.agents.agent_lifecycle import _probe_liveness_signals

    orch = SimpleNamespace(_workdir=tmp_path)
    session = AgentSession(
        id="sess-log-check",
        role="backend",
        provider="claude",
        model_config=ModelConfig("sonnet", "high"),
        task_ids=["T-1"],
        pid=123,
        spawn_ts=1000.0,
    )

    with (
        patch("bernstein.core.agents.agent_lifecycle._is_process_alive", return_value=False),
        caplog.at_level(logging.INFO, logger="bernstein.core.agents.agent_lifecycle"),
    ):
        result = _probe_liveness_signals(orch, session, now=1500.0)

    assert result["has_fresh_signal"] is False
    records = [r for r in caplog.records if "liveness_judgment" in r.message]
    assert len(records) == 1
    msg = records[0].message
    for expected in (
        "session=sess-log-check",
        "pid=123",
        "pid_alive=False",
        "heartbeat_age_s=",
        "log_age_s=",
        "git_age_s=",
        "grace_s=",
        "verdict=",
        "reason=",
    ):
        assert expected in msg, f"missing {expected!r} in log line: {msg}"


# ---------------------------------------------------------------------------
# _probe_fast_exit: structured diagnostics for a suspiciously fast agent death
# ---------------------------------------------------------------------------


def test_probe_fast_exit_returns_structured_dict_for_fast_death(tmp_path: Path) -> None:
    """A synthetic failing/fast process must yield a structured dict with
    exit code, log tail, and manifest path - not a bare boolean."""
    session = AgentSession(
        id="sess-fast-death",
        role="backend",
        provider="claude",
        model_config=ModelConfig("sonnet", "high"),
        task_ids=["T-1"],
        exit_code=0,
    )
    # Simulate an agent that has been "alive" for only 2 seconds.
    session.spawn_ts = __import__("time").time() - 2.0

    # Log file the process wrote before dying.
    runtime_dir = tmp_path / ".sdd" / "runtime"
    runtime_dir.mkdir(parents=True)
    log_path = runtime_dir / f"{session.id}.log"
    log_lines = [f"line {i}" for i in range(100)]
    log_path.write_text("\n".join(log_lines) + "\n")

    # Preserved runner manifest (written by _preserve_runner_logs before
    # this probe would ever run in production).
    preserved_dir = runtime_dir / "agent_logs" / session.id
    preserved_dir.mkdir(parents=True)
    manifest_path = preserved_dir / f"{session.id}.manifest.json"
    manifest_path.write_text("{}")

    orch = SimpleNamespace()
    orch._workdir = tmp_path
    orch._spawner = MagicMock()
    orch._spawner.get_worktree_path.return_value = None

    result = _probe_fast_exit(orch, session, "T-1")

    assert isinstance(result, dict)
    assert result["suspicious"] is True
    assert result["exit_code"] == 0
    assert result["manifest_path"] == str(manifest_path)
    assert result["log_path"] == str(log_path)
    # Log tail must contain real content, not be swallowed/truncated to nothing.
    assert result["log_tail"], "log_tail must not be empty when a log file exists"
    assert result["log_tail"][-1] == "line 99"
    assert len(result["log_tail"]) <= 60
    assert result["session_id"] == "sess-fast-death"
    assert result["task_id"] == "T-1"


def test_probe_fast_exit_not_suspicious_for_long_lived_agent(tmp_path: Path) -> None:
    """An agent that ran well past the fast-exit threshold is not flagged."""
    session = AgentSession(
        id="sess-normal",
        role="backend",
        provider="claude",
        model_config=ModelConfig("sonnet", "high"),
        task_ids=["T-1"],
        exit_code=0,
    )
    session.spawn_ts = __import__("time").time() - 600.0  # ran for 10 minutes

    orch = SimpleNamespace()
    orch._workdir = tmp_path
    orch._spawner = MagicMock()
    orch._spawner.get_worktree_path.return_value = None

    result = _probe_fast_exit(orch, session, "T-1")

    assert result["suspicious"] is False
    assert result["exit_code"] == 0
    assert result["log_tail"] == []
    assert result["manifest_path"] is None


def test_probe_fast_exit_falls_back_to_worktree_manifest(tmp_path: Path) -> None:
    """When no preserved manifest exists, fall back to the (still-live) worktree copy."""
    session = AgentSession(
        id="sess-wt",
        role="backend",
        provider="claude",
        model_config=ModelConfig("sonnet", "high"),
        task_ids=["T-1"],
        exit_code=0,
    )
    session.spawn_ts = __import__("time").time() - 1.0

    worktree_runtime = tmp_path / "worktree" / ".sdd" / "runtime"
    worktree_runtime.mkdir(parents=True)
    wt_manifest = worktree_runtime / f"{session.id}.manifest.json"
    wt_manifest.write_text("{}")

    orch = SimpleNamespace()
    orch._workdir = tmp_path
    orch._spawner = MagicMock()
    orch._spawner.get_worktree_path.return_value = tmp_path / "worktree"

    result = _probe_fast_exit(orch, session, "T-1")

    assert result["suspicious"] is True
    assert result["manifest_path"] == str(wt_manifest)


# ---------------------------------------------------------------------------
# Cost propagation regression: orphan/auto-complete-after-death path must
# recover real runner cost instead of silently recording $0.0
# (D2 openrouter FAIL-NOTE, 2026-07-03: runner priced the manager's session
# at $0.0038789583 but .sdd/metrics/tasks.jsonl recorded cost_usd: 0.0 and
# retrospective's cost_aggregation fallback logged "K=1 skipped
# (reasons=cost_usd==0), final total_cost=$0.000000").
# ---------------------------------------------------------------------------


def _write_tokens_sidecar(tmp_path: Path, session_id: str, input_tokens: int, output_tokens: int) -> Path:
    """Write a runner cost sidecar (.sdd/runtime/<session_id>.tokens) with one usage record.

    Mirrors the schema written by bernstein.adapters.openai_agents_runner's
    _append_tokens_sidecar(): one JSON object per line, {"ts", "in", "out"}.
    """
    runtime_dir = tmp_path / ".sdd" / "runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    sidecar_path = runtime_dir / f"{session_id}.tokens"
    with sidecar_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"ts": 1783037874.0, "in": input_tokens, "out": output_tokens}) + "\n")
    return sidecar_path


def test_orphaned_task_folds_in_real_runner_cost_from_sidecar(tmp_path: Path) -> None:
    """Reproduces the D2 openrouter FAIL-NOTE: an agent that made real, priced
    LLM calls dies before self-reporting completion. The orphan/auto-complete
    path picks the task up, finds a clean exit + empty diff (exactly the
    manager's outcome in the FAIL-NOTE), and must fold the runner's real cost
    (recovered from the .tokens sidecar) into both:

      1. .sdd/metrics/tasks.jsonl (via EvolutionCoordinator.record_task_completion)
      2. the observability MetricsCollector's in-memory task_metrics entry,
         which is what retrospective.py's cost_aggregation fallback reads.

    Before the fix, both of these hardcoded cost_usd=0.0 on this path.
    """
    from bernstein.core.observability import metric_collector as metric_collector_mod
    from bernstein.core.quality.retrospective import generate_retrospective
    from bernstein.evolution import EvolutionCoordinator

    task = _make_task(task_id="c504cea0632a")
    task.status = TaskStatus.CLAIMED
    task.role = "manager"
    session = AgentSession(
        id="manager-1d73fe26",
        role="manager",
        provider="openai_agents",
        model_config=ModelConfig("deepseek/deepseek-chat", "high"),
        task_ids=[task.id],
        exit_code=0,  # Clean exit, exactly like the FAIL-NOTE manager
    )

    # Real runner cost: 14136 input / 1311 output tokens on deepseek/deepseek-chat
    # (the exact figures quoted in the D2 openrouter FAIL-NOTE).
    _write_tokens_sidecar(tmp_path, session.id, input_tokens=14136, output_tokens=1311)

    # Reset the observability MetricsCollector global singleton so this test
    # doesn't leak state from/into other tests, then register the task the
    # way task_lifecycle.py's spawn path does (collector.start_task()).
    metric_collector_mod._default_collector = None
    obs_collector = metric_collector_mod.get_collector(tmp_path / ".sdd" / "metrics")
    obs_collector.start_task(task.id, role="manager", model="deepseek/deepseek-chat", provider="openai_agents")

    orch = _make_orch_no_ratelimit(tmp_path)
    orch._evolution = EvolutionCoordinator(state_dir=tmp_path / ".sdd")

    with (
        patch("bernstein.core.agents.agent_lifecycle.collect_completion_data", return_value={"files_modified": []}),
        patch("bernstein.core.agents.agent_lifecycle._has_git_commits_on_branch", return_value=False),
        patch("bernstein.core.agents.agent_lifecycle.complete_task"),
        patch("bernstein.core.agents.agent_lifecycle.retry_or_fail_task"),
    ):
        handle_orphaned_task(orch, task.id, session, {"claimed": [task], "open": [], "in_progress": [], "done": []})

    # 1. .sdd/metrics/tasks.jsonl must carry the real, nonzero cost.
    tasks_jsonl = tmp_path / ".sdd" / "metrics" / "tasks.jsonl"
    assert tasks_jsonl.exists(), "tasks.jsonl was never written"
    records = [json.loads(line) for line in tasks_jsonl.read_text().splitlines() if line.strip()]
    task_records = [r for r in records if r["task_id"] == task.id]
    assert task_records, f"no tasks.jsonl record for {task.id}"
    assert task_records[-1]["cost_usd"] > 0.0, f"cost_usd was not folded in: {task_records[-1]}"
    assert task_records[-1]["tokens_prompt"] == 14136
    assert task_records[-1]["tokens_completion"] == 1311

    # 2. Retrospective's cost aggregation (which reads the observability
    #    collector's in-memory task_metrics, not tasks.jsonl) must also see
    #    the real cost, not "$0.000000" / "K=1 skipped (cost_usd==0)".
    generate_retrospective(
        done_tasks=[],
        failed_tasks=[],
        collector=obs_collector,
        runtime_dir=tmp_path / ".sdd" / "runtime",
        run_start_ts=1783037874.0,
    )
    retro_text = (tmp_path / ".sdd" / "runtime" / "retrospective.md").read_text()
    total_cost = obs_collector.get_total_cost()
    fallback_total = sum(tm.cost_usd for tm in obs_collector.task_metrics.values())
    assert total_cost > 0.0 or fallback_total > 0.0, (
        f"retrospective cost aggregation still sees $0 total (total_cost={total_cost}, "
        f"fallback_total={fallback_total}); retrospective.md:\n{retro_text}"
    )

    metric_collector_mod._default_collector = None


def test_reap_wall_clock_timeout_logs_and_continues_when_evolution_raises(tmp_path: Path, caplog) -> None:  # type: ignore[no-untyped-def]
    """Item 25: reap paths must not swallow exceptions from best-effort
    evolution/metrics recording silently. record_agent_lifetime raising
    (e.g. the item-22 TypeError) must produce a logged ERROR with a
    traceback and the session id, and the reap must still complete
    (result.reaped populated, spawner.kill called)."""
    import logging

    from bernstein.core.agents.agent_lifecycle import _reap_wall_clock_timeout

    session = AgentSession(
        id="agent-x1",
        role="backend",
        provider="claude",
        model_config=ModelConfig("claude-3", "high"),
        task_ids=["T-1"],
    )

    orch = SimpleNamespace()
    orch._spawner = MagicMock()
    orch._spawner.get_worktree_path.return_value = "/tmp/worktree-agent-x1"
    orch._signal_mgr = MagicMock()
    orch._evolution = MagicMock()
    orch._evolution.record_agent_lifetime.side_effect = TypeError("boom: unexpected keyword")
    orch._preserved_worktrees = {}

    result = SimpleNamespace(reaped=[])

    with (
        patch("bernstein.core.agents.agent_lifecycle._propagate_abort_to_children"),
        patch("bernstein.core.agents.agent_lifecycle._release_file_ownership"),
        patch("bernstein.core.agents.agent_lifecycle._release_task_to_session"),
        patch("bernstein.core.agents.agent_lifecycle._preserve_runner_logs"),
        patch("bernstein.core.agents.agent_lifecycle.handle_orphaned_task"),
        patch("bernstein.core.agents.agent_lifecycle._save_partial_work", return_value=False),
        caplog.at_level(logging.ERROR),
    ):
        _reap_wall_clock_timeout(orch, session, result, {}, runtime=42.0)

    # Reap must still complete despite the evolution-recording exception.
    assert result.reaped == [session.id]
    orch._spawner.kill.assert_called_once()
    orch._spawner.cleanup_worktree.assert_called_once_with(session.id)

    # The failure must be loud: ERROR level, with the session id, and a
    # traceback (logger.exception attaches exc_info).
    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records, "expected an ERROR log for the swallowed exception, found none"
    assert any(session.id in r.getMessage() for r in error_records), caplog.text
    assert any("record_agent_lifetime" in r.getMessage() for r in error_records), caplog.text
    assert any(r.exc_info for r in error_records), "expected logger.exception to attach a traceback"


def test_reap_heartbeat_timeout_logs_and_continues_when_evolution_raises(tmp_path: Path, caplog) -> None:  # type: ignore[no-untyped-def]
    """Same as above for the heartbeat-timeout reap path."""
    import logging

    from bernstein.core.agents.agent_lifecycle import _reap_heartbeat_timeout

    session = AgentSession(
        id="agent-x2",
        role="backend",
        provider="claude",
        model_config=ModelConfig("claude-3", "high"),
        task_ids=["T-2"],
        spawn_ts=1000.0,
    )

    orch = SimpleNamespace()
    orch._spawner = MagicMock()
    orch._signal_mgr = MagicMock()
    orch._evolution = MagicMock()
    orch._evolution.record_agent_lifetime.side_effect = TypeError("boom: unexpected keyword")
    orch._record_provider_health = MagicMock()
    orch._wal_writer = None
    orch._client = MagicMock()
    orch._config = SimpleNamespace(server_url="http://server", max_task_retries=3)
    orch._retried_task_ids = set()
    orch._workdir = tmp_path

    result = SimpleNamespace(reaped=[])

    with (
        patch("bernstein.core.agents.agent_lifecycle._propagate_abort_to_children"),
        patch("bernstein.core.agents.agent_lifecycle._release_file_ownership"),
        patch("bernstein.core.agents.agent_lifecycle._release_task_to_session"),
        patch("bernstein.core.agents.agent_lifecycle.retry_or_fail_task"),
        caplog.at_level(logging.ERROR),
    ):
        _reap_heartbeat_timeout(orch, session, result, {}, now=1100.0, age=100.0)

    assert result.reaped == [session.id]
    orch._spawner.kill.assert_called_once()

    error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert error_records, "expected an ERROR log for the swallowed exception, found none"
    assert any(session.id in r.getMessage() for r in error_records), caplog.text
    assert any("record_agent_lifetime" in r.getMessage() for r in error_records), caplog.text
    assert any(r.exc_info for r in error_records), "expected logger.exception to attach a traceback"


# ---------------------------------------------------------------------------
# Log-detected fatal failures: fast-fail via retry_or_fail_task
# ---------------------------------------------------------------------------


def _make_orch_fast_fail(tmp_path, failure_type: str) -> SimpleNamespace:  # type: ignore[no-untyped-def]
    """Orch mock whose tracker classifies the dead agent's log as *failure_type*."""
    orch = _make_orch(
        tmp_path,
        CascadeExhausted(excluded_providers=frozenset({"claude"}), reason="all alternates throttled"),
    )
    orch._rate_limit_tracker.detect_failure_type.return_value = failure_type
    orch._config = SimpleNamespace(
        server_url="http://server",
        recovery="restart",
        max_crash_retries=3,
        max_task_retries=3,
    )
    return orch


def test_handle_orphaned_task_max_turns_fast_fails_without_provider_throttle(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """MaxTurnsExceeded (task-claimed-stuck fix): the task is failed/retried
    immediately instead of deferring behind the liveness grace window, and
    the provider is NOT throttled or cascade-reassigned - a turn cap is a
    per-task ceiling, not a provider-health signal."""
    task = _make_task()
    task.status = TaskStatus.CLAIMED
    session = AgentSession(
        id="sess-1",
        role="backend",
        provider="claude",
        model_config=ModelConfig("sonnet", "high"),
        task_ids=[task.id],
    )
    orch = _make_orch_fast_fail(tmp_path, "max_turns")

    with patch("bernstein.core.agents.agent_lifecycle.retry_or_fail_task") as retry_or_fail_task:
        handle_orphaned_task(orch, task.id, session, {"claimed": [task], "open": [], "in_progress": [], "done": []})

    retry_or_fail_task.assert_called_once()
    assert "max_turns" in retry_or_fail_task.call_args.args[1]
    orch._rate_limit_tracker.throttle_provider.assert_not_called()
    orch._cascade_manager.find_fallback.assert_not_called()
    orch._record_provider_health.assert_called_once_with(session, success=False)


def test_handle_orphaned_task_provider_fatal_types_fast_fail_and_throttle(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """timeout/auth_error/api_error used to be detected but never acted on
    (they fell through to the generic no-signals path and its liveness
    deferral); they now fast-fail via retry_or_fail_task while keeping the
    provider throttle they always triggered."""
    for failure_type in ("timeout", "auth_error", "api_error"):
        task = _make_task(task_id=f"T-{failure_type}")
        task.status = TaskStatus.CLAIMED
        session = AgentSession(
            id=f"sess-{failure_type}",
            role="backend",
            provider="claude",
            model_config=ModelConfig("sonnet", "high"),
            task_ids=[task.id],
        )
        orch = _make_orch_fast_fail(tmp_path, failure_type)

        with patch("bernstein.core.agents.agent_lifecycle.retry_or_fail_task") as retry_or_fail_task:
            handle_orphaned_task(orch, task.id, session, {"claimed": [task], "open": [], "in_progress": [], "done": []})

        retry_or_fail_task.assert_called_once()
        assert failure_type in retry_or_fail_task.call_args.args[1]
        orch._rate_limit_tracker.throttle_provider.assert_called_once()
        orch._record_provider_health.assert_called_once_with(session, success=False)
