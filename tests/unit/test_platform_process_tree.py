"""Process-tree lifecycle platform layer (issue #2367).

Covers the cross-platform process supervision surface:

* ``process_group_popen_kwargs`` - the deterministic projection of the
  current platform onto ``subprocess.Popen`` spawn keywords.
* ``reap_process_group`` - the TERM -> poll -> KILL escalation that returns
  a structured :class:`ProcessReapReceipt` instead of a bare bool, so every
  reap can be mirrored into the audit chain.
* ``WindowsJobObject`` - Job Object supervision primitives that replace
  POSIX process groups on Windows.  Inert on POSIX; exercised here through
  a mocked kernel32 so the Windows branches are covered on every OS.
* ``CLIAdapter.kill`` returning the reap receipt to its caller.
"""

from __future__ import annotations

import signal
import subprocess
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.platform_compat import (
    IS_WINDOWS,
    ProcessReapReceipt,
    WindowsJobObject,
    kill_process_group_graceful,
    process_group_alive,
    process_group_popen_kwargs,
    reap_process_group,
)

_PC = "bernstein.core.config.platform_compat"


# ---------------------------------------------------------------------------
# process_group_popen_kwargs
# ---------------------------------------------------------------------------


class TestProcessGroupPopenKwargs:
    def test_posix_uses_start_new_session(self) -> None:
        if IS_WINDOWS:
            pytest.skip("POSIX projection asserted under mock below")
        assert process_group_popen_kwargs() == {"start_new_session": True}

    @patch(f"{_PC}.IS_WINDOWS", True)
    def test_windows_uses_new_process_group_flag(self) -> None:
        kwargs = process_group_popen_kwargs()
        assert "start_new_session" not in kwargs
        expected = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200)
        assert kwargs == {"creationflags": expected}

    @patch(f"{_PC}.IS_WINDOWS", False)
    def test_projection_is_deterministic(self) -> None:
        """Two calls on the same platform produce identical kwargs."""
        assert process_group_popen_kwargs() == process_group_popen_kwargs()

    def test_kwargs_accepted_by_popen(self) -> None:
        """The projected kwargs must spawn a real process on this platform."""
        import sys

        proc = subprocess.Popen(
            [sys.executable, "-c", "pass"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **process_group_popen_kwargs(),  # type: ignore[arg-type]
        )
        assert proc.wait(timeout=30) == 0


# ---------------------------------------------------------------------------
# reap_process_group -> ProcessReapReceipt
# ---------------------------------------------------------------------------


class TestReapProcessGroup:
    def test_invalid_pgid_receipt(self) -> None:
        receipt = reap_process_group(0)
        assert receipt.delivered is False
        assert receipt.escalated is False
        assert receipt.pgid == 0

    def test_undeliverable_term_receipt(self) -> None:
        with patch(f"{_PC}.kill_process_group", return_value=False) as mock_kpg:
            receipt = reap_process_group(12345)
        assert receipt.delivered is False
        assert receipt.escalated is False
        mock_kpg.assert_called_once_with(12345, signal.SIGTERM)

    def test_clean_exit_receipt(self) -> None:
        alive = iter([True, False])
        with (
            patch(f"{_PC}.kill_process_group", return_value=True) as mock_kpg,
            patch(f"{_PC}.process_group_alive", side_effect=lambda _pid: next(alive, False)),
        ):
            receipt = reap_process_group(12345, grace_seconds=1.0, poll_interval=0.01)
        assert receipt.delivered is True
        assert receipt.escalated is False
        assert mock_kpg.call_count == 1

    def test_escalation_receipt(self) -> None:
        with (
            patch(f"{_PC}.kill_process_group", return_value=True) as mock_kpg,
            patch(f"{_PC}.process_group_alive", return_value=True),
        ):
            receipt = reap_process_group(12345, grace_seconds=0.05, poll_interval=0.01)
        assert receipt.delivered is True
        assert receipt.escalated is True
        assert mock_kpg.call_count == 2
        # First TERM, then the platform force-kill signal.
        assert mock_kpg.call_args_list[0].args == (12345, signal.SIGTERM)

    def test_escalates_when_group_alive_after_leader_reaped(self) -> None:
        """Leader reaped but a surviving child keeps the group alive.

        A session leader can exit while a child it spawned survives; the
        process group is still alive (``os.killpg(pgid, 0)`` succeeds) even
        though the lead PID is gone.  The reap must probe the whole group and
        escalate to SIGKILL rather than declaring the group dead off the lead
        PID and leaking the surviving tree (issue #2643).
        """

        def _killpg(_pgid: int, _sig: int) -> None:
            # Signal 0 is the group-liveness probe: the group is still alive.
            # SIGTERM/SIGKILL deliveries "succeed" (no raise) too.
            return None

        with (
            patch(f"{_PC}.os.killpg", side_effect=_killpg) as mock_killpg,
            # Lead PID is already reaped; a lead-PID-only check stops here.
            patch(f"{_PC}.process_alive", return_value=False),
        ):
            receipt = reap_process_group(4321, grace_seconds=0.05, poll_interval=0.01)

        assert receipt.delivered is True
        assert receipt.escalated is True
        forced = [c for c in mock_killpg.call_args_list if c.args[1] in (signal.SIGKILL, 9)]
        assert forced, "expected a SIGKILL escalation delivered to the surviving group"

    def test_receipt_details_projection(self) -> None:
        """to_details() is a deterministic dict projection of the receipt."""
        receipt = ProcessReapReceipt(
            pgid=42,
            os_name="linux",
            method="posix_process_group",
            delivered=True,
            escalated=False,
            grace_seconds=3.0,
        )
        details = receipt.to_details()
        assert details == {
            "pgid": 42,
            "os_name": "linux",
            "method": "posix_process_group",
            "delivered": True,
            "escalated": False,
            "grace_seconds": 3.0,
        }
        # Frozen dataclass: two identical receipts project identically.
        assert (
            details
            == ProcessReapReceipt(
                pgid=42,
                os_name="linux",
                method="posix_process_group",
                delivered=True,
                escalated=False,
                grace_seconds=3.0,
            ).to_details()
        )

    def test_receipt_method_names_platform(self) -> None:
        with patch(f"{_PC}.kill_process_group", return_value=False):
            receipt = reap_process_group(999)
        if IS_WINDOWS:
            assert receipt.method == "windows_process_tree"
        else:
            assert receipt.method == "posix_process_group"

    def test_graceful_wrapper_matches_receipt(self) -> None:
        """kill_process_group_graceful stays a bool projection of the receipt."""
        with patch(f"{_PC}.kill_process_group", return_value=False):
            assert kill_process_group_graceful(12345) is False
        alive = iter([False])
        with (
            patch(f"{_PC}.kill_process_group", return_value=True),
            patch(f"{_PC}.process_group_alive", side_effect=lambda _pid: next(alive, False)),
        ):
            assert kill_process_group_graceful(12345, grace_seconds=0.1, poll_interval=0.01) is True


# ---------------------------------------------------------------------------
# process_group_alive
# ---------------------------------------------------------------------------


class TestProcessGroupAlive:
    def test_nonpositive_pgid_is_dead(self) -> None:
        assert process_group_alive(0) is False
        assert process_group_alive(-1) is False

    def test_group_alive_when_killpg_succeeds(self) -> None:
        with patch(f"{_PC}.os.killpg", return_value=None) as mock_killpg:
            assert process_group_alive(4321) is True
        mock_killpg.assert_called_once_with(4321, 0)

    def test_group_dead_on_esrch(self) -> None:
        # ProcessLookupError is the ESRCH mapping: no members left in the group.
        with patch(f"{_PC}.os.killpg", side_effect=ProcessLookupError):
            assert process_group_alive(4321) is False

    def test_group_alive_on_eperm(self) -> None:
        # PermissionError (EPERM) means a process exists we may not signal.
        with patch(f"{_PC}.os.killpg", side_effect=PermissionError):
            assert process_group_alive(4321) is True

    def test_generic_oserror_is_dead(self) -> None:
        with patch(f"{_PC}.os.killpg", side_effect=OSError):
            assert process_group_alive(4321) is False

    def test_windows_falls_back_to_pid_check(self) -> None:
        with (
            patch(f"{_PC}.IS_WINDOWS", True),
            patch(f"{_PC}.process_alive", return_value=True) as mock_alive,
        ):
            assert process_group_alive(4321) is True
        mock_alive.assert_called_once_with(4321)


# ---------------------------------------------------------------------------
# WindowsJobObject
# ---------------------------------------------------------------------------


class TestWindowsJobObject:
    def test_unavailable_on_posix(self) -> None:
        if IS_WINDOWS:
            pytest.skip("availability asserted on POSIX")
        assert WindowsJobObject.available() is False

    def test_create_raises_on_posix(self) -> None:
        if IS_WINDOWS:
            pytest.skip("POSIX misuse guard")
        job = WindowsJobObject()
        with pytest.raises(RuntimeError):
            job.create()

    @patch(f"{_PC}.IS_WINDOWS", True)
    def test_create_configures_kill_on_close(self) -> None:
        kernel32 = MagicMock()
        kernel32.CreateJobObjectW.return_value = 1234
        kernel32.SetInformationJobObject.return_value = 1
        with patch(f"{_PC}._win_kernel32", return_value=kernel32):
            job = WindowsJobObject()
            assert job.create() is True
        kernel32.CreateJobObjectW.assert_called_once()
        kernel32.SetInformationJobObject.assert_called_once()

    @patch(f"{_PC}.IS_WINDOWS", True)
    def test_create_failure_returns_false(self) -> None:
        kernel32 = MagicMock()
        kernel32.CreateJobObjectW.return_value = 0
        with patch(f"{_PC}._win_kernel32", return_value=kernel32):
            job = WindowsJobObject()
            assert job.create() is False

    @patch(f"{_PC}.IS_WINDOWS", True)
    def test_assign_opens_and_assigns_process(self) -> None:
        kernel32 = MagicMock()
        kernel32.CreateJobObjectW.return_value = 1234
        kernel32.SetInformationJobObject.return_value = 1
        kernel32.OpenProcess.return_value = 555
        kernel32.AssignProcessToJobObject.return_value = 1
        with patch(f"{_PC}._win_kernel32", return_value=kernel32):
            job = WindowsJobObject()
            assert job.create() is True
            assert job.assign(4321) is True
        kernel32.OpenProcess.assert_called_once()
        kernel32.AssignProcessToJobObject.assert_called_once_with(1234, 555)
        kernel32.CloseHandle.assert_called_with(555)

    @patch(f"{_PC}.IS_WINDOWS", True)
    def test_assign_without_create_returns_false(self) -> None:
        kernel32 = MagicMock()
        with patch(f"{_PC}._win_kernel32", return_value=kernel32):
            job = WindowsJobObject()
            assert job.assign(4321) is False
        kernel32.OpenProcess.assert_not_called()

    @patch(f"{_PC}.IS_WINDOWS", True)
    def test_terminate_kills_whole_tree(self) -> None:
        kernel32 = MagicMock()
        kernel32.CreateJobObjectW.return_value = 1234
        kernel32.SetInformationJobObject.return_value = 1
        kernel32.TerminateJobObject.return_value = 1
        with patch(f"{_PC}._win_kernel32", return_value=kernel32):
            job = WindowsJobObject()
            assert job.create() is True
            assert job.terminate() is True
        kernel32.TerminateJobObject.assert_called_once()
        assert kernel32.TerminateJobObject.call_args.args[0] == 1234

    @patch(f"{_PC}.IS_WINDOWS", True)
    def test_close_releases_handle(self) -> None:
        kernel32 = MagicMock()
        kernel32.CreateJobObjectW.return_value = 1234
        kernel32.SetInformationJobObject.return_value = 1
        with patch(f"{_PC}._win_kernel32", return_value=kernel32):
            job = WindowsJobObject()
            assert job.create() is True
            job.close()
            # Second close is a no-op.
            job.close()
        kernel32.CloseHandle.assert_called_once_with(1234)

    @patch(f"{_PC}.IS_WINDOWS", True)
    def test_context_manager_closes(self) -> None:
        kernel32 = MagicMock()
        kernel32.CreateJobObjectW.return_value = 1234
        kernel32.SetInformationJobObject.return_value = 1
        with patch(f"{_PC}._win_kernel32", return_value=kernel32):
            with WindowsJobObject() as job:
                assert job.create() is True
        kernel32.CloseHandle.assert_called_once_with(1234)


# ---------------------------------------------------------------------------
# CLIAdapter.kill returns the reap receipt
# ---------------------------------------------------------------------------


class TestAdapterKillReceipt:
    def test_base_kill_returns_receipt(self) -> None:
        from bernstein.adapters.base import CLIAdapter

        class _Stub(CLIAdapter):
            def spawn(self, *args: object, **kwargs: object) -> object:  # type: ignore[override]
                raise NotImplementedError

            def name(self) -> str:
                return "stub"

        with patch(f"{_PC}.kill_process_group", return_value=False):
            receipt = _Stub().kill(31337)
        assert isinstance(receipt, ProcessReapReceipt)
        assert receipt.pgid == 31337
        assert receipt.delivered is False
