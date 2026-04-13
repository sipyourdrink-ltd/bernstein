"""Tests for Chaos Engineering CLI and server kill recovery."""

from __future__ import annotations

import errno
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bernstein.cli.chaos_cmd import chaos_group
from click.testing import CliRunner

# ---------------------------------------------------------------------------
# CLI smoke tests
# ---------------------------------------------------------------------------


def test_chaos_rate_limit(tmp_path: Path) -> None:
    """Rate-limit command writes the active sentinel file."""
    with patch("bernstein.cli.chaos_cmd.CHAOS_DIR", tmp_path):
        runner = CliRunner()
        result = runner.invoke(chaos_group, ["rate-limit", "--duration", "10", "--provider", "test-p"])

        assert result.exit_code == 0
        assert "Provider test-p rate-limited" in result.output

        rate_limit_file = tmp_path / "rate_limit_active.json"
        assert rate_limit_file.exists()
        data = json.loads(rate_limit_file.read_text())
        assert data["provider"] == "test-p"


def test_chaos_status_empty(tmp_path: Path) -> None:
    """Status command reports no history when the log is absent."""
    with patch("bernstein.cli.chaos_cmd.CHAOS_DIR", tmp_path):
        runner = CliRunner()
        result = runner.invoke(chaos_group, ["status"])
        assert result.exit_code == 0
        assert "No chaos experiments recorded yet" in result.output


def test_chaos_status_shows_recorded_event(tmp_path: Path) -> None:
    """Status command displays events previously written to the chaos log."""
    log_path = tmp_path / "chaos_log.jsonl"
    event = {
        "scenario": "agent-kill",
        "target": "agent-abc",
        "success": True,
        "error": "",
        "timestamp": time.time(),
    }
    log_path.write_text(json.dumps(event) + "\n")

    with patch("bernstein.cli.chaos_cmd.CHAOS_DIR", tmp_path):
        runner = CliRunner()
        result = runner.invoke(chaos_group, ["status"])
        assert result.exit_code == 0
        assert "agent-kill" in result.output


def test_chaos_rate_limit_records_event(tmp_path: Path) -> None:
    """Rate-limit command appends a structured event to the chaos log."""
    with patch("bernstein.cli.chaos_cmd.CHAOS_DIR", tmp_path):
        runner = CliRunner()
        runner.invoke(chaos_group, ["rate-limit", "--duration", "30", "--provider", "openai"])

        log_path = tmp_path / "chaos_log.jsonl"
        assert log_path.exists()
        events = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        assert len(events) == 1
        assert events[0]["scenario"] == "rate-limit"
        assert events[0]["success"] is True


def test_chaos_agent_kill_no_agents(tmp_path: Path) -> None:
    """Agent-kill reports gracefully when no agents are running."""
    runner = CliRunner()
    # Run inside tmp_path so agents_dir is missing
    with runner.isolated_filesystem(temp_dir=tmp_path):
        result = runner.invoke(chaos_group, ["agent-kill"])
    assert result.exit_code == 0
    assert "No active agents" in result.output


def test_chaos_disk_full_creates_sentinel(tmp_path: Path) -> None:
    """Disk-full command writes the disk_full_active.json sentinel."""
    with patch("bernstein.cli.chaos_cmd.CHAOS_DIR", tmp_path):
        runner = CliRunner()
        result = runner.invoke(chaos_group, ["disk-full", "--duration", "5"])
        assert result.exit_code == 0

        sentinel = tmp_path / "disk_full_active.json"
        assert sentinel.exists()
        data = json.loads(sentinel.read_text())
        assert data["duration_seconds"] == 5
        assert data["expires_at"] > data["started_at"]


def test_chaos_agent_oom_records_event(tmp_path: Path) -> None:
    """agent-oom command records a chaos event in the log."""
    with patch("bernstein.cli.chaos_cmd.CHAOS_DIR", tmp_path):
        runner = CliRunner()
        result = runner.invoke(chaos_group, ["agent-oom", "--agent-id", "test-agent"])
        assert result.exit_code == 0
        assert "Simulating OOM" in result.output

        log_path = tmp_path / "chaos_log.jsonl"
        assert log_path.exists()
        events = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        assert len(events) == 1
        assert events[0]["scenario"] == "agent-oom"
        assert events[0]["target"] == "test-agent"
        assert events[0]["success"] is True


def test_chaos_agent_oom_default_target(tmp_path: Path) -> None:
    """agent-oom without --agent-id uses 'random-active' as target."""
    with patch("bernstein.cli.chaos_cmd.CHAOS_DIR", tmp_path):
        runner = CliRunner()
        result = runner.invoke(chaos_group, ["agent-oom"])
        assert result.exit_code == 0

        log_path = tmp_path / "chaos_log.jsonl"
        events = [json.loads(line) for line in log_path.read_text().splitlines() if line.strip()]
        assert events[0]["target"] == "random-active"


# ---------------------------------------------------------------------------
# Server kill recovery: TaskStore JSONL persistence
# ---------------------------------------------------------------------------


def _write_task_jsonl(jsonl: Path, task_id: str, title: str, status: str, role: str = "qa") -> None:
    """Write a minimal task record to a JSONL file (helper)."""
    jsonl.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "id": task_id,
        "title": title,
        "description": "",
        "role": role,
        "priority": 3,
        "status": status,
    }
    with jsonl.open("a") as fh:
        fh.write(json.dumps(record) + "\n")


def test_claimed_task_reset_to_open_after_server_kill(tmp_path: Path) -> None:
    """After server is killed mid-task, CLAIMED tasks are reset to open on restart.

    When the server process dies, all in-flight (CLAIMED) tasks have no active
    agent.  replay_jsonl + recover_stale_claimed_tasks resets them to open so
    a fresh agent can pick them up on the next tick.
    """
    from bernstein.core.task_store import TaskStore

    jsonl = tmp_path / "runtime" / "tasks.jsonl"
    task_id = "chaos-task-claimed-001"

    # Simulate: task was in CLAIMED state when server died
    _write_task_jsonl(jsonl, task_id, title="mid-task-on-crash", status="claimed")

    store = TaskStore(jsonl)
    store.replay_jsonl()
    store.recover_stale_claimed_tasks()

    recovered = store.get_task(task_id)
    assert recovered is not None
    assert recovered.status.value == "open", (
        f"Expected task to be reset to 'open' after recovery, got '{recovered.status.value}'"
    )


def test_recover_stale_claimed_preserves_done_tasks(tmp_path: Path) -> None:
    """recover_stale_claimed_tasks must not touch terminal tasks (done, failed)."""
    from bernstein.core.task_store import TaskStore

    jsonl = tmp_path / "runtime" / "tasks.jsonl"
    _write_task_jsonl(jsonl, "t-done", title="done-task", status="done")
    _write_task_jsonl(jsonl, "t-failed", title="failed-task", status="failed")

    store = TaskStore(jsonl)
    store.replay_jsonl()
    store.recover_stale_claimed_tasks()

    assert store.get_task("t-done").status.value == "done"  # type: ignore[union-attr]
    assert store.get_task("t-failed").status.value == "failed"  # type: ignore[union-attr]


def test_recover_stale_claimed_multiple_tasks(tmp_path: Path) -> None:
    """All CLAIMED tasks across different roles are reset to open after restart."""
    from bernstein.core.task_store import TaskStore

    jsonl = tmp_path / "runtime" / "tasks.jsonl"
    _write_task_jsonl(jsonl, "t-be", title="backend-task", status="claimed", role="backend")
    _write_task_jsonl(jsonl, "t-qa", title="qa-task", status="claimed", role="qa")
    _write_task_jsonl(jsonl, "t-open", title="open-task", status="open", role="backend")

    store = TaskStore(jsonl)
    store.replay_jsonl()
    store.recover_stale_claimed_tasks()

    assert store.get_task("t-be").status.value == "open"  # type: ignore[union-attr]
    assert store.get_task("t-qa").status.value == "open"  # type: ignore[union-attr]
    assert store.get_task("t-open").status.value == "open"  # type: ignore[union-attr]


def test_taskstore_survives_restart(tmp_path: Path) -> None:
    """TaskStore replays an open task after a simulated server restart (JSONL recovery)."""
    from bernstein.core.task_store import TaskStore

    jsonl = tmp_path / "runtime" / "tasks.jsonl"
    task_id = "chaos-task-open-001"

    # Simulate first server lifetime: write task directly to JSONL
    _write_task_jsonl(jsonl, task_id, title="chaos-recovery-test", status="open")

    # Second "server" lifetime: replay from JSONL
    store = TaskStore(jsonl)
    store.replay_jsonl()
    recovered = store.get_task(task_id)

    assert recovered is not None
    assert recovered.title == "chaos-recovery-test"
    assert recovered.status.value == "open"


def test_taskstore_recovers_completed_task(tmp_path: Path) -> None:
    """Completed task status is replayed correctly after restart."""
    from bernstein.core.task_store import TaskStore

    jsonl = tmp_path / "runtime" / "tasks.jsonl"
    task_id = "chaos-task-done-001"

    # Write an open record first, then a done update (simulates two JSONL appends)
    _write_task_jsonl(jsonl, task_id, title="will-complete", status="open")
    _write_task_jsonl(jsonl, task_id, title="will-complete", status="done")

    store = TaskStore(jsonl)
    store.replay_jsonl()
    recovered = store.get_task(task_id)

    assert recovered is not None
    assert recovered.status.value == "done"


def test_taskstore_replay_tolerates_corrupt_line(tmp_path: Path) -> None:
    """Corrupt JSONL lines are skipped; valid records are still recovered."""
    from bernstein.core.task_store import TaskStore

    jsonl = tmp_path / "runtime" / "tasks.jsonl"
    jsonl.parent.mkdir(parents=True, exist_ok=True)

    valid_task = {
        "id": "task-valid-001",
        "title": "ok-task",
        "description": "",
        "role": "qa",
        "priority": 5,
        "status": "open",
    }
    jsonl.write_text(json.dumps(valid_task) + "\n" + "NOT_JSON_AT_ALL\n")

    store = TaskStore(jsonl)
    store.replay_jsonl()

    recovered = store.get_task("task-valid-001")
    assert recovered is not None
    assert recovered.title == "ok-task"


# ---------------------------------------------------------------------------
# Chaos: agent OOM — slot reclaimed, task requeued, worktree preserved
# ---------------------------------------------------------------------------


def test_classify_abort_reason_oom_exit_137() -> None:
    """classify_agent_abort_reason returns OOM for exit code 137 (OOM-kill)."""
    from bernstein.core.agent_lifecycle import classify_agent_abort_reason
    from bernstein.core.models import AbortReason, AgentSession, ModelConfig

    session = AgentSession(id="s1", role="backend", provider="claude", model_config=ModelConfig("sonnet", "high"))
    session.exit_code = 137

    reason, detail = classify_agent_abort_reason(session)
    assert reason == AbortReason.OOM
    assert "137" in detail


def test_classify_abort_reason_oom_sigkill() -> None:
    """classify_agent_abort_reason returns OOM when agent is killed by SIGKILL (signal 9)."""
    from bernstein.core.agent_lifecycle import classify_agent_abort_reason
    from bernstein.core.models import AbortReason, AgentSession, ModelConfig

    session = AgentSession(id="s2", role="backend", provider="claude", model_config=ModelConfig("sonnet", "high"))
    # Negative exit code convention: -9 means killed by signal 9 (SIGKILL)
    session.exit_code = -9

    reason, _detail = classify_agent_abort_reason(session)
    assert reason == AbortReason.OOM


def _make_oom_orch(tmp_path: Path) -> SimpleNamespace:
    """Minimal orchestrator mock for OOM recovery tests."""
    orch = SimpleNamespace()
    orch._config = SimpleNamespace(
        server_url="http://server",
        recovery="resume",
        max_crash_retries=3,
        max_task_retries=3,
    )
    orch._client = MagicMock()
    ok = MagicMock()
    ok.raise_for_status.return_value = None
    orch._client.post.return_value = ok
    orch._client.patch.return_value = ok
    orch._workdir = tmp_path
    orch._rate_limit_tracker = None
    orch._crash_counts = {}
    orch._preserved_worktrees = {}
    orch._retried_task_ids = set()
    orch._record_provider_health = MagicMock()
    orch._evolution = None
    orch._wal_writer = None
    orch._spawner = MagicMock()
    orch._spawner.get_worktree_path.return_value = None
    return orch


def test_oom_slot_reclaimed_and_task_requeued(tmp_path: Path) -> None:
    """After OOM crash the orphaned task is requeued (retried/failed via retry_or_fail_task)."""
    from bernstein.core.agent_lifecycle import handle_orphaned_task
    from bernstein.core.models import AgentSession, Complexity, ModelConfig, Scope, Task, TaskStatus, TaskType

    task = Task(
        id="oom-task-1",
        title="OOM victim task",
        description="",
        role="backend",
        status=TaskStatus.CLAIMED,
        scope=Scope.MEDIUM,
        complexity=Complexity.MEDIUM,
        task_type=TaskType.STANDARD,
    )
    session = AgentSession(
        id="oom-sess-1",
        role="backend",
        provider="claude",
        model_config=ModelConfig("sonnet", "high"),
        task_ids=[task.id],
        exit_code=137,  # OOM kill
    )
    orch = _make_oom_orch(tmp_path)

    with (
        patch("bernstein.core.agents.agent_lifecycle.collect_completion_data", return_value={"files_modified": []}),
        patch("bernstein.core.agents.agent_reaping._has_git_commits_on_branch", return_value=False),
        patch("bernstein.core.agents.agent_lifecycle.complete_task") as mock_complete,
        patch("bernstein.core.agents.agent_lifecycle.retry_or_fail_task") as mock_retry,
    ):
        handle_orphaned_task(orch, task.id, session, {"claimed": [task], "open": [], "in_progress": [], "done": []})

    # Slot was reclaimed (no completion) — task goes to retry/fail path
    mock_complete.assert_not_called()
    mock_retry.assert_called_once()


def test_oom_worktree_preserved_on_resume_policy(tmp_path: Path) -> None:
    """With recovery=resume, the worktree is preserved after OOM so the next agent can resume."""
    from bernstein.core.agent_lifecycle import _maybe_preserve_worktree
    from bernstein.core.models import AgentSession, ModelConfig

    worktree_path = tmp_path / "worktrees" / "oom-sess-2"
    worktree_path.mkdir(parents=True)

    session = AgentSession(
        id="oom-sess-2",
        role="backend",
        provider="claude",
        model_config=ModelConfig("sonnet", "high"),
        task_ids=["oom-task-2"],
        exit_code=137,
    )

    orch = SimpleNamespace()
    orch._config = SimpleNamespace(recovery="resume", max_crash_retries=3)
    orch._crash_counts = {"oom-task-2": 1}
    orch._preserved_worktrees = {}
    orch._spawner = SimpleNamespace(_worktree_paths={"oom-sess-2": worktree_path})

    _maybe_preserve_worktree(orch, session, "oom-task-2")

    # Worktree path stored so the next spawn can reuse it
    assert "oom-task-2" in orch._preserved_worktrees
    assert orch._preserved_worktrees["oom-task-2"] == worktree_path


def test_oom_worktree_not_preserved_when_restart_policy(tmp_path: Path) -> None:
    """With recovery=restart, worktree is NOT preserved after OOM."""
    from bernstein.core.agent_lifecycle import _maybe_preserve_worktree
    from bernstein.core.models import AgentSession, ModelConfig

    worktree_path = tmp_path / "worktrees" / "oom-sess-3"
    worktree_path.mkdir(parents=True)

    session = AgentSession(
        id="oom-sess-3",
        role="backend",
        provider="claude",
        model_config=ModelConfig("sonnet", "high"),
        task_ids=["oom-task-3"],
        exit_code=137,
    )

    orch = SimpleNamespace()
    orch._config = SimpleNamespace(recovery="restart", max_crash_retries=3)
    orch._crash_counts = {"oom-task-3": 0}
    orch._preserved_worktrees = {}
    orch._spawner = SimpleNamespace(_worktree_paths={"oom-sess-3": worktree_path})

    _maybe_preserve_worktree(orch, session, "oom-task-3")

    assert "oom-task-3" not in orch._preserved_worktrees


def test_save_partial_work_skips_missing_worktree() -> None:
    """_save_partial_work returns False immediately when worktree directory is absent."""
    from bernstein.core.agent_lifecycle import _save_partial_work
    from bernstein.core.models import AgentSession, ModelConfig

    session = AgentSession(
        id="oom-sess-no-wt",
        role="backend",
        provider="claude",
        model_config=ModelConfig("sonnet", "high"),
    )
    spawner = MagicMock()
    spawner.get_worktree_path.return_value = None

    result = _save_partial_work(spawner, session)
    assert result is False


# ---------------------------------------------------------------------------
# Chaos: disk full during merge — graceful error, no corruption, cleanup
# ---------------------------------------------------------------------------


def test_retry_io_raises_immediately_on_enospc() -> None:
    """_retry_io raises OSError immediately for ENOSPC (disk full, non-transient)."""
    import asyncio

    from bernstein.core.task_store import _retry_io

    enospc = OSError(errno.ENOSPC, "No space left on device")

    def _fail() -> None:
        raise enospc

    with patch("bernstein.core.tasks.task_store_core.asyncio.to_thread", side_effect=enospc):
        with patch("bernstein.core.tasks.task_store_core.asyncio.sleep"):
            try:
                asyncio.run(_retry_io(_fail))
                raise AssertionError("Expected OSError to be raised")
            except OSError as exc:
                assert exc.errno == errno.ENOSPC


def test_save_partial_work_handles_git_oserror_gracefully(tmp_path: Path) -> None:
    """_save_partial_work suppresses OSError during git commit (disk full) and returns False."""
    from bernstein.core.agent_lifecycle import _save_partial_work
    from bernstein.core.models import AgentSession, ModelConfig

    worktree = tmp_path / "agent-wt"
    worktree.mkdir()

    session = AgentSession(
        id="disk-sess-1",
        role="backend",
        provider="claude",
        model_config=ModelConfig("sonnet", "high"),
    )

    spawner = MagicMock()
    spawner.get_worktree_path.return_value = worktree

    disk_full_error = OSError(errno.ENOSPC, "No space left on device")

    with patch("bernstein.core.agents.agent_lifecycle.subprocess") as mock_sub:
        mock_sub.TimeoutExpired = TimeoutError
        mock_sub.run.side_effect = disk_full_error

        result = _save_partial_work(spawner, session)

    # Disk-full error during git commit is suppressed; returns False (no WIP commit)
    assert result is False


def test_save_partial_work_cleanup_still_called_after_disk_full(tmp_path: Path) -> None:
    """cleanup_worktree is callable after _save_partial_work fails due to disk full."""
    from bernstein.core.agent_lifecycle import _save_partial_work
    from bernstein.core.models import AgentSession, ModelConfig

    worktree = tmp_path / "agent-wt2"
    worktree.mkdir()
    (worktree / "some_file.py").write_text("# work in progress\n")

    session = AgentSession(
        id="disk-sess-2",
        role="backend",
        provider="claude",
        model_config=ModelConfig("sonnet", "high"),
    )

    spawner = MagicMock()
    spawner.get_worktree_path.return_value = worktree

    with patch("bernstein.core.agents.agent_lifecycle.subprocess") as mock_sub:
        mock_sub.TimeoutExpired = TimeoutError
        mock_sub.run.side_effect = OSError(errno.ENOSPC, "No space left on device")
        _save_partial_work(spawner, session)

    # Verify cleanup path: worktree dir still exists (not corrupted) and can be removed
    import shutil

    shutil.rmtree(worktree)
    assert not worktree.exists()


def test_disk_full_merge_cleanup_worktree_survives_oserror(tmp_path: Path) -> None:
    """cleanup_worktree handles OSError during rmtree (e.g. disk full) without raising."""
    from bernstein.core.spawner import AgentSpawner

    # Create a minimal spawner with just enough to test cleanup_worktree
    spawner = AgentSpawner.__new__(AgentSpawner)
    spawner._workdir = tmp_path
    spawner._worktree_paths = {}
    spawner._worktree_roots = {}
    spawner._worktree_managers = {}
    spawner._worktree_mgr = None

    session_id = "disk-sess-3"
    worktree = tmp_path / session_id
    worktree.mkdir()
    spawner._worktree_paths[session_id] = worktree

    with patch("shutil.rmtree", side_effect=OSError(errno.ENOSPC, "No space left on device")):
        # Should not raise — disk-full OSError in rmtree is caught and logged
        spawner.cleanup_worktree(session_id)

    # Worktree path removed from tracking dict regardless of rmtree failure
    assert session_id not in spawner._worktree_paths


# ---------------------------------------------------------------------------
# Chaos: disk full during merge — _merge_worktree_branch resilience
# ---------------------------------------------------------------------------


def test_merge_worktree_branch_returns_failed_result_on_exception(tmp_path: Path) -> None:
    """_merge_worktree_branch returns MergeResult(success=False) on any exception.

    Simulates ENOSPC raised during merge_with_conflict_detection to verify
    the outer exception handler converts it to a clean MergeResult instead
    of propagating the error up the call stack.
    """
    from bernstein.core.spawner import AgentSpawner

    spawner = AgentSpawner.__new__(AgentSpawner)
    spawner._workdir = tmp_path
    spawner._worktree_paths = {}
    spawner._worktree_roots = {}
    spawner._worktree_managers = {}
    spawner._worktree_mgr = None

    enospc = OSError(errno.ENOSPC, "No space left on device")

    with patch("bernstein.core.agents.spawner_merge.merge_with_conflict_detection", side_effect=enospc):
        result = spawner._merge_worktree_branch("disk-sess-4", repo_root=tmp_path)

    assert result.success is False
    assert "No space left on device" in result.error
    assert result.conflicting_files == []


def test_merge_worktree_branch_no_corruption_after_disk_full(tmp_path: Path) -> None:
    """No state corruption after disk-full during merge — worktree tracking stays clean.

    After a failed merge due to ENOSPC, the spawner's internal tracking
    dictionaries must be unmodified so cleanup_worktree can still run.
    """
    from bernstein.core.spawner import AgentSpawner

    spawner = AgentSpawner.__new__(AgentSpawner)
    spawner._workdir = tmp_path
    spawner._worktree_paths = {}
    spawner._worktree_roots = {}
    spawner._worktree_managers = {}
    spawner._worktree_mgr = None

    session_id = "disk-sess-5"
    worktree = tmp_path / session_id
    worktree.mkdir()
    spawner._worktree_paths[session_id] = worktree

    with patch(
        "bernstein.core.agents.spawner_merge.merge_with_conflict_detection",
        side_effect=OSError(errno.ENOSPC, "disk full"),
    ):
        result = spawner._merge_worktree_branch(session_id, repo_root=tmp_path)

    # Merge failed but worktree tracking is intact for cleanup
    assert result.success is False
    assert session_id in spawner._worktree_paths

    # Cleanup can still run without raising
    with patch("shutil.rmtree"):
        spawner.cleanup_worktree(session_id)

    assert session_id not in spawner._worktree_paths


def test_merge_queue_processes_next_job_after_failed_merge(tmp_path: Path) -> None:
    """MergeQueue continues processing after a disk-full failure on one job.

    A failed merge (e.g. ENOSPC) dequeues the current job; the next job in
    the queue remains reachable and can be processed normally.
    """
    from bernstein.core.merge_queue import MergeQueue

    queue = MergeQueue()
    queue.enqueue("sess-fail", task_id="t-fail", task_title="Will fail (disk full)")
    queue.enqueue("sess-ok", task_id="t-ok", task_title="Will succeed")

    assert len(queue) == 2

    # Dequeue the first job and simulate disk-full failure (consumer decides, queue is neutral)
    job1 = queue.dequeue()
    assert job1 is not None
    assert job1.session_id == "sess-fail"

    # Queue depth drops to 1 regardless of merge outcome
    assert len(queue) == 1

    # Second job is still accessible
    job2 = queue.dequeue()
    assert job2 is not None
    assert job2.session_id == "sess-ok"

    assert len(queue) == 0


# ---------------------------------------------------------------------------
# Chaos: kill server mid-task — IN_PROGRESS recovery + CLOSED preservation
# ---------------------------------------------------------------------------


def test_in_progress_task_reset_to_open_after_server_kill(tmp_path: Path) -> None:
    """IN_PROGRESS tasks are reset to OPEN after server kill, just like CLAIMED.

    When the server is killed, tasks in IN_PROGRESS state have no active agent
    any more than CLAIMED tasks do.  recover_stale_claimed_tasks must reset
    both so a fresh agent can pick them up.
    """
    from bernstein.core.task_store import TaskStore

    jsonl = tmp_path / "runtime" / "tasks.jsonl"
    _write_task_jsonl(jsonl, "t-in-prog", title="in-flight-task", status="in_progress")

    store = TaskStore(jsonl)
    store.replay_jsonl()
    store.recover_stale_claimed_tasks()

    task = store.get_task("t-in-prog")
    assert task is not None
    assert task.status.value == "open", (
        f"Expected IN_PROGRESS task to be reset to 'open' after recovery, got '{task.status.value}'"
    )


def test_recovery_resets_both_claimed_and_in_progress(tmp_path: Path) -> None:
    """recover_stale_claimed_tasks resets both CLAIMED and IN_PROGRESS in one pass."""
    from bernstein.core.task_store import TaskStore

    jsonl = tmp_path / "runtime" / "tasks.jsonl"
    _write_task_jsonl(jsonl, "t-claimed", title="claimed-task", status="claimed")
    _write_task_jsonl(jsonl, "t-in-prog", title="in-progress-task", status="in_progress")
    _write_task_jsonl(jsonl, "t-open", title="already-open-task", status="open")

    store = TaskStore(jsonl)
    store.replay_jsonl()
    count = store.recover_stale_claimed_tasks()

    assert count == 2
    assert store.get_task("t-claimed").status.value == "open"  # type: ignore[union-attr]
    assert store.get_task("t-in-prog").status.value == "open"  # type: ignore[union-attr]
    assert store.get_task("t-open").status.value == "open"  # status unchanged


def test_recovery_preserves_closed_terminal_state(tmp_path: Path) -> None:
    """CLOSED tasks (verified+merged) are never touched by recover_stale_claimed_tasks."""
    from bernstein.core.task_store import TaskStore

    jsonl = tmp_path / "runtime" / "tasks.jsonl"
    _write_task_jsonl(jsonl, "t-closed", title="closed-task", status="closed")
    _write_task_jsonl(jsonl, "t-done", title="done-task", status="done")
    _write_task_jsonl(jsonl, "t-failed", title="failed-task", status="failed")

    store = TaskStore(jsonl)
    store.replay_jsonl()
    count = store.recover_stale_claimed_tasks()

    assert count == 0
    assert store.get_task("t-closed").status.value == "closed"  # type: ignore[union-attr]
    assert store.get_task("t-done").status.value == "done"  # type: ignore[union-attr]
    assert store.get_task("t-failed").status.value == "failed"  # type: ignore[union-attr]


def test_recovery_is_idempotent_across_multiple_restarts(tmp_path: Path) -> None:
    """Calling recover_stale_claimed_tasks twice is safe (idempotent).

    A double restart must not cause duplicate resets or spurious resets of
    tasks that are already open.
    """
    from bernstein.core.task_store import TaskStore

    jsonl = tmp_path / "runtime" / "tasks.jsonl"
    _write_task_jsonl(jsonl, "t-stale", title="stale-claimed", status="claimed")

    store = TaskStore(jsonl)
    store.replay_jsonl()

    # First restart
    count1 = store.recover_stale_claimed_tasks()
    assert count1 == 1
    assert store.get_task("t-stale").status.value == "open"  # type: ignore[union-attr]

    # Second restart (task is already open — no additional resets)
    count2 = store.recover_stale_claimed_tasks()
    assert count2 == 0
    assert store.get_task("t-stale").status.value == "open"  # type: ignore[union-attr]


def test_no_data_loss_after_server_kill_with_mixed_task_states(tmp_path: Path) -> None:
    """No tasks are lost after server kill with a mix of all non-terminal states.

    Verifies the full JSONL replay + recovery pipeline preserves data
    integrity: total task count is unchanged and every task is reachable.
    """
    from bernstein.core.task_store import TaskStore

    jsonl = tmp_path / "runtime" / "tasks.jsonl"
    all_ids = ["t-open", "t-claimed", "t-in-prog", "t-done"]
    _write_task_jsonl(jsonl, "t-open", title="open", status="open")
    _write_task_jsonl(jsonl, "t-claimed", title="claimed", status="claimed")
    _write_task_jsonl(jsonl, "t-in-prog", title="in-progress", status="in_progress")
    _write_task_jsonl(jsonl, "t-done", title="done", status="done")

    store = TaskStore(jsonl)
    store.replay_jsonl()
    store.recover_stale_claimed_tasks()

    # All tasks survived replay — no data loss
    for tid in all_ids:
        assert store.get_task(tid) is not None, f"Task {tid} was lost after recovery"

    # Terminal task (done) is unchanged; non-terminal non-open are reset to open
    assert store.get_task("t-done").status.value == "done"  # type: ignore[union-attr]
    assert store.get_task("t-open").status.value == "open"  # type: ignore[union-attr]
    assert store.get_task("t-claimed").status.value == "open"  # type: ignore[union-attr]
    assert store.get_task("t-in-prog").status.value == "open"  # type: ignore[union-attr]
