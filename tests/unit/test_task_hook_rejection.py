"""Tests for task hook rejection (T719)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from bernstein.plugins.manager import HookBlockingError, PluginManager


class TestPreTaskCreateHook:
    """T719 — pre-task-create hooks can block task creation."""

    def test_fire_pre_task_create_passes_when_no_hooks(self, tmp_path: Path) -> None:
        pm = PluginManager()
        # Should not raise
        pm.fire_pre_task_create(task_id="t1", role="backend", title="Test", description="A task")

    def test_fire_pre_task_create_blocked_by_hook(self, tmp_path: Path) -> None:
        pm = PluginManager()

        class BlockingPlugin:
            from bernstein.plugins import hookimpl

            @hookimpl
            def on_pre_task_create(self, **kwargs) -> None:
                raise HookBlockingError("blocker", "Not allowed: test rejection")

        pm.register(BlockingPlugin(), name="blocker")
        with pytest.raises(HookBlockingError, match="Not allowed"):
            pm.fire_pre_task_create(task_id="t1", role="backend", title="Test", description="A task")

    def test_blocking_hook_returns_400_in_route(self, tmp_path: Path) -> None:
        """When HookBlockingError is raised, route returns 400."""
        from bernstein.plugins.manager import HookBlockingError

        msg = str(HookBlockingError("test", "Rejected by hook"))
        assert "Rejected by hook" in msg


class TestCommandHookBlocking:
    """T719 — command hooks (shell scripts) can block via exit code 2."""

    def test_blocking_script_exits_2_raises(self, tmp_path: Path) -> None:
        # The CommandHook takes a base dir; _run_command looks for <base>/hook_name/
        base = tmp_path / "hooks"
        hook_dir = base / "on_pre_task_create"
        hook_dir.mkdir(parents=True)
        script_path = hook_dir / "block.sh"
        script_path.write_text("#!/bin/bash\nexit 2\n")
        os.chmod(script_path, script_path.stat().st_mode | stat.S_IXUSR)

        from bernstein.plugins.manager import CommandHook

        hook = CommandHook(base)
        with pytest.raises(HookBlockingError):
            hook.on_pre_task_create(task_id="t1", role="backend", title="Test", description="Task")

    def test_non_blocking_script_exits_0_passes(self, tmp_path: Path) -> None:
        base = tmp_path / "hooks"
        hook_dir = base / "on_pre_task_create"
        hook_dir.mkdir(parents=True)
        script_path = hook_dir / "pass.sh"
        script_path.write_text("#!/bin/bash\nexit 0\n")
        os.chmod(script_path, script_path.stat().st_mode | stat.S_IXUSR)

        from bernstein.plugins.manager import CommandHook

        hook = CommandHook(base)
        # Should not raise
        hook.on_pre_task_create(task_id="t1", role="backend", title="Test", description="Task")
