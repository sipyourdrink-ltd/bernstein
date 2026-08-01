"""``bernstein stop --force`` must not leave an orphaned watchdog alive (issue #3312).

The process-scan fallback (`hard_stop`'s source D, `_collect_repo_processes`)
is supposed to catch a watchdog whose pidfile was overwritten by a later run
or was never written. On Windows that fallback silently excluded the watchdog
(and the orchestrator/server) from every sweep, for two independent reasons:

1. ``_list_process_snapshots_windows`` queried ``Get-Process | Select
   ... Path`` -- the *executable* path (``python.exe``), never the actual
   command line -- so none of ``_classify_repo_process``'s argv markers
   (``--watchdog``, the heartbeat/worktree path prefixes) could ever match.
2. Even with the command line visible, ``_classify_repo_process`` required
   ``process_cwd(pid) == workdir`` for the watchdog/orchestrator/server kinds.
   ``process_cwd`` shells out to ``lsof``, which does not exist on Windows, so
   it always returns ``None`` there and the check always failed.

The fix makes ``_start_watchdog`` write ``--workdir <path>`` into its own
argv and teaches ``_classify_repo_process`` to read watchdog ownership
straight from that marker, without a cwd probe, plus fixes the Windows
snapshot probe to capture the real command line.

These tests reproduce the enumeration gap platform-independently by forcing
``process_cwd`` to always return ``None`` -- exactly what happens for real on
Windows -- rather than requiring an actual Windows host.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from bernstein.cli.commands import stop_cmd
from bernstein.core.orchestration import bootstrap


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_dead(pid: int, timeout: float = 6.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _alive(pid):
            return True
        time.sleep(0.05)
    return not _alive(pid)


def _wait_dead_direct_child(proc: subprocess.Popen[bytes], timeout: float = 6.0) -> bool:
    """Reap a stub spawned directly by this test via ``Popen.wait``.

    ``proc`` is a direct child of the test process, so a killed-but-unreaped
    child becomes a zombie: it still exists in the process table and
    ``os.kill(pid, 0)`` keeps succeeding on it until something calls
    ``wait()``. A negative return code confirms the process was terminated
    by a signal rather than having exited on its own.
    """
    try:
        return proc.wait(timeout=timeout) < 0
    except subprocess.TimeoutExpired:
        return False


def _spawn_orphan_watchdog_stub() -> subprocess.Popen[bytes]:
    """A tiny real process standing in for an orphaned watchdog.

    Started detached (``start_new_session=True``), exactly like the real
    watchdog launched by ``_start_watchdog``, so it survives its "parent"
    (the test) the same way a real watchdog survives a crashed CLI process.
    We never actually run the watchdog module here -- the classifier only
    ever reads the *command-line string* we hand it via a crafted
    ``_ProcessSnapshot``, not the process's real argv.
    """
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        start_new_session=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


def _hard_cleanup(proc: subprocess.Popen[bytes]) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.killpg(proc.pid, 9)
    with contextlib.suppress(ProcessLookupError, PermissionError, OSError):
        os.kill(proc.pid, 9)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=3)


pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="uses POSIX process groups to spawn/verify the stub")


class TestStartWatchdogNamesItsOwnWorkdir:
    """``_start_watchdog`` must self-declare its workdir in argv (issue #3312)."""

    def test_argv_contains_workdir_flag(self, tmp_path: Path, monkeypatch: Any) -> None:
        captured: dict[str, list[str]] = {}

        class _FakeProc:
            def __init__(self) -> None:
                self.pid = 424242

            def wait(self, timeout: float | None = None) -> int:
                raise subprocess.TimeoutExpired(cmd="watchdog", timeout=timeout or 0.0)

        def _fake_popen(argv: list[str], **_kwargs: Any) -> _FakeProc:
            captured["argv"] = argv
            return _FakeProc()

        monkeypatch.setattr(bootstrap.subprocess, "Popen", _fake_popen)
        (tmp_path / ".sdd" / "runtime").mkdir(parents=True)

        bootstrap._start_watchdog(tmp_path, 8052)

        argv = captured["argv"]
        assert "--workdir" in argv, argv
        assert argv[argv.index("--workdir") + 1] == str(tmp_path)


class TestClassifyRepoProcessWatchdogWithoutCwdProbe:
    """``_classify_repo_process`` must attribute a watchdog via its argv marker."""

    def test_fails_without_workdir_marker_when_cwd_probe_is_unavailable(self, tmp_path: Path) -> None:
        """Pre-fix shape: no ``--workdir`` marker, cwd probe unavailable (Windows)."""
        snapshot = stop_cmd._ProcessSnapshot(
            pid=999,
            ppid=1,
            pgid=999,
            command=f"{sys.executable} -m bernstein.core.orchestration.bootstrap --watchdog --port 8052",
        )
        with contextlib.ExitStack() as stack:
            stack.enter_context(_force_process_cwd_unavailable())
            result = stop_cmd._classify_repo_process(
                snapshot, tmp_path, str(tmp_path / "heartbeats"), str(tmp_path / "worktrees")
            )
        assert result is None

    def test_succeeds_with_workdir_marker_when_cwd_probe_is_unavailable(self, tmp_path: Path) -> None:
        """Post-fix shape: ``--workdir`` marker present, cwd probe still unavailable."""
        snapshot = stop_cmd._ProcessSnapshot(
            pid=999,
            ppid=1,
            pgid=999,
            command=(
                f"{sys.executable} -m bernstein.core.orchestration.bootstrap "
                f"--watchdog --port 8052 --workdir {tmp_path}"
            ),
        )
        with contextlib.ExitStack() as stack:
            stack.enter_context(_force_process_cwd_unavailable())
            result = stop_cmd._classify_repo_process(
                snapshot, tmp_path, str(tmp_path / "heartbeats"), str(tmp_path / "worktrees")
            )
        assert result == ("infra", "Watchdog")


def _force_process_cwd_unavailable() -> Any:
    """Context manager simulating the real Windows behaviour: ``process_cwd`` always ``None``.

    ``process_cwd`` shells out to ``lsof``, absent on Windows, so every call
    returns ``None`` there regardless of the pid queried. Forcing that here
    reproduces the platform gap on any host.
    """
    import unittest.mock

    return unittest.mock.patch.object(stop_cmd, "process_cwd", return_value=None)


class TestCollectRepoProcessesReapsOrphanedWatchdog:
    """End-to-end: the reap-fallback sweep used by ``stop --force`` kills a real orphan."""

    def test_reaps_watchdog_via_argv_marker_when_pidfile_and_cwd_probe_are_both_gone(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        """Reproduces issue #3312: no pidfile for this watchdog, no cwd probe (Windows).

        Mirrors the reported scenario: an earlier run's watchdog is still
        alive but its pidfile was overwritten (or was never written) by a
        later run, and the host is Windows so the process-scan fallback's
        cwd cross-check can never confirm anything. Fails on unmodified code
        because ``_classify_repo_process`` has no argv-only path for the
        watchdog and therefore never classifies it.
        """
        monkeypatch.chdir(tmp_path)
        stub = _spawn_orphan_watchdog_stub()
        try:
            snapshot = stop_cmd._ProcessSnapshot(
                pid=stub.pid,
                ppid=1,
                pgid=stub.pid,
                command=(
                    f"{sys.executable} -m bernstein.core.orchestration.bootstrap "
                    f"--watchdog --port 8052 --workdir {tmp_path}"
                ),
            )
            monkeypatch.setattr(stop_cmd, "_list_process_snapshots", lambda: [snapshot])
            monkeypatch.setattr(stop_cmd, "process_cwd", lambda _pid: None)

            # No watchdog.pid on disk at all -- exactly the "pidfile belongs to
            # a run whose other state was already cleaned up" scenario named
            # in the issue.
            assert not (tmp_path / ".sdd" / "runtime" / "watchdog.pid").exists()

            killed: set[int] = set()
            stop_cmd._collect_repo_processes(killed)

            assert _wait_dead_direct_child(stub), "orphaned watchdog stub survived the reap-fallback sweep"
            assert stub.pid in killed
        finally:
            _hard_cleanup(stub)


class TestHardStopReapsOrphanedWatchdog:
    """``stop --force`` (``hard_stop``) itself must not leave the watchdog running."""

    def test_hard_stop_kills_orphaned_watchdog_with_no_pidfile_on_simulated_windows(
        self, tmp_path: Path, monkeypatch: Any
    ) -> None:
        monkeypatch.chdir(tmp_path)
        stub = _spawn_orphan_watchdog_stub()
        try:
            snapshot = stop_cmd._ProcessSnapshot(
                pid=stub.pid,
                ppid=1,
                pgid=stub.pid,
                command=(
                    f"{sys.executable} -m bernstein.core.orchestration.bootstrap "
                    f"--watchdog --port 8052 --workdir {tmp_path}"
                ),
            )
            monkeypatch.setattr(stop_cmd, "_list_process_snapshots", lambda: [snapshot])
            monkeypatch.setattr(stop_cmd, "process_cwd", lambda _pid: None)
            # Keep the sweep hermetic: no session server, no tunnels, no real
            # port holder on this host should be touched by the test.
            monkeypatch.setattr(stop_cmd, "save_session_on_stop", lambda _workdir: None)
            monkeypatch.setattr(stop_cmd, "stop_active_tunnels", lambda: 0)
            monkeypatch.setattr(stop_cmd, "_kill_port_holder", lambda *_a, **_k: None)

            stop_cmd.hard_stop()

            assert _wait_dead_direct_child(stub), "'bernstein stop --force' left the watchdog running"
        finally:
            _hard_cleanup(stub)


class TestParseWindowsProcessCsv:
    """The Windows snapshot probe must capture the full command line (issue #3312)."""

    def test_captures_full_command_line_not_just_executable_path(self) -> None:
        csv_text = (
            '"ProcessId","ParentProcessId","CommandLine"\n'
            '"4242","1","\\"C:\\\\Python312\\\\python.exe\\" -m '
            "bernstein.core.orchestration.bootstrap --watchdog --port 8052 "
            '--workdir \\"C:\\\\Users\\\\op\\\\proj\\""\n'
        )

        snapshots = stop_cmd._parse_windows_process_csv(csv_text)

        assert len(snapshots) == 1
        snap = snapshots[0]
        assert snap.pid == 4242
        assert snap.ppid == 1
        assert "bernstein.core.orchestration.bootstrap" in snap.command
        assert "--watchdog" in snap.command
        assert "--workdir" in snap.command

    def test_handles_commas_inside_quoted_command_line(self) -> None:
        """A naive ``split('","')`` mis-parses a CommandLine containing a comma."""
        csv_text = '"ProcessId","ParentProcessId","CommandLine"\n"10","1","python.exe --seed goal-a,goal-b"\n'

        snapshots = stop_cmd._parse_windows_process_csv(csv_text)

        assert len(snapshots) == 1
        assert snapshots[0].command == "python.exe --seed goal-a,goal-b"

    def test_empty_output_yields_no_snapshots(self) -> None:
        assert stop_cmd._parse_windows_process_csv("") == []

    def test_header_only_yields_no_snapshots(self) -> None:
        csv_text = '"ProcessId","ParentProcessId","CommandLine"\n'
        assert stop_cmd._parse_windows_process_csv(csv_text) == []
