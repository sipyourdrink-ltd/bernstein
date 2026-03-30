"""Unit tests for adapter timeout watchdog functionality."""

from __future__ import annotations

import signal
import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bernstein.adapters.base import CLIAdapter, SpawnResult
from bernstein.adapters.codex import CodexAdapter
from bernstein.adapters.gemini import GeminiAdapter
from bernstein.adapters.generic import GenericAdapter
from bernstein.core.models import ModelConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_popen_mock(pid: int) -> MagicMock:
    m = MagicMock()
    m.pid = pid
    return m


def _spawn_with_timeout(
    adapter: CLIAdapter,
    tmp_path: Path,
    timeout: int,
    popen_path: str,
    session_id: str = "timeout-test",
) -> SpawnResult:
    """Helper to spawn an adapter with a given timeout, mocking Popen."""
    proc_mock = _make_popen_mock(pid=42)
    with patch(popen_path, return_value=proc_mock):
        return adapter.spawn(
            prompt="do work",
            workdir=tmp_path,
            model_config=ModelConfig(model="gpt-4o", effort="high"),
            session_id=session_id,
            timeout_seconds=timeout,
        )


# ---------------------------------------------------------------------------
# SpawnResult.timeout_timer is set
# ---------------------------------------------------------------------------


class TestTimeoutTimerSet:
    """Verify spawn() sets a timeout_timer on SpawnResult when timeout > 0."""

    @pytest.mark.parametrize(
        "adapter_factory,popen_path",
        [
            (lambda: CodexAdapter(), "bernstein.adapters.codex.subprocess.Popen"),
            (lambda: GeminiAdapter(), "bernstein.adapters.gemini.subprocess.Popen"),
            (
                lambda: GenericAdapter(cli_command="mytool"),
                "bernstein.adapters.generic.subprocess.Popen",
            ),
        ],
        ids=["codex", "gemini", "generic"],
    )
    def test_timer_is_set(self, adapter_factory: object, popen_path: str, tmp_path: Path) -> None:
        adapter = adapter_factory()  # type: ignore[operator]
        result = _spawn_with_timeout(adapter, tmp_path, timeout=1800, popen_path=popen_path)
        assert result.timeout_timer is not None
        assert isinstance(result.timeout_timer, threading.Timer)
        result.timeout_timer.cancel()

    @pytest.mark.parametrize(
        "adapter_factory,popen_path",
        [
            (lambda: CodexAdapter(), "bernstein.adapters.codex.subprocess.Popen"),
            (lambda: GeminiAdapter(), "bernstein.adapters.gemini.subprocess.Popen"),
            (
                lambda: GenericAdapter(cli_command="mytool"),
                "bernstein.adapters.generic.subprocess.Popen",
            ),
        ],
        ids=["codex", "gemini", "generic"],
    )
    def test_timer_not_set_when_zero(self, adapter_factory: object, popen_path: str, tmp_path: Path) -> None:
        adapter = adapter_factory()  # type: ignore[operator]
        result = _spawn_with_timeout(adapter, tmp_path, timeout=0, popen_path=popen_path)
        assert result.timeout_timer is None


# ---------------------------------------------------------------------------
# cancel_timeout()
# ---------------------------------------------------------------------------


class TestCancelTimeout:
    """CLIAdapter.cancel_timeout() stops the timer and clears the reference."""

    def test_cancel_stops_timer(self, tmp_path: Path) -> None:
        adapter = CodexAdapter()
        result = _spawn_with_timeout(
            adapter, tmp_path, timeout=1800, popen_path="bernstein.adapters.codex.subprocess.Popen"
        )
        assert result.timeout_timer is not None
        CLIAdapter.cancel_timeout(result)
        assert result.timeout_timer is None

    def test_cancel_on_no_timer_is_noop(self) -> None:
        result = SpawnResult(pid=1, log_path=Path("/dev/null"))
        CLIAdapter.cancel_timeout(result)  # must not raise
        assert result.timeout_timer is None


# ---------------------------------------------------------------------------
# Watchdog kill sequence: SIGTERM → wait → SIGKILL
# ---------------------------------------------------------------------------


class TestWatchdogKillSequence:
    """Verify the timeout watchdog sends SIGTERM, then SIGKILL after grace period."""

    def test_sigterm_then_sigkill(self, tmp_path: Path) -> None:
        """When process stays alive after SIGTERM, watchdog escalates to SIGKILL."""
        adapter = CodexAdapter()

        # Use a very short timeout so the test finishes quickly
        with (
            patch("bernstein.adapters.base.os.getpgid", return_value=42),
            patch("bernstein.adapters.base.os.killpg") as mock_killpg,
            # os.kill(pid, 0) succeeds (process alive) for first checks, then dies
            patch("bernstein.adapters.base.os.kill") as mock_kill,
            # Shorten the grace period to 1 second for test speed
            patch("bernstein.adapters.base._SIGTERM_GRACE_SECONDS", 1),
            patch("bernstein.adapters.base.time.sleep"),
            patch("bernstein.adapters.base.time.monotonic") as mock_monotonic,
        ):
            # Simulate time progression: start=0, first check=0.5 (within grace),
            # second check=2.0 (past grace of 1s)
            mock_monotonic.side_effect = [0.0, 0.5, 2.0]
            # Process stays alive after SIGTERM
            mock_kill.return_value = None

            timer = adapter._start_timeout_watchdog(pid=42, timeout_seconds=0, session_id="test-kill")
            # Timer fires immediately with timeout=0; wait for it
            timer.join(timeout=5)

            # Should have called SIGTERM then SIGKILL
            assert mock_killpg.call_count == 2
            mock_killpg.assert_any_call(42, signal.SIGTERM)
            mock_killpg.assert_any_call(42, signal.SIGKILL)

    def test_sigterm_only_when_process_exits(self, tmp_path: Path) -> None:
        """When process exits after SIGTERM, no SIGKILL is sent."""
        adapter = CodexAdapter()

        with (
            patch("bernstein.adapters.base.os.getpgid", return_value=99),
            patch("bernstein.adapters.base.os.killpg") as mock_killpg,
            patch("bernstein.adapters.base.os.kill", side_effect=OSError("no such process")),
            patch("bernstein.adapters.base.time.sleep"),
            patch("bernstein.adapters.base.time.monotonic", return_value=0.0),
        ):
            timer = adapter._start_timeout_watchdog(pid=99, timeout_seconds=0, session_id="test-exit")
            timer.join(timeout=5)

            # Only SIGTERM, no SIGKILL
            mock_killpg.assert_called_once_with(99, signal.SIGTERM)

    def test_already_dead_before_sigterm(self, tmp_path: Path) -> None:
        """When process is already dead, SIGTERM OSError is suppressed."""
        adapter = GeminiAdapter()

        with (
            patch("bernstein.adapters.base.os.getpgid", side_effect=OSError("no such process")),
            patch("bernstein.adapters.base.os.killpg", side_effect=OSError("no such process")),
        ):
            timer = adapter._start_timeout_watchdog(pid=777, timeout_seconds=0, session_id="test-dead")
            timer.join(timeout=5)  # must not raise


# ---------------------------------------------------------------------------
# Watchdog logging
# ---------------------------------------------------------------------------


class TestWatchdogLogging:
    """Verify structured warnings are logged on timeout."""

    def test_logs_timeout_warning(self, tmp_path: Path) -> None:
        adapter = CodexAdapter()
        logged: list[tuple[str, tuple[object, ...]]] = []

        # Capture log calls from the real logger
        def _capture_warning(msg: str, *args: object) -> None:
            logged.append((msg, args))

        with (
            patch("bernstein.adapters.base.os.getpgid", return_value=50),
            patch("bernstein.adapters.base.os.killpg"),
            patch("bernstein.adapters.base.os.kill", side_effect=OSError),
            patch("bernstein.adapters.base.time.sleep"),
            patch("bernstein.adapters.base.time.monotonic", return_value=0.0),
            patch.object(
                __import__("bernstein.adapters.base", fromlist=["logger"]).logger,
                "warning",
                side_effect=_capture_warning,
            ),
        ):
            # timeout_seconds=0 fires timer immediately
            timer = adapter._start_timeout_watchdog(pid=50, timeout_seconds=0, session_id="log-test")
            timer.join(timeout=5)

        assert len(logged) >= 1
        # First warning should mention the timeout and session
        fmt = logged[0][0] % logged[0][1]
        assert "log-test" in fmt
        assert "pid=50" in fmt


# ---------------------------------------------------------------------------
# Timer is daemon (doesn't prevent process exit)
# ---------------------------------------------------------------------------


class TestTimerProperties:
    """Verify timer thread properties."""

    def test_timer_is_daemon(self, tmp_path: Path) -> None:
        adapter = CodexAdapter()
        result = _spawn_with_timeout(
            adapter, tmp_path, timeout=9999, popen_path="bernstein.adapters.codex.subprocess.Popen"
        )
        assert result.timeout_timer is not None
        assert result.timeout_timer.daemon is True
        result.timeout_timer.cancel()

    def test_timer_name_contains_session_id(self, tmp_path: Path) -> None:
        adapter = CodexAdapter()
        result = _spawn_with_timeout(
            adapter,
            tmp_path,
            timeout=9999,
            popen_path="bernstein.adapters.codex.subprocess.Popen",
            session_id="my-session-xyz",
        )
        assert result.timeout_timer is not None
        assert "my-session-xyz" in result.timeout_timer.name
        result.timeout_timer.cancel()
