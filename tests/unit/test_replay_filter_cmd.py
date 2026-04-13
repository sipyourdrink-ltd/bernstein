"""Tests for CLI-010: replay with filtering and search."""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.cli.replay_filter_cmd import filter_events, replay_filter_cmd
from click.testing import CliRunner


def _make_events() -> list[dict[str, object]]:
    """Create sample replay events for testing."""
    return [
        {"ts": 1.0, "elapsed_s": 0.0, "event": "run_started", "run_id": "run1"},
        {
            "ts": 2.0,
            "elapsed_s": 1.0,
            "event": "agent_spawned",
            "agent_id": "backend-abc123",
            "role": "backend",
            "model": "sonnet",
        },
        {"ts": 3.0, "elapsed_s": 2.0, "event": "task_claimed", "agent_id": "backend-abc123", "task_id": "task-1"},
        {
            "ts": 4.0,
            "elapsed_s": 3.0,
            "event": "agent_spawned",
            "agent_id": "qa-def456",
            "role": "qa",
            "model": "haiku",
        },
        {
            "ts": 5.0,
            "elapsed_s": 4.0,
            "event": "task_completed",
            "agent_id": "backend-abc123",
            "task_id": "task-1",
            "status": "done",
        },
        {"ts": 6.0, "elapsed_s": 5.0, "event": "run_completed", "run_id": "run1"},
    ]


class TestFilterEvents:
    """Tests for the event filtering logic."""

    def test_no_filters_returns_all(self) -> None:
        events = _make_events()
        result = filter_events(events)
        assert len(result) == len(events)

    def test_filter_by_event_type(self) -> None:
        events = _make_events()
        result = filter_events(events, event_type="agent_spawned")
        assert len(result) == 2
        assert all(e["event"] == "agent_spawned" for e in result)

    def test_filter_by_agent(self) -> None:
        events = _make_events()
        result = filter_events(events, agent="backend")
        assert len(result) == 3  # spawned, claimed, completed
        assert all("backend" in str(e.get("agent_id", "")) for e in result)

    def test_filter_by_search(self) -> None:
        events = _make_events()
        result = filter_events(events, search="sonnet")
        assert len(result) == 1
        assert result[0]["event"] == "agent_spawned"

    def test_filter_by_key_value(self) -> None:
        events = _make_events()
        result = filter_events(events, filter_str="role=qa")
        assert len(result) == 1
        assert result[0]["role"] == "qa"

    def test_combined_filters(self) -> None:
        events = _make_events()
        result = filter_events(events, event_type="agent_spawned", agent="backend")
        assert len(result) == 1
        assert result[0]["agent_id"] == "backend-abc123"

    def test_no_matches(self) -> None:
        events = _make_events()
        result = filter_events(events, search="nonexistent_string")
        assert len(result) == 0


class TestReplayFilterCmd:
    """Tests for the replay filter CLI command."""

    def test_replay_filter_help(self) -> None:
        runner = CliRunner()
        result = runner.invoke(replay_filter_cmd, ["--help"])
        assert result.exit_code == 0
        assert "--filter" in result.output
        assert "--event-type" in result.output
        assert "--agent" in result.output
        assert "--search" in result.output

    def test_replay_filter_list(self, tmp_path: Path) -> None:
        sdd_dir = tmp_path / ".sdd"
        runs_dir = sdd_dir / "runs" / "20240315-143022"
        runs_dir.mkdir(parents=True)
        (runs_dir / "replay.jsonl").write_text(json.dumps({"ts": 1.0, "elapsed_s": 0.0, "event": "run_started"}) + "\n")

        runner = CliRunner()
        result = runner.invoke(replay_filter_cmd, ["list", "--sdd-dir", str(sdd_dir)])
        assert result.exit_code == 0
        assert "20240315" in result.output

    def test_replay_filter_with_event_type(self, tmp_path: Path) -> None:
        sdd_dir = tmp_path / ".sdd"
        runs_dir = sdd_dir / "runs" / "run1"
        runs_dir.mkdir(parents=True)
        events = [
            {"ts": 1.0, "elapsed_s": 0.0, "event": "run_started"},
            {"ts": 2.0, "elapsed_s": 1.0, "event": "agent_spawned", "agent_id": "be-1"},
            {"ts": 3.0, "elapsed_s": 2.0, "event": "run_completed"},
        ]
        (runs_dir / "replay.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")

        runner = CliRunner()
        result = runner.invoke(
            replay_filter_cmd,
            ["run1", "--sdd-dir", str(sdd_dir), "--event-type", "agent_spawned"],
        )
        assert result.exit_code == 0
        assert "agent_spawned" in result.output

    def test_replay_empty_after_filter(self, tmp_path: Path) -> None:
        sdd_dir = tmp_path / ".sdd"
        runs_dir = sdd_dir / "runs" / "run1"
        runs_dir.mkdir(parents=True)
        events = [{"ts": 1.0, "elapsed_s": 0.0, "event": "run_started"}]
        (runs_dir / "replay.jsonl").write_text(json.dumps(events[0]) + "\n")

        runner = CliRunner()
        result = runner.invoke(
            replay_filter_cmd,
            ["run1", "--sdd-dir", str(sdd_dir), "--search", "nonexistent"],
        )
        assert result.exit_code == 0
        assert "no events match" in result.output.lower()
