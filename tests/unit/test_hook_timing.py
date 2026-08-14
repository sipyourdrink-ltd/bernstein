"""Tests for hook execution timing and outcome logging (T479)."""

from __future__ import annotations

import logging
import time
from pathlib import Path

import pytest

from bernstein.plugins import hookimpl
from bernstein.plugins.manager import HookBlockingError, PluginManager


@pytest.fixture(autouse=True)
def _trusted_workspace(monkeypatch: pytest.MonkeyPatch) -> None:
    """Exercise hook-execution mechanics against a trusted workspace.

    Trust gating (untrusted / indeterminate workspaces) is owned by
    tests/unit/test_plugins.py; these suites assume trust has been granted.
    """
    monkeypatch.setattr("bernstein.plugins.manager.is_workspace_trusted", lambda _root: True)


class _NoopPlugin:
    """Test plugin that does nothing - used for timing tests."""

    @hookimpl
    def on_task_created(self, task_id: str, role: str, title: str) -> None:
        pass  # Intentionally empty: testing hook timing, not behavior


class TestHookTiming:
    """Tests for hook execution timing and outcome logging."""

    def test_successful_hook_logs_debug_duration(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """Successful hook execution logs debug-level duration."""
        pm = PluginManager(workdir=tmp_path)
        pm.register(_NoopPlugin(), name="noop")

        caplog.set_level(logging.DEBUG)
        pm.fire_task_created(task_id="t1", role="backend", title="Test")

        log_messages = [r.message for r in caplog.records]
        timing_msgs = [m for m in log_messages if "Hook " in m and "duration=" in m]
        assert len(timing_msgs) >= 1
        assert "outcome=success" in timing_msgs[0]
        assert "foreground" in timing_msgs[0]

    def test_failing_hook_logs_exception_outcome(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """Failing hook logs warning with exception in message."""

        class FailingPlugin:
            @hookimpl
            def on_task_created(self, task_id: str, role: str, title: str) -> None:
                raise ValueError("plugin bug")

        pm = PluginManager(workdir=tmp_path)
        pm.register(FailingPlugin(), name="failing")

        pm.fire_task_created(task_id="t1", role="backend", title="Test")

        log_messages = [r.message for r in caplog.records]
        raised = [m for m in log_messages if "raised an exception" in m]
        assert len(raised) >= 1
        assert "plugin bug" in raised[0]

    def test_slow_hook_logs_warning(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        """Slow hook (above threshold) logs a warning with timing detail."""

        class SlowPlugin:
            @hookimpl
            def on_task_created(self, task_id: str, role: str, title: str) -> None:
                time.sleep(0.05)  # 50ms - small but measurable

        pm = PluginManager(workdir=tmp_path)
        pm.register(SlowPlugin(), name="slow")

        import bernstein.plugins.manager as mgr_mod

        old_threshold = mgr_mod.SLOW_HOOK_THRESHOLD
        try:
            mgr_mod.SLOW_HOOK_THRESHOLD = 0.01  # 10ms threshold
            caplog.set_level(logging.WARNING)
            pm.fire_task_created(task_id="t1", role="backend", title="Test")
        finally:
            mgr_mod.SLOW_HOOK_THRESHOLD = old_threshold

        log_messages = [r.message for r in caplog.records]
        slow_msgs = [m for m in log_messages if "Slow hook" in m]
        assert len(slow_msgs) >= 1
        assert "on_task_created" in slow_msgs[0]
        assert "foreground" in slow_msgs[0]

    def test_hook_blocking_error_propagates(self, tmp_path: Path) -> None:
        """Blocking errors are re-raised, not swallowed."""

        class BlockingPlugin:
            @hookimpl
            def on_task_created(self, task_id: str, role: str, title: str) -> None:
                raise HookBlockingError("on_task_created", "blocked by policy")

        pm = PluginManager(workdir=tmp_path)
        pm.register(BlockingPlugin(), name="blocking")

        with pytest.raises(HookBlockingError) as exc_info:
            pm.fire_task_created(task_id="t1", role="backend", title="Test")
        assert exc_info.value.hook_name == "on_task_created"
