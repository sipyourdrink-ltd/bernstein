"""Tests for bernstein-worker process wrapper and bernstein ps."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from typing import TYPE_CHECKING

from bernstein.adapters.base import build_worker_cmd

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# build_worker_cmd
# ---------------------------------------------------------------------------


class TestBuildWorkerCmd:
    def test_basic_wrapping(self, tmp_path: Path) -> None:
        result = build_worker_cmd(
            ["claude", "--model", "sonnet"],
            role="qa",
            session_id="qa-abc123",
            pid_dir=tmp_path,
            model="claude-sonnet-4-6",
        )
        assert result[0] == sys.executable
        assert result[1:3] == ["-m", "bernstein.core.worker"]
        assert "--role" in result
        assert result[result.index("--role") + 1] == "qa"
        assert "--session" in result
        assert result[result.index("--session") + 1] == "qa-abc123"
        assert "--" in result
        sep_idx = result.index("--")
        assert result[sep_idx + 1 :] == ["claude", "--model", "sonnet"]

    def test_model_metadata(self, tmp_path: Path) -> None:
        result = build_worker_cmd(
            ["codex"],
            role="backend",
            session_id="backend-xyz",
            pid_dir=tmp_path,
            model="gpt-4o",
        )
        assert "--model" in result
        assert result[result.index("--model") + 1] == "gpt-4o"


# ---------------------------------------------------------------------------
# Worker process (integration)
# ---------------------------------------------------------------------------


class TestWorkerProcess:
    def test_worker_writes_and_cleans_pid_file(self, tmp_path: Path) -> None:
        """Worker should write PID file on start and remove it on exit."""
        pid_dir = tmp_path / "pids"
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "bernstein.core.worker",
                "--role",
                "test",
                "--session",
                "test-123",
                "--pid-dir",
                str(pid_dir),
                "--model",
                "test-model",
                "--",
                "sleep",
                "10",
            ],
            start_new_session=True,
        )

        # Wait for PID file to appear
        pid_file = pid_dir / "test-123.json"
        deadline = time.monotonic() + 5
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.1)

        assert pid_file.exists(), "PID file was not created"

        # Wait for child_pid to be written (second write after child spawn)
        deadline2 = time.monotonic() + 5
        info: dict[str, object] = {}
        while time.monotonic() < deadline2:
            info = json.loads(pid_file.read_text())
            if "child_pid" in info:
                break
            time.sleep(0.1)

        assert info["role"] == "test"
        assert info["session"] == "test-123"
        assert info["command"] == "sleep"
        assert info["model"] == "test-model"
        assert "worker_pid" in info
        assert "child_pid" in info

        # Kill and verify cleanup
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=5)

        # Give a moment for cleanup
        time.sleep(0.2)
        assert not pid_file.exists(), "PID file was not cleaned up"

    def test_worker_forwards_signals(self, tmp_path: Path) -> None:
        """Worker should forward SIGTERM to child and exit."""
        pid_dir = tmp_path / "pids"
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "bernstein.core.worker",
                "--role",
                "qa",
                "--session",
                "sig-test",
                "--pid-dir",
                str(pid_dir),
                "--",
                "sleep",
                "60",
            ],
            start_new_session=True,
        )

        # Wait for PID file
        pid_file = pid_dir / "sig-test.json"
        deadline = time.monotonic() + 5
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.1)

        assert pid_file.exists()

        # Send SIGTERM to the process group
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        proc.wait(timeout=5)

        # Should have exited (SIGTERM → child killed → worker exits)
        assert proc.poll() is not None

    def test_worker_exits_with_child_code(self, tmp_path: Path) -> None:
        """Worker should exit with the child's exit code."""
        pid_dir = tmp_path / "pids"
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "bernstein.core.worker",
                "--role",
                "test",
                "--session",
                "exit-test",
                "--pid-dir",
                str(pid_dir),
                "--",
                sys.executable,
                "-c",
                "import sys; sys.exit(42)",
            ],
        )
        exit_code = proc.wait(timeout=10)
        assert exit_code == 42

    def test_worker_handles_missing_command(self) -> None:
        """Worker should exit 127 for missing command."""
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "bernstein.core.worker",
                "--role",
                "test",
                "--session",
                "missing-cmd",
                "--pid-dir",
                "/tmp",
                "--",
                "nonexistent_command_xyz_12345",
            ],
            stderr=subprocess.PIPE,
        )
        exit_code = proc.wait(timeout=10)
        assert exit_code == 127
