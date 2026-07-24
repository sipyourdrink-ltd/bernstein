"""``bernstein ps`` cross-checks a live process scan (issue #2874).

``hard_stop`` deletes ``.sdd/runtime/pids/*.json``. Before this change ``ps``
read only those PID files, so once they were gone it reported "No running
agents" even while the orchestrator, server, or a re-parented agent was still
alive. ``ps`` now also scans the OS process table for repo-owned processes and
surfaces the live ones, guarding against the race where a PID exits between the
scan and the liveness re-check.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bernstein.cli.commands import status_cmd


class TestScanLiveAgentRows:
    def test_reports_infra_and_agents_without_pid_files(self, tmp_path: Path, monkeypatch: Any) -> None:
        worktree_prefix = str(tmp_path / ".sdd" / "worktrees")
        lines = [
            (100, "python -m uvicorn bernstein.core.server:app --port 8052"),
            (101, "python -m bernstein.core.orchestrator --port 8052 --cells 1"),
            (102, "python -m bernstein.core.orchestration.bootstrap --watchdog --port 8052"),
            (103, f"python worker --worktree {worktree_prefix}/task-7"),
            (104, "python -c import time; time.sleep(9)"),  # unrelated
        ]
        monkeypatch.setattr(status_cmd, "list_command_lines", lambda: lines)
        # Infra processes require cwd==workdir; the agent (104) is unrelated.
        monkeypatch.setattr(status_cmd, "process_cwd", lambda _pid: tmp_path)
        monkeypatch.setattr(status_cmd, "is_process_alive", lambda _pid: True)

        rows = status_cmd._scan_live_agent_rows(tmp_path, set())
        by_pid = {r["worker_pid"]: r for r in rows}

        assert set(by_pid) == {100, 101, 102, 103}
        assert by_pid[100]["role"] == "server"
        assert by_pid[101]["role"] == "spawner"
        assert by_pid[102]["role"] == "watchdog"
        assert by_pid[103]["role"] == "agent"
        assert all(r["source"] == "scan" for r in rows)

    def test_skips_known_pids(self, tmp_path: Path, monkeypatch: Any) -> None:
        lines = [(200, "python -m uvicorn bernstein.core.server:app --port 8052")]
        monkeypatch.setattr(status_cmd, "list_command_lines", lambda: lines)
        monkeypatch.setattr(status_cmd, "process_cwd", lambda _pid: tmp_path)
        monkeypatch.setattr(status_cmd, "is_process_alive", lambda _pid: True)

        rows = status_cmd._scan_live_agent_rows(tmp_path, {200})
        assert rows == []

    def test_race_pid_dies_mid_scan_is_dropped(self, tmp_path: Path, monkeypatch: Any) -> None:
        worktree_prefix = str(tmp_path / ".sdd" / "worktrees")
        lines = [(300, f"python worker --worktree {worktree_prefix}/task-1")]
        monkeypatch.setattr(status_cmd, "list_command_lines", lambda: lines)
        monkeypatch.setattr(status_cmd, "process_cwd", lambda _pid: tmp_path)
        # The pid appeared in the ps snapshot but has since exited.
        monkeypatch.setattr(status_cmd, "is_process_alive", lambda _pid: False)

        assert status_cmd._scan_live_agent_rows(tmp_path, set()) == []

    def test_infra_outside_workdir_is_ignored(self, tmp_path: Path, monkeypatch: Any) -> None:
        """A server from a sibling checkout must not be attributed to us."""
        lines = [(400, "python -m uvicorn bernstein.core.server:app --port 8052")]
        monkeypatch.setattr(status_cmd, "list_command_lines", lambda: lines)
        monkeypatch.setattr(status_cmd, "process_cwd", lambda _pid: tmp_path / "other")
        monkeypatch.setattr(status_cmd, "is_process_alive", lambda _pid: True)

        assert status_cmd._scan_live_agent_rows(tmp_path, set()) == []


class TestPsCommandLiveScan:
    def test_ps_shows_scanned_agent_when_pid_files_gone(self, tmp_path: Path, monkeypatch: Any) -> None:
        """End-to-end: no PID files, but a live server surfaces in ``ps``."""
        from click.testing import CliRunner

        monkeypatch.chdir(tmp_path)
        pid_dir = tmp_path / ".sdd" / "runtime" / "pids"
        pid_dir.mkdir(parents=True)

        lines = [(500, "python -m uvicorn bernstein.core.server:app --port 8052")]
        monkeypatch.setattr(status_cmd, "list_command_lines", lambda: lines)
        monkeypatch.setattr(status_cmd, "process_cwd", lambda _pid: tmp_path)
        monkeypatch.setattr(status_cmd, "is_process_alive", lambda _pid: True)

        result = CliRunner().invoke(status_cmd.ps_cmd, ["--json-output", "--pid-dir", str(pid_dir)])
        assert result.exit_code == 0, result.output
        assert "500" in result.output
        assert "server" in result.output
