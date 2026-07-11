"""Windows-branch coverage for the process-lifecycle platform layer.

These tests exercise the Windows code paths of
``bernstein.core.config.platform_compat`` on any host by substituting a
mock ``kernel32`` / ``subprocess.run`` and flipping ``IS_WINDOWS``. They
carry no platform skip marker: the Windows branches are pure logic once
the kernel calls are mocked, so parity coverage no longer waits for a
Windows runner.

The reap-receipt cases assert the *verifiable artifact* itself: the
:class:`ProcessReapReceipt` is a deterministic projection of the platform
onto ``(os_name, method, delivered, escalated)``. On Windows the force
tier must fall back to the numeric ``9`` code because ``SIGKILL`` does not
exist, and the receipt must record ``windows_process_tree`` as the
mechanism. Stripping that projection is what a Windows regression looks
like, so we pin it here rather than only on a Windows host.
"""

from __future__ import annotations

import ctypes
import signal
import subprocess
from typing import Any

import bernstein.core.config.platform_compat as pc

# ---------------------------------------------------------------------------
# kill_process - Windows branch
# ---------------------------------------------------------------------------


class TestKillProcessWindowsBranch:
    """Windows dispatch of kill_process onto taskkill / os.kill."""

    def test_sigterm_routes_to_taskkill_no_force(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        calls: list[tuple[int, bool, bool]] = []

        def _fake(pid: int, *, force: bool = False, tree: bool = False) -> bool:
            calls.append((pid, force, tree))
            return True

        monkeypatch.setattr(pc, "_win_taskkill", _fake)
        assert pc.kill_process(4321, signal.SIGTERM) is True
        assert calls == [(4321, False, False)]

    def test_sigkill_code_routes_to_forced_taskkill(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        calls: list[tuple[int, bool, bool]] = []

        def _fake(pid: int, *, force: bool = False, tree: bool = False) -> bool:
            calls.append((pid, force, tree))
            return True

        monkeypatch.setattr(pc, "_win_taskkill", _fake)
        # 9 == SIGKILL numeric value; SIGKILL is not importable on Windows.
        assert pc.kill_process(4321, 9) is True
        assert calls == [(4321, True, False)]

    def test_other_signal_falls_back_to_os_kill(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        seen: list[tuple[int, int]] = []
        monkeypatch.setattr(pc.os, "kill", lambda pid, sig: seen.append((pid, sig)))
        # 2 is neither SIGTERM(15) nor the SIGKILL numeric (9).
        assert pc.kill_process(4321, 2) is True
        assert seen == [(4321, 2)]

    def test_other_signal_oserror_returns_false(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(pc, "IS_WINDOWS", True)

        def _boom(pid: int, sig: int) -> None:
            raise OSError("no such process")

        monkeypatch.setattr(pc.os, "kill", _boom)
        assert pc.kill_process(4321, 2) is False

    def test_nonpositive_pid_short_circuits(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        # Must return False without ever touching taskkill.
        monkeypatch.setattr(
            pc,
            "_win_taskkill",
            lambda *a, **k: (_ for _ in ()).throw(AssertionError("called")),
        )
        assert pc.kill_process(0) is False
        assert pc.kill_process(-1) is False


# ---------------------------------------------------------------------------
# kill_process_group - Windows branch
# ---------------------------------------------------------------------------


class TestKillProcessGroupWindowsBranch:
    """Windows kill_process_group maps onto a taskkill tree termination."""

    def test_term_kills_tree_without_force(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        calls: list[tuple[int, bool, bool]] = []

        def _fake(pid: int, *, force: bool = False, tree: bool = False) -> bool:
            calls.append((pid, force, tree))
            return True

        monkeypatch.setattr(pc, "_win_taskkill", _fake)
        assert pc.kill_process_group(777, signal.SIGTERM) is True
        assert calls == [(777, False, True)]

    def test_kill_code_forces_tree_termination(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        calls: list[tuple[int, bool, bool]] = []

        def _fake(pid: int, *, force: bool = False, tree: bool = False) -> bool:
            calls.append((pid, force, tree))
            return True

        monkeypatch.setattr(pc, "_win_taskkill", _fake)
        assert pc.kill_process_group(777, 9) is True
        assert calls == [(777, True, True)]

    def test_nonpositive_pgid_returns_false(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        assert pc.kill_process_group(0) is False
        assert pc.kill_process_group(-5) is False


# ---------------------------------------------------------------------------
# _win_taskkill - taskkill primary path + PowerShell fallback
# ---------------------------------------------------------------------------


class _Result:
    def __init__(self, returncode: int) -> None:
        self.returncode = returncode
        self.stdout = ""
        self.stderr = ""


class TestWinTaskkill:
    """The taskkill primary path and the PowerShell verify-fallback."""

    def test_taskkill_success_skips_fallback(self, monkeypatch: Any) -> None:
        cmds: list[list[str]] = []

        def _run(cmd: list[str], **_kw: Any) -> _Result:
            cmds.append(cmd)
            return _Result(0)

        monkeypatch.setattr(pc.subprocess, "run", _run)
        assert pc._win_taskkill(123, force=True, tree=True) is True
        # Exactly one invocation: taskkill, no PowerShell fallback.
        assert len(cmds) == 1
        assert cmds[0][0] == "taskkill"
        assert "/F" in cmds[0] and "/T" in cmds[0]
        assert cmds[0][-2:] == ["/PID", "123"]

    def test_taskkill_failure_triggers_powershell_verify(self, monkeypatch: Any) -> None:
        cmds: list[list[str]] = []

        def _run(cmd: list[str], **_kw: Any) -> _Result:
            cmds.append(cmd)
            return _Result(1)  # taskkill and powershell both "ran"

        monkeypatch.setattr(pc.subprocess, "run", _run)
        # After the fallback, liveness says the process is gone -> success.
        monkeypatch.setattr(pc, "_win_process_alive", lambda pid: False)
        assert pc._win_taskkill(123) is True
        assert [c[0] for c in cmds] == ["taskkill", "powershell"]

    def test_powershell_fallback_still_alive_returns_false(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(pc.subprocess, "run", lambda cmd, **_kw: _Result(1))
        monkeypatch.setattr(pc, "_win_process_alive", lambda pid: True)
        assert pc._win_taskkill(123) is False

    def test_taskkill_timeout_falls_through_to_powershell(self, monkeypatch: Any) -> None:
        cmds: list[list[str]] = []

        def _run(cmd: list[str], **_kw: Any) -> _Result:
            cmds.append(cmd)
            if cmd[0] == "taskkill":
                raise subprocess.TimeoutExpired(cmd, 5)
            return _Result(0)

        monkeypatch.setattr(pc.subprocess, "run", _run)
        monkeypatch.setattr(pc, "_win_process_alive", lambda pid: False)
        assert pc._win_taskkill(123) is True
        assert [c[0] for c in cmds] == ["taskkill", "powershell"]

    def test_powershell_oserror_returns_false(self, monkeypatch: Any) -> None:
        def _run(cmd: list[str], **_kw: Any) -> _Result:
            if cmd[0] == "taskkill":
                return _Result(1)
            raise OSError("powershell missing")

        monkeypatch.setattr(pc.subprocess, "run", _run)
        assert pc._win_taskkill(123) is False


# ---------------------------------------------------------------------------
# _win_process_alive - kernel32 liveness probe
# ---------------------------------------------------------------------------


class _FakeKernel32:
    def __init__(self, handle: int, exit_code: int, get_ok: bool = True) -> None:
        self._handle = handle
        self._exit_code = exit_code
        self._get_ok = get_ok
        self.closed: list[int] = []
        self.opened: list[tuple[int, bool, int]] = []

    def OpenProcess(self, access: int, inherit: bool, pid: int) -> int:
        self.opened.append((access, inherit, pid))
        return self._handle

    def GetExitCodeProcess(self, handle: int, ptr: Any) -> int:
        if self._get_ok:
            ptr._obj.value = self._exit_code
            return 1
        return 0

    def CloseHandle(self, handle: int) -> None:
        self.closed.append(handle)


def _install_windll(monkeypatch: Any, fake: _FakeKernel32) -> None:
    class _Windll:
        kernel32 = fake

    monkeypatch.setattr(ctypes, "windll", _Windll(), raising=False)


class TestWinProcessAlive:
    """kernel32 OpenProcess + GetExitCodeProcess liveness projection."""

    def test_null_handle_is_dead(self, monkeypatch: Any) -> None:
        fake = _FakeKernel32(handle=0, exit_code=0)
        _install_windll(monkeypatch, fake)
        assert pc._win_process_alive(4321) is False
        # No handle opened means nothing to close.
        assert fake.closed == []

    def test_still_active_is_alive(self, monkeypatch: Any) -> None:
        fake = _FakeKernel32(handle=99, exit_code=259)  # 259 == STILL_ACTIVE
        _install_windll(monkeypatch, fake)
        assert pc._win_process_alive(4321) is True
        assert fake.closed == [99]

    def test_exited_process_is_dead(self, monkeypatch: Any) -> None:
        fake = _FakeKernel32(handle=99, exit_code=0)
        _install_windll(monkeypatch, fake)
        assert pc._win_process_alive(4321) is False
        assert fake.closed == [99]

    def test_get_exit_code_failure_is_dead(self, monkeypatch: Any) -> None:
        fake = _FakeKernel32(handle=99, exit_code=259, get_ok=False)
        _install_windll(monkeypatch, fake)
        assert pc._win_process_alive(4321) is False
        # Handle still closed on the failure path.
        assert fake.closed == [99]

    def test_process_alive_delegates_on_windows(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "_win_process_alive", lambda pid: True)
        assert pc.process_alive(4321) is True

    def test_process_alive_nonpositive_skips_probe(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(
            pc,
            "_win_process_alive",
            lambda pid: (_ for _ in ()).throw(AssertionError("probed")),
        )
        assert pc.process_alive(0) is False
        assert pc.process_alive(-3) is False


# ---------------------------------------------------------------------------
# reap_process_group - Windows-branch receipt projection (the artifact)
# ---------------------------------------------------------------------------


class TestReapReceiptWindowsProjection:
    """The reap receipt is a deterministic projection of the Windows path."""

    def test_clean_exit_projects_windows_tree_method(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "_detect_os_name", lambda: "windows")
        monkeypatch.setattr(pc, "kill_process_group", lambda pgid, sig=signal.SIGTERM: True)
        monkeypatch.setattr(pc, "process_alive", lambda pid: False)

        receipt = pc.reap_process_group(999, grace_seconds=0.05, poll_interval=0.01)

        assert receipt.os_name == "windows"
        assert receipt.method == "windows_process_tree"
        assert receipt.delivered is True
        assert receipt.escalated is False
        details = receipt.to_details()
        assert details["os_name"] == "windows"
        assert details["method"] == "windows_process_tree"

    def test_escalation_uses_numeric_kill_code(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "_detect_os_name", lambda: "windows")
        sigs: list[int] = []

        def _kpg(pgid: int, sig: int = signal.SIGTERM) -> bool:
            sigs.append(sig)
            return True

        monkeypatch.setattr(pc, "kill_process_group", _kpg)
        # Never dies -> reap must escalate.
        monkeypatch.setattr(pc, "process_alive", lambda pid: True)

        receipt = pc.reap_process_group(999, grace_seconds=0.02, poll_interval=0.01)

        assert receipt.method == "windows_process_tree"
        assert receipt.escalated is True
        # First tier is SIGTERM; force tier degrades to numeric 9 because
        # SIGKILL is not importable when IS_WINDOWS is set.
        assert sigs[0] == signal.SIGTERM
        assert sigs[-1] == 9

    def test_undeliverable_term_projects_not_delivered(self, monkeypatch: Any) -> None:
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "_detect_os_name", lambda: "windows")
        monkeypatch.setattr(pc, "kill_process_group", lambda pgid, sig=signal.SIGTERM: False)
        monkeypatch.setattr(
            pc,
            "process_alive",
            lambda pid: (_ for _ in ()).throw(AssertionError("polled")),
        )

        receipt = pc.reap_process_group(999, grace_seconds=0.05, poll_interval=0.01)

        assert receipt.os_name == "windows"
        assert receipt.method == "windows_process_tree"
        assert receipt.delivered is False
        assert receipt.escalated is False
