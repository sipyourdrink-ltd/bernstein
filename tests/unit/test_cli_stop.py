"""Tests for soft/hard stop, shutdown signals, and crash recovery."""

from __future__ import annotations

import json
import os
import signal
from pathlib import Path
from unittest.mock import patch

import bernstein.cli.stop_cmd as stop_cmd_module
from click.testing import CliRunner

from bernstein.cli.main import (
    cli,
    kill_pid_hard,
    recover_orphaned_claims,
    return_claimed_to_open,
    save_session_on_stop,
    write_shutdown_signals,
)

# ---------------------------------------------------------------------------
# write_shutdown_signals
# ---------------------------------------------------------------------------


class TestWriteShutdownSignals:
    def test_writes_signal_files_for_each_agent(self, tmp_path: Path) -> None:
        """Creates SHUTDOWN files under signals/{id}/ for each agent."""
        os.chdir(tmp_path)
        runtime = tmp_path / ".sdd" / "runtime"
        runtime.mkdir(parents=True)
        agents = {"agents": [{"id": "agent-1"}, {"id": "agent-2"}]}
        (runtime / "agents.json").write_text(json.dumps(agents))

        result = write_shutdown_signals(reason="test stop")

        assert sorted(result) == ["agent-1", "agent-2"]
        for aid in ("agent-1", "agent-2"):
            sig_file = tmp_path / ".sdd" / "runtime" / "signals" / aid / "SHUTDOWN"
            assert sig_file.exists()
            content = sig_file.read_text()
            assert "test stop" in content

    def test_returns_empty_when_no_agents_json(self, tmp_path: Path) -> None:
        """Returns empty list when agents.json does not exist."""
        os.chdir(tmp_path)
        result = write_shutdown_signals()
        assert result == []

    def test_skips_agents_without_id(self, tmp_path: Path) -> None:
        """Agents missing the 'id' field are silently skipped."""
        os.chdir(tmp_path)
        runtime = tmp_path / ".sdd" / "runtime"
        runtime.mkdir(parents=True)
        agents = {"agents": [{"id": ""}, {"id": "good-agent"}]}
        (runtime / "agents.json").write_text(json.dumps(agents))

        result = write_shutdown_signals()
        assert result == ["good-agent"]

    def test_handles_malformed_json(self, tmp_path: Path) -> None:
        """Gracefully returns empty on malformed agents.json."""
        os.chdir(tmp_path)
        runtime = tmp_path / ".sdd" / "runtime"
        runtime.mkdir(parents=True)
        (runtime / "agents.json").write_text("{bad json")

        result = write_shutdown_signals()
        assert result == []


# ---------------------------------------------------------------------------
# return_claimed_to_open
# ---------------------------------------------------------------------------


class TestReturnClaimedToOpen:
    def test_moves_claimed_files_to_open(self, tmp_path: Path) -> None:
        """Files in claimed/ are moved back to open/."""
        os.chdir(tmp_path)
        claimed = tmp_path / ".sdd" / "backlog" / "claimed"
        open_dir = tmp_path / ".sdd" / "backlog" / "open"
        claimed.mkdir(parents=True)
        open_dir.mkdir(parents=True)
        (claimed / "100-some-task.yaml").write_text("# Task")
        (claimed / "101-other-task.yaml").write_text("# Task 2")

        count = return_claimed_to_open()

        assert count == 2
        assert (open_dir / "100-some-task.yaml").exists()
        assert (open_dir / "101-other-task.yaml").exists()
        assert not list(claimed.glob("*.md"))

    def test_removes_duplicates_in_closed(self, tmp_path: Path) -> None:
        """Files whose number exists in closed/ are deleted, not moved."""
        os.chdir(tmp_path)
        claimed = tmp_path / ".sdd" / "backlog" / "claimed"
        open_dir = tmp_path / ".sdd" / "backlog" / "open"
        closed = tmp_path / ".sdd" / "backlog" / "closed"
        claimed.mkdir(parents=True)
        open_dir.mkdir(parents=True)
        closed.mkdir(parents=True)

        (claimed / "100-task.yaml").write_text("# claimed")
        (closed / "100-task.yaml").write_text("# done")

        count = return_claimed_to_open()

        assert count == 0
        assert not (claimed / "100-task.yaml").exists()  # deleted
        assert not (open_dir / "100-task.yaml").exists()  # not moved

    def test_removes_duplicates_in_done(self, tmp_path: Path) -> None:
        """Files whose number exists in done/ are deleted, not moved."""
        os.chdir(tmp_path)
        claimed = tmp_path / ".sdd" / "backlog" / "claimed"
        open_dir = tmp_path / ".sdd" / "backlog" / "open"
        done = tmp_path / ".sdd" / "backlog" / "done"
        claimed.mkdir(parents=True)
        open_dir.mkdir(parents=True)
        done.mkdir(parents=True)

        (claimed / "200-build-api.yaml").write_text("# claimed")
        (done / "200-build-api.yaml").write_text("# completed")

        count = return_claimed_to_open()

        assert count == 0
        assert not (claimed / "200-build-api.yaml").exists()

    def test_returns_zero_when_no_claimed_dir(self, tmp_path: Path) -> None:
        """Returns 0 when claimed/ directory doesn't exist."""
        os.chdir(tmp_path)
        count = return_claimed_to_open()
        assert count == 0

    def test_creates_open_dir_if_missing(self, tmp_path: Path) -> None:
        """Creates open/ directory if it doesn't exist."""
        os.chdir(tmp_path)
        claimed = tmp_path / ".sdd" / "backlog" / "claimed"
        claimed.mkdir(parents=True)
        (claimed / "300-new-task.yaml").write_text("# Task")

        count = return_claimed_to_open()

        assert count == 1
        assert (tmp_path / ".sdd" / "backlog" / "open" / "300-new-task.yaml").exists()


# ---------------------------------------------------------------------------
# save_session_on_stop
# ---------------------------------------------------------------------------


class TestSaveSessionOnStop:
    def test_writes_session_state_json(self, tmp_path: Path) -> None:
        """Creates session_state.json with correct structure."""
        sdd = tmp_path / ".sdd"
        (sdd / "backlog" / "open").mkdir(parents=True)
        (sdd / "backlog" / "claimed").mkdir(parents=True)
        (sdd / "backlog" / "open" / "1-task.yaml").write_text("# T")
        (sdd / "backlog" / "claimed" / "2-task.yaml").write_text("# T2")

        # Mock httpx to force fallback path (no running server)
        with patch("bernstein.cli.stop_cmd.auth_headers", return_value={}):
            with patch("httpx.get", side_effect=Exception("no server")):
                save_session_on_stop(tmp_path)

        state_file = sdd / "runtime" / "session_state.json"
        assert state_file.exists()
        state = json.loads(state_file.read_text())
        assert state["open_tasks"] == 1
        assert state["claimed_tasks"] == 1
        assert "stopped_at" in state

    def test_handles_missing_backlog_dirs(self, tmp_path: Path) -> None:
        """Works even when backlog directories don't exist."""
        with patch("httpx.get", side_effect=Exception("no server")):
            save_session_on_stop(tmp_path)

        state_file = tmp_path / ".sdd" / "runtime" / "session_state.json"
        assert state_file.exists()
        state = json.loads(state_file.read_text())
        assert state["open_tasks"] == 0
        assert state["claimed_tasks"] == 0


# ---------------------------------------------------------------------------
# recover_orphaned_claims
# ---------------------------------------------------------------------------


class TestRecoverOrphanedClaims:
    def test_delegates_toreturn_claimed_to_open(self, tmp_path: Path) -> None:
        """recover_orphaned_claims just calls return_claimed_to_open."""
        os.chdir(tmp_path)
        claimed = tmp_path / ".sdd" / "backlog" / "claimed"
        claimed.mkdir(parents=True)
        (claimed / "400-orphan.md").write_text("# Orphaned")

        count = recover_orphaned_claims()
        assert count == 1
        assert (tmp_path / ".sdd" / "backlog" / "open" / "400-orphan.md").exists()


# ---------------------------------------------------------------------------
# kill_pid_hard
# ---------------------------------------------------------------------------


class TestKillPidHard:
    def test_sends_sigkill_to_process_group(self, tmp_path: Path) -> None:
        """Sends SIGKILL to the process group."""
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("12345")

        with (
            patch("bernstein.cli.helpers.is_alive", return_value=True),
            patch("os.getpgid", return_value=12345),
            patch("os.killpg") as mock_killpg,
        ):
            kill_pid_hard(str(pid_file), "test")

        mock_killpg.assert_called_once_with(12345, signal.SIGKILL)
        assert not pid_file.exists()

    def test_falls_back_to_kill_on_getpgid_error(self, tmp_path: Path) -> None:
        """Falls back to os.kill if getpgid fails."""
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("99999")

        with (
            patch("bernstein.cli.helpers.is_alive", return_value=True),
            patch("os.getpgid", side_effect=OSError("no pgid")),
            patch("os.kill") as mock_kill,
        ):
            kill_pid_hard(str(pid_file), "test")

        mock_kill.assert_called_once_with(99999, signal.SIGKILL)

    def test_removes_pid_file_even_if_not_alive(self, tmp_path: Path) -> None:
        """PID file is removed even when the process is not running."""
        pid_file = tmp_path / "test.pid"
        pid_file.write_text("11111")

        with patch("bernstein.cli.helpers.is_alive", return_value=False):
            kill_pid_hard(str(pid_file), "test")

        assert not pid_file.exists()


class TestHardStopFallbacks:
    def test_collect_pids_from_supervisor_state_kills_server_when_pid_file_is_missing(self, tmp_path: Path) -> None:
        os.chdir(tmp_path)
        runtime = tmp_path / ".sdd" / "runtime"
        runtime.mkdir(parents=True)
        (runtime / "supervisor_state.json").write_text(
            json.dumps(
                {
                    "started_at": 1.0,
                    "restart_count": 0,
                    "current_pid": 4321,
                    "last_restart_at": None,
                }
            ),
            encoding="utf-8",
        )

        killed: set[int] = set()
        with (
            patch("bernstein.cli.stop_cmd.is_alive", return_value=True),
            patch("bernstein.cli.stop_cmd._kill_named_pid") as mock_kill,
        ):
            stop_cmd_module._collect_pids_from_supervisor_state(killed)  # pyright: ignore[reportPrivateUsage]

        mock_kill.assert_called_once_with(4321, "Task server", killed)

    def test_collect_repo_processes_targets_repo_owned_runtime_processes(self, tmp_path: Path) -> None:
        os.chdir(tmp_path)
        snapshots = [
            stop_cmd_module._ProcessSnapshot(  # pyright: ignore[reportPrivateUsage]
                pid=10,
                ppid=1,
                pgid=10,
                command=f"/bin/zsh -c while true; do > '{tmp_path}/.sdd/runtime/heartbeats/a.json'; done",
            ),
            stop_cmd_module._ProcessSnapshot(  # pyright: ignore[reportPrivateUsage]
                pid=11,
                ppid=1,
                pgid=11,
                command="/usr/bin/python -m bernstein.core.bootstrap --watchdog --port 8052",
            ),
            stop_cmd_module._ProcessSnapshot(  # pyright: ignore[reportPrivateUsage]
                pid=12,
                ppid=1,
                pgid=12,
                command="/usr/bin/python -m bernstein.core.orchestrator --port 8052 --cells 1",
            ),
            stop_cmd_module._ProcessSnapshot(  # pyright: ignore[reportPrivateUsage]
                pid=13,
                ppid=1,
                pgid=13,
                command="/usr/bin/python -m uvicorn bernstein.core.server:app --host 127.0.0.1 --port 8052",
            ),
            stop_cmd_module._ProcessSnapshot(  # pyright: ignore[reportPrivateUsage]
                pid=14,
                ppid=1,
                pgid=14,
                command="/usr/bin/python -m bernstein.core.bootstrap --watchdog --port 8052",
            ),
        ]

        agent_calls: list[int] = []
        infra_calls: list[tuple[int, str]] = []
        killed: set[int] = set()

        def _record_agent(pid: int, label: str, seen: set[int]) -> None:
            del label, seen
            agent_calls.append(pid)

        def _record_infra(pid: int, label: str, seen: set[int]) -> None:
            del seen
            infra_calls.append((pid, label))

        with (
            patch("bernstein.cli.stop_cmd._list_process_snapshots", return_value=snapshots),
            patch(
                "bernstein.cli.stop_cmd.process_cwd",
                side_effect=[tmp_path, tmp_path, tmp_path, Path("/tmp")],
            ),
            patch(
                "bernstein.cli.stop_cmd._kill_agent_pid",
                side_effect=_record_agent,
            ),
            patch(
                "bernstein.cli.stop_cmd._kill_named_pid",
                side_effect=_record_infra,
            ),
        ):
            stop_cmd_module._collect_repo_processes(killed)  # pyright: ignore[reportPrivateUsage]

        assert agent_calls == [10]
        assert infra_calls == [(11, "Watchdog"), (12, "Spawner"), (13, "Task server")]


# ---------------------------------------------------------------------------
# CLI stop command integration
# ---------------------------------------------------------------------------


class TestStopCommand:
    def test_soft_stop_is_default(self) -> None:
        """Default stop (no flags) calls _soft_stop."""
        runner = CliRunner()
        with patch("bernstein.cli.stop_cmd.soft_stop") as mock_soft:
            result = runner.invoke(cli, ["stop"])
            mock_soft.assert_called_once_with(30)
            assert result.exit_code == 0

    def test_hard_stop_with_force_flag(self) -> None:
        """--force flag triggers _hard_stop."""
        runner = CliRunner()
        with patch("bernstein.cli.stop_cmd.hard_stop") as mock_hard:
            result = runner.invoke(cli, ["stop", "--force"])
            mock_hard.assert_called_once()
            assert result.exit_code == 0

    def test_hard_stop_with_hard_flag(self) -> None:
        """--hard flag (alias) triggers _hard_stop."""
        runner = CliRunner()
        with patch("bernstein.cli.stop_cmd.hard_stop") as mock_hard:
            result = runner.invoke(cli, ["stop", "--hard"])
            mock_hard.assert_called_once()
            assert result.exit_code == 0

    def test_custom_timeout(self) -> None:
        """--timeout value is forwarded to _soft_stop."""
        runner = CliRunner()
        with patch("bernstein.cli.stop_cmd.soft_stop") as mock_soft:
            result = runner.invoke(cli, ["stop", "--timeout", "60"])
            mock_soft.assert_called_once_with(60)
            assert result.exit_code == 0
