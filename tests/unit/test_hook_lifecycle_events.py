"""Tests for T681 — 22 new hook lifecycle events (hookspecs + fire methods)."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from bernstein.plugins.hookspecs import BernsteinSpec
from bernstein.plugins.manager import (
    CommandHook,
    HookBlockingError,
    PluginManager,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def plugin_mgr(tmp_path: Path) -> PluginManager:
    """PluginManager with no plugins loaded."""
    manager = PluginManager(workdir=tmp_path)
    return manager


# ---------------------------------------------------------------------------
# HookSpec existence tests
# ---------------------------------------------------------------------------


class TestHookSpecExists:
    """Verify all 22 new hook specs are defined on BernsteinSpec."""

    @pytest.mark.parametrize(
        "hook_name",
        [
            "on_pre_tool_use",
            "on_post_tool_use",
            "on_post_tool_use_failure",
            "on_notification",
            "on_user_prompt_submit",
            "on_session_start",
            "on_session_end",
            "on_stop",
            "on_stop_failure",
            "on_subagent_start",
            "on_subagent_stop",
            "on_permission_request",
            "on_setup",
            "on_teammate_idle",
            "on_elicitation",
            "on_elicitation_result",
            "on_config_change",
            "on_worktree_create",
            "on_worktree_remove",
            "on_instructions_loaded",
            "on_cwd_changed",
            "on_file_changed",
        ],
    )
    def test_hook_spec_defined(self, hook_name: str) -> None:
        assert hasattr(BernsteinSpec, hook_name), f"{hook_name} should exist on BernsteinSpec"


class TestFirstResultHooks:
    """Verify hooks marked firstresult=True (blocking semantics) by inspecting source."""

    def test_on_pre_tool_use_is_firstresult(self) -> None:
        import inspect
        src = inspect.getsource(BernsteinSpec.on_pre_tool_use)
        assert "firstresult" in src

    def test_on_permission_denied_is_firstresult(self) -> None:
        import inspect
        src = inspect.getsource(BernsteinSpec.on_permission_denied)
        assert "firstresult" in src


# ---------------------------------------------------------------------------
# Fire method tests — verify PluginManager has fire_* for each new hook
# ---------------------------------------------------------------------------


class TestFireMethodsExist:
    """Verify all fire_* methods exist on PluginManager."""

    @pytest.mark.parametrize(
        "method_name",
        [
            "fire_pre_tool_use",
            "fire_post_tool_use",
            "fire_post_tool_use_failure",
            "fire_notification",
            "fire_user_prompt_submit",
            "fire_session_start",
            "fire_session_end",
            "fire_stop",
            "fire_stop_failure",
            "fire_subagent_start",
            "fire_subagent_stop",
            "fire_permission_request",
            "fire_setup",
            "fire_teammate_idle",
            "fire_elicitation",
            "fire_elicitation_result",
            "fire_config_change",
            "fire_worktree_create",
            "fire_worktree_remove",
            "fire_instructions_loaded",
            "fire_cwd_changed",
            "fire_file_changed",
        ],
    )
    def test_fire_method_exists(self, plugin_mgr: PluginManager, method_name: str) -> None:
        assert hasattr(plugin_mgr, method_name), f"{method_name} should exist on PluginManager"
        assert callable(getattr(plugin_mgr, method_name))


# ---------------------------------------------------------------------------
# Fire method dispatch tests — no crash, no errors
# ---------------------------------------------------------------------------


class TestFireMethodDispatch:
    """Test fire_* methods dispatch correctly without plugins (no crash)."""

    def test_fire_pre_tool_use_no_plugins(self, plugin_mgr: PluginManager) -> None:
        result = plugin_mgr.fire_pre_tool_use("sess1", "Bash", {"command": "echo hello"})
        assert result is None

    def test_fire_post_tool_use_no_plugins(self, plugin_mgr: PluginManager) -> None:
        plugin_mgr.fire_post_tool_use("sess1", "Bash", {"command": "ls"}, "file1.txt", True)

    def test_fire_post_tool_use_failure_no_plugins(self, plugin_mgr: PluginManager) -> None:
        plugin_mgr.fire_post_tool_use_failure("sess1", "Bash", {"command": "rm -rf"}, "error", 3)

    def test_fire_notification_no_plugins(self, plugin_mgr: PluginManager) -> None:
        plugin_mgr.fire_notification("sess1", "info", "Task started")

    def test_fire_session_start_no_plugins(self, plugin_mgr: PluginManager) -> None:
        plugin_mgr.fire_session_start("sess1", "backend", "task123")

    def test_fire_session_end_no_plugins(self, plugin_mgr: PluginManager) -> None:
        plugin_mgr.fire_session_end("sess1", "backend", "completed")

    def test_fire_stop_no_plugins(self, plugin_mgr: PluginManager) -> None:
        plugin_mgr.fire_stop("sess1", "user_initiated")

    def test_fire_stop_failure_no_plugins(self, plugin_mgr: PluginManager) -> None:
        plugin_mgr.fire_stop_failure("sess1", "user_initiated", "process stuck")

    def test_fire_cwd_changed_no_plugins(self, plugin_mgr: PluginManager) -> None:
        plugin_mgr.fire_cwd_changed("sess1", "/old", "/new")

    def test_fire_file_changed_no_plugins(self, plugin_mgr: PluginManager) -> None:
        plugin_mgr.fire_file_changed("sess1", "src/foo.py", "modified")


# ---------------------------------------------------------------------------
# CommandHook script invocation tests
# ---------------------------------------------------------------------------


class TestCommandHookScripts:
    """Test CommandHook discovers and runs hook scripts from .bernstein/hooks/."""

    def _write_hook_script(self, hooks_dir: Path, hook_name: str, exit_code: int, stdout: str = "") -> Path:
        """Create an executable hook script in the hook directory."""
        script_dir = hooks_dir / hook_name
        script_dir.mkdir(parents=True, exist_ok=True)
        script = script_dir / "test_hook.sh"
        script.write_text(f"#!/bin/bash\nexit {exit_code}\n")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        return script

    def _write_hook_script_json(self, hooks_dir: Path, hook_name: str, exit_code: int, stdout: str) -> Path:
        """Create an executable hook script that outputs JSON."""
        script_dir = hooks_dir / hook_name
        script_dir.mkdir(parents=True, exist_ok=True)
        script = script_dir / "test_hook.sh"
        script.write_text(f"#!/bin/bash\nprintf '%s' '{json.dumps(stdout)}'\nexit {exit_code}\n")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        return script

    def test_pre_tool_use_script_exit_0(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / ".bernstein" / "hooks"
        self._write_hook_script(hooks_dir, "on_pre_tool_use", 0)

        cmd = CommandHook(hooks_dir)
        cmd.on_pre_tool_use("sess1", "Bash", {"command": "echo hi"})

    def test_pre_tool_use_script_exit_2_blocks(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / ".bernstein" / "hooks"
        self._write_hook_script(hooks_dir, "on_pre_tool_use", 2)

        cmd = CommandHook(hooks_dir)
        with pytest.raises(HookBlockingError) as excinfo:
            cmd.on_pre_tool_use("sess1", "Bash", {"command": "echo hi"})
        assert excinfo.value.hook_name == "on_pre_tool_use"

    def test_pre_tool_use_json_error_message(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / ".bernstein" / "hooks"
        script_dir = hooks_dir / "on_pre_tool_use"
        script_dir.mkdir(parents=True)

        script = script_dir / "test_hook.sh"
        script.write_text('#!/bin/bash\nprintf \'{"status":"error","message":"Policy violation"}\'\nexit 2\n')
        script.chmod(script.stat().st_mode | stat.S_IEXEC)

        cmd = CommandHook(hooks_dir)
        with pytest.raises(HookBlockingError) as excinfo:
            cmd.on_pre_tool_use("sess1", "Bash", {"command": "echo hi"})
        assert "Policy violation" in str(excinfo.value)

    def test_post_tool_use_script_runs(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / ".bernstein" / "hooks"
        self._write_hook_script(hooks_dir, "on_post_tool_use", 0)
        cmd = CommandHook(hooks_dir)
        cmd.on_post_tool_use("sess1", "Bash", {"command": "ls"}, "output.txt", True)

    def test_session_start_script_runs(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / ".bernstein" / "hooks"
        self._write_hook_script(hooks_dir, "on_session_start", 0)
        cmd = CommandHook(hooks_dir)
        cmd.on_session_start("sess1", "backend", "task123")

    def test_session_end_script_runs(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / ".bernstein" / "hooks"
        self._write_hook_script(hooks_dir, "on_session_end", 0)
        cmd = CommandHook(hooks_dir)
        cmd.on_session_end("sess1", "backend", "completed")

    def test_cwd_changed_script_runs(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / ".bernstein" / "hooks"
        self._write_hook_script(hooks_dir, "on_cwd_changed", 0)
        cmd = CommandHook(hooks_dir)
        cmd.on_cwd_changed("sess1", "/old", "/new")

    def test_file_changed_script_runs(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / ".bernstein" / "hooks"
        self._write_hook_script(hooks_dir, "on_file_changed", 0)
        cmd = CommandHook(hooks_dir)
        cmd.on_file_changed("sess1", "src/foo.py", "modified")

    def test_stop_script_runs(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / ".bernstein" / "hooks"
        self._write_hook_script(hooks_dir, "on_stop", 0)
        cmd = CommandHook(hooks_dir)
        cmd.on_stop("sess1", "user_initiated")

    def test_stop_failure_script_runs(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / ".bernstein" / "hooks"
        self._write_hook_script(hooks_dir, "on_stop_failure", 0)
        cmd = CommandHook(hooks_dir)
        cmd.on_stop_failure("sess1", "user_initiated", "process stuck")

    def test_subagent_start_script_runs(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / ".bernstein" / "hooks"
        self._write_hook_script(hooks_dir, "on_subagent_start", 0)
        cmd = CommandHook(hooks_dir)
        cmd.on_subagent_start("sess1", "sub1", "qa")

    def test_subagent_stop_script_runs(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / ".bernstein" / "hooks"
        self._write_hook_script(hooks_dir, "on_subagent_stop", 0)
        cmd = CommandHook(hooks_dir)
        cmd.on_subagent_stop("sess1", "sub1", "completed")

    def test_permission_request_script_runs(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / ".bernstein" / "hooks"
        self._write_hook_script(hooks_dir, "on_permission_request", 0)
        cmd = CommandHook(hooks_dir)
        cmd.on_permission_request("sess1", "Write", "allow")

    def test_setup_script_runs(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / ".bernstein" / "hooks"
        self._write_hook_script(hooks_dir, "on_setup", 0)
        cmd = CommandHook(hooks_dir)
        cmd.on_setup("sess1", "backend", str(tmp_path))

    def test_teammate_idle_script_runs(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / ".bernstein" / "hooks"
        self._write_hook_script(hooks_dir, "on_teammate_idle", 0)
        cmd = CommandHook(hooks_dir)
        cmd.on_teammate_idle("sess1", "backend", 5)

    def test_elicitation_script_runs(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / ".bernstein" / "hooks"
        self._write_hook_script(hooks_dir, "on_elicitation", 0)
        cmd = CommandHook(hooks_dir)
        cmd.on_elicitation("sess1", "Which model?", ["opus", "sonnet"])

    def test_elicitation_result_script_runs(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / ".bernstein" / "hooks"
        self._write_hook_script(hooks_dir, "on_elicitation_result", 0)
        cmd = CommandHook(hooks_dir)
        cmd.on_elicitation_result("sess1", "Which model?", "sonnet")

    def test_notification_script_runs(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / ".bernstein" / "hooks"
        self._write_hook_script(hooks_dir, "on_notification", 0)
        cmd = CommandHook(hooks_dir)
        cmd.on_notification("sess1", "warn", "High memory usage")

    def test_user_prompt_submit_script_runs(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / ".bernstein" / "hooks"
        self._write_hook_script(hooks_dir, "on_user_prompt_submit", 0)
        cmd = CommandHook(hooks_dir)
        cmd.on_user_prompt_submit("sess1", "Do the thing")

    def test_config_change_script_runs(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / ".bernstein" / "hooks"
        self._write_hook_script(hooks_dir, "on_config_change", 0)
        cmd = CommandHook(hooks_dir)
        cmd.on_config_change("model", "sonnet", "opus")

    def test_worktree_create_script_runs(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / ".bernstein" / "hooks"
        self._write_hook_script(hooks_dir, "on_worktree_create", 0)
        cmd = CommandHook(hooks_dir)
        cmd.on_worktree_create("sess1", "/tmp/wt", "fix/bug")

    def test_worktree_remove_script_runs(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / ".bernstein" / "hooks"
        self._write_hook_script(hooks_dir, "on_worktree_remove", 0)
        cmd = CommandHook(hooks_dir)
        cmd.on_worktree_remove("sess1", "/tmp/wt")

    def test_instructions_loaded_script_runs(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / ".bernstein" / "hooks"
        self._write_hook_script(hooks_dir, "on_instructions_loaded", 0)
        cmd = CommandHook(hooks_dir)
        cmd.on_instructions_loaded("sess1", "backend", ["/path/AGENTS.md"])

    def test_post_tool_use_failure_script_runs(self, tmp_path: Path) -> None:
        hooks_dir = tmp_path / ".bernstein" / "hooks"
        self._write_hook_script(hooks_dir, "on_post_tool_use_failure", 0)
        cmd = CommandHook(hooks_dir)
        cmd.on_post_tool_use_failure("sess1", "Bash", {"command": "bad"}, "error msg", 3)

    def test_non_zero_exit_warns(self, tmp_path: Path) -> None:
        """Exit code other than 0 or 2 should warn but not raise."""
        hooks_dir = tmp_path / ".bernstein" / "hooks"
        self._write_hook_script(hooks_dir, "on_post_tool_use", 1)
        cmd = CommandHook(hooks_dir)
        # Should not raise -- just logs warning
        cmd.on_post_tool_use("sess1", "Bash", {"command": "ls"}, "out", True)
