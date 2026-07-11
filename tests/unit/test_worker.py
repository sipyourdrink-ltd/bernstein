"""Tests for bernstein-worker process wrapper and bernstein ps."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from typing import TYPE_CHECKING

import pytest

from bernstein.adapters.base import build_worker_cmd
from bernstein.core.orchestration.worker import _resolve_launch_cmd

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
            workdir=tmp_path,
            log_path=tmp_path / "qa-abc123.log",
            model="claude-sonnet-4-6",
        )
        assert result[0] == sys.executable
        assert result[1:3] == ["-m", "bernstein.core.orchestration.worker"]
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
            workdir=tmp_path,
            log_path=tmp_path / "backend-xyz.log",
            model="gpt-5.4",
        )
        assert "--model" in result
        assert result[result.index("--model") + 1] == "gpt-5.4"


# ---------------------------------------------------------------------------
# _resolve_launch_cmd -- cross-platform argv[0] resolution (issue #2287)
# ---------------------------------------------------------------------------


class TestResolveLaunchCmd:
    """argv[0] resolution: bare name -> absolute path, Windows .cmd -> cmd.exe.

    On Windows, ``CreateProcess`` (what ``subprocess.Popen`` calls without a
    shell) ignores ``PATHEXT`` for ``argv[0]`` and cannot run a ``.cmd``/``.bat``
    batch shim even by full path. nvm-windows installs the Codex/Claude/Gemini
    CLIs as ``codex.cmd`` etc., so a bare ``"codex"`` failed with ``exit 127``.
    These tests simulate Windows via monkeypatch; CI runs on Linux.
    """

    def test_bare_name_resolves_to_absolute_path_posix(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A bare name that shutil.which finds becomes its absolute path."""
        monkeypatch.setattr("bernstein.core.orchestration.worker.os.name", "posix")
        monkeypatch.setattr(
            "bernstein.core.orchestration.worker.shutil.which",
            lambda name: "/usr/local/bin/codex" if name == "codex" else None,
        )
        result = _resolve_launch_cmd(["codex", "exec", "-m", "gpt-5.5"])
        assert result == ["/usr/local/bin/codex", "exec", "-m", "gpt-5.5"]

    def test_windows_cmd_shim_wrapped_in_cmd_exe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """On nt, a resolved .cmd shim is routed through cmd.exe /c."""
        monkeypatch.setattr("bernstein.core.orchestration.worker.os.name", "nt")
        # Windows uses ``\\`` as sep and ``/`` as altsep.
        monkeypatch.setattr("bernstein.core.orchestration.worker.os.sep", "\\")
        monkeypatch.setattr("bernstein.core.orchestration.worker.os.altsep", "/")
        monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")
        monkeypatch.setattr(
            "bernstein.core.orchestration.worker.shutil.which",
            lambda name: r"C:\nvm4w\nodejs\codex.CMD" if name == "codex" else None,
        )
        result = _resolve_launch_cmd(["codex", "exec", "-m", "gpt-5.5"])
        assert result == [
            r"C:\Windows\System32\cmd.exe",
            "/c",
            r"C:\nvm4w\nodejs\codex.CMD",
            "exec",
            "-m",
            "gpt-5.5",
        ]

    def test_windows_bat_shim_wrapped_in_cmd_exe(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A .bat shim is treated like .cmd -- routed through cmd.exe."""
        monkeypatch.setattr("bernstein.core.orchestration.worker.os.name", "nt")
        monkeypatch.setattr("bernstein.core.orchestration.worker.os.sep", "\\")
        monkeypatch.setattr("bernstein.core.orchestration.worker.os.altsep", "/")
        monkeypatch.setenv("COMSPEC", r"C:\Windows\System32\cmd.exe")
        monkeypatch.setattr(
            "bernstein.core.orchestration.worker.shutil.which",
            lambda name: r"C:\tools\agent.bat" if name == "agent" else None,
        )
        result = _resolve_launch_cmd(["agent", "--go"])
        assert result[0] == r"C:\Windows\System32\cmd.exe"
        assert result[1] == "/c"
        assert result[2] == r"C:\tools\agent.bat"
        assert result[3:] == ["--go"]

    def test_windows_real_exe_launched_directly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A resolved .exe is a PE binary: launch it directly, no cmd.exe."""
        monkeypatch.setattr("bernstein.core.orchestration.worker.os.name", "nt")
        monkeypatch.setattr("bernstein.core.orchestration.worker.os.sep", "\\")
        monkeypatch.setattr("bernstein.core.orchestration.worker.os.altsep", "/")
        monkeypatch.setattr(
            "bernstein.core.orchestration.worker.shutil.which",
            lambda name: r"C:\Python\python.exe" if name == "python" else None,
        )
        result = _resolve_launch_cmd(["python", "-c", "pass"])
        assert result == [r"C:\Python\python.exe", "-c", "pass"]

    def test_already_absolute_path_left_untouched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """A path-qualified argv[0] is not re-resolved through shutil.which."""

        def _boom(_name: str) -> str:
            raise AssertionError("shutil.which must not be called for a path-qualified argv[0]")

        monkeypatch.setattr("bernstein.core.orchestration.worker.os.name", "posix")
        monkeypatch.setattr("bernstein.core.orchestration.worker.shutil.which", _boom)
        cmd = ["/usr/local/bin/codex", "exec"]
        assert _resolve_launch_cmd(cmd) == cmd

    def test_unresolved_bare_name_preserved(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """shutil.which returning None keeps the original so the caller's
        FileNotFoundError -> 'command not found' (exit 127) path still fires."""
        monkeypatch.setattr("bernstein.core.orchestration.worker.os.name", "posix")
        monkeypatch.setattr(
            "bernstein.core.orchestration.worker.shutil.which",
            lambda _name: None,
        )
        cmd = ["nonexistent_command_xyz", "--flag"]
        assert _resolve_launch_cmd(cmd) == cmd

    def test_windows_altsep_qualified_path_left_untouched(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """On Windows a forward-slash path (altsep) is also treated as
        qualified and not re-resolved."""

        def _boom(_name: str) -> str:
            raise AssertionError("shutil.which must not be called for a path-qualified argv[0]")

        monkeypatch.setattr("bernstein.core.orchestration.worker.os.name", "nt")
        monkeypatch.setattr("bernstein.core.orchestration.worker.os.sep", "\\")
        monkeypatch.setattr("bernstein.core.orchestration.worker.os.altsep", "/")
        monkeypatch.setattr("bernstein.core.orchestration.worker.shutil.which", _boom)
        cmd = ["C:/nvm4w/nodejs/codex.cmd", "exec"]
        assert _resolve_launch_cmd(cmd) == cmd

    def test_empty_cmd_returned_as_is(self) -> None:
        """Guard: an empty argv is returned unchanged (main() handles it)."""
        assert _resolve_launch_cmd([]) == []


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
                "bernstein.core.orchestration.worker",
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

    @pytest.mark.skipif(os.name == "nt", reason="POSIX process-group signals")
    def test_worker_cleans_pid_file_when_sigterm_races_startup(self, tmp_path: Path) -> None:
        """Regression (#2341): SIGTERM at startup must never leak the PID file.

        The worker must install its terminating-signal handler *before* the
        PID file exists, so no instant passes where the file is present
        without a cleanup path. Before the fix the handler was installed only
        after the child spawn; a SIGTERM landing in that window took the
        default disposition (terminate, no ``finally`` unlink) and leaked the
        PID file - a phantom ``bernstein ps`` entry and the intermittent
        cleanup-order failure this test guards.

        Each iteration sends a single SIGTERM the instant the PID file first
        appears (the widest point of the old window, before ``child_pid`` is
        even written) and asserts the file is always gone. On the buggy
        ordering this leaks on a large fraction of iterations; the fixed
        ordering cleans up every time.
        """
        for i in range(24):
            pid_dir = tmp_path / f"pids-{i}"
            proc = subprocess.Popen(
                [
                    sys.executable,
                    "-m",
                    "bernstein.core.orchestration.worker",
                    "--role",
                    "test",
                    "--session",
                    f"race-{i}",
                    "--pid-dir",
                    str(pid_dir),
                    "--",
                    "sleep",
                    "10",
                ],
                start_new_session=True,
            )
            pid_file = pid_dir / f"race-{i}.json"
            # Wait only until the PID file first appears - the earliest and
            # widest point of the startup race window.
            deadline = time.monotonic() + 5
            while not pid_file.exists() and time.monotonic() < deadline:
                time.sleep(0.0005)
            assert pid_file.exists(), f"iter {i}: PID file never appeared"

            # One group-directed SIGTERM at that instant, exactly as a reaper
            # would, then assert the worker cleaned up after itself.
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                raise
            time.sleep(0.2)
            assert not pid_file.exists(), (
                f"iter {i}: PID file leaked after SIGTERM at startup (worker exit={proc.returncode})"
            )

    def test_worker_forwards_signals(self, tmp_path: Path) -> None:
        """Worker should forward SIGTERM to child and exit."""
        pid_dir = tmp_path / "pids"
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "bernstein.core.orchestration.worker",
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
                "bernstein.core.orchestration.worker",
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
                "bernstein.core.orchestration.worker",
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

    def test_worker_exits_128_plus_signal_when_child_killed_by_sigterm(self, tmp_path: Path) -> None:
        """Regression: a signal-killed child must surface as ``128 + N``.

        Popen.wait returns ``-N`` when the child dies on signal N. The
        old worker passed that through ``sys.exit`` directly, which the
        runtime clamps to ``256 - N`` (e.g. SIGTERM -> 241). External
        supervisors that key on standard codes (sysexits, ``bernstein
        ps``, shell-style runners) then misread the result as an unknown
        failure rather than "killed by signal".

        We spawn a child that explicitly self-terminates via SIGTERM so
        the assertion is portable: 128 + SIGTERM (15) == 143 on every
        POSIX platform.
        """
        pid_dir = tmp_path / "pids"
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "bernstein.core.orchestration.worker",
                "--role",
                "test",
                "--session",
                "sig-exit-test",
                "--pid-dir",
                str(pid_dir),
                "--",
                sys.executable,
                "-c",
                "import os, signal; os.kill(os.getpid(), signal.SIGTERM)",
            ],
        )
        exit_code = proc.wait(timeout=10)
        assert exit_code == 128 + signal.SIGTERM
