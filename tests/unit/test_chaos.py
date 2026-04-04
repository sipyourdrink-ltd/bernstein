"""Tests for Chaos Engineering CLI and server kill recovery."""

from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from bernstein.cli.chaos_cmd import chaos_group

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

    valid_task = {"id": "task-valid-001", "title": "ok-task", "description": "", "role": "qa", "priority": 5, "status": "open"}
    jsonl.write_text(json.dumps(valid_task) + "\n" + "NOT_JSON_AT_ALL\n")

    store = TaskStore(jsonl)
    store.replay_jsonl()

    recovered = store.get_task("task-valid-001")
    assert recovered is not None
    assert recovered.title == "ok-task"
