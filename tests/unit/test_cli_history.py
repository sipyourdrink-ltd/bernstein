"""Tests for CLI history tracking and undo suggestions."""

from __future__ import annotations

from pathlib import Path

from bernstein.cli.cli_history import (
    UNDO_MAP,
    HistoryEntry,
    format_history,
    is_destructive,
    load_history,
    record_command,
    suggest_undo,
)


class TestHistoryEntry:
    """HistoryEntry frozen dataclass creation."""

    def test_create_entry(self) -> None:
        entry = HistoryEntry(
            command="run",
            args=["--plan", "foo.yaml"],
            timestamp="2026-04-10T12:00:00+00:00",
            cwd="/tmp/project",
            exit_code=0,
        )
        assert entry.command == "run"
        assert entry.args == ["--plan", "foo.yaml"]
        assert entry.timestamp == "2026-04-10T12:00:00+00:00"
        assert entry.cwd == "/tmp/project"
        assert entry.exit_code == 0

    def test_create_entry_none_exit_code(self) -> None:
        entry = HistoryEntry(
            command="status",
            args=[],
            timestamp="2026-04-10T12:00:00+00:00",
            cwd="/tmp/project",
            exit_code=None,
        )
        assert entry.exit_code is None

    def test_frozen(self) -> None:
        entry = HistoryEntry(
            command="run",
            args=[],
            timestamp="2026-04-10T12:00:00+00:00",
            cwd="/tmp",
            exit_code=0,
        )
        try:
            entry.command = "stop"  # type: ignore[misc]
            raise AssertionError("Expected FrozenInstanceError")  # pragma: no cover
        except AttributeError:
            pass  # expected — frozen dataclass


class TestRecordCommand:
    """record_command writes JSONL to disk."""

    def test_writes_jsonl(self, tmp_path: Path) -> None:
        hist = tmp_path / "history.jsonl"
        record_command("run", ["--verbose"], exit_code=0, history_path=hist)

        assert hist.exists()
        lines = hist.read_text().strip().splitlines()
        assert len(lines) == 1

    def test_appends_multiple(self, tmp_path: Path) -> None:
        hist = tmp_path / "history.jsonl"
        record_command("run", [], exit_code=0, history_path=hist)
        record_command("stop", [], exit_code=0, history_path=hist)

        lines = hist.read_text().strip().splitlines()
        assert len(lines) == 2

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        hist = tmp_path / "sub" / "dir" / "history.jsonl"
        record_command("status", [], history_path=hist)

        assert hist.exists()


class TestLoadHistory:
    """load_history reads entries back from JSONL."""

    def test_reads_entries(self, tmp_path: Path) -> None:
        hist = tmp_path / "history.jsonl"
        record_command("run", ["plan.yaml"], exit_code=0, history_path=hist)
        record_command("stop", [], exit_code=0, history_path=hist)

        entries = load_history(history_path=hist)
        assert len(entries) == 2
        # Newest first
        assert entries[0].command == "stop"
        assert entries[1].command == "run"

    def test_limit(self, tmp_path: Path) -> None:
        hist = tmp_path / "history.jsonl"
        for i in range(10):
            record_command(f"cmd-{i}", [], exit_code=0, history_path=hist)

        entries = load_history(history_path=hist, limit=3)
        assert len(entries) == 3
        # Should be the 3 most recent, newest first
        assert entries[0].command == "cmd-9"
        assert entries[1].command == "cmd-8"
        assert entries[2].command == "cmd-7"

    def test_empty_file(self, tmp_path: Path) -> None:
        hist = tmp_path / "history.jsonl"
        hist.write_text("")
        entries = load_history(history_path=hist)
        assert entries == []

    def test_missing_file(self, tmp_path: Path) -> None:
        hist = tmp_path / "nonexistent.jsonl"
        entries = load_history(history_path=hist)
        assert entries == []


class TestSuggestUndo:
    """suggest_undo returns the correct inverse command."""

    def test_stop_returns_run(self) -> None:
        assert suggest_undo("stop") == "run"

    def test_task_cancel_returns_task_retry(self) -> None:
        assert suggest_undo("task cancel") == "task retry"

    def test_drain_returns_undrain(self) -> None:
        assert suggest_undo("drain") == "undrain"

    def test_nondestructive_returns_none(self) -> None:
        assert suggest_undo("status") is None

    def test_unknown_returns_none(self) -> None:
        assert suggest_undo("nonexistent-command") is None


class TestIsDestructive:
    """is_destructive classifies commands correctly."""

    def test_destructive_commands(self) -> None:
        for cmd in UNDO_MAP:
            assert is_destructive(cmd) is True

    def test_non_destructive(self) -> None:
        assert is_destructive("status") is False
        assert is_destructive("run") is False
        assert is_destructive("agents") is False


class TestFormatHistory:
    """format_history produces readable output."""

    def test_empty(self) -> None:
        assert format_history([]) == "No command history."

    def test_produces_table(self) -> None:
        entries = [
            HistoryEntry(
                command="run",
                args=["plan.yaml"],
                timestamp="2026-04-10T12:00:00+00:00",
                cwd="/tmp",
                exit_code=0,
            ),
        ]
        output = format_history(entries)
        assert "Timestamp" in output
        assert "Command" in output
        assert "run" in output
        assert "plan.yaml" in output

    def test_limit_respected(self) -> None:
        entries = [
            HistoryEntry(
                command=f"cmd-{i}",
                args=[],
                timestamp=f"2026-04-10T12:0{i}:00+00:00",
                cwd="/tmp",
                exit_code=0,
            )
            for i in range(5)
        ]
        output = format_history(entries, limit=2)
        # Header + separator + 2 data rows = 4 lines
        lines = output.strip().splitlines()
        assert len(lines) == 4

    def test_none_exit_code_shows_dash(self) -> None:
        entries = [
            HistoryEntry(
                command="status",
                args=[],
                timestamp="2026-04-10T12:00:00+00:00",
                cwd="/tmp",
                exit_code=None,
            ),
        ]
        output = format_history(entries)
        assert "-" in output
