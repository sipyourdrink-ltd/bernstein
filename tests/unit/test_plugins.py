"""Tests for the Bernstein plugin system."""

from __future__ import annotations

import warnings
from pathlib import Path
from typing import Any, cast
from unittest.mock import patch

import pytest

from bernstein.core.persistence.workspace import grant_workspace_trust
from bernstein.plugins import hookimpl
from bernstein.plugins.manager import PluginManager

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


class _CollectorPlugin:
    """Test plugin that records every hook call it receives."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    @hookimpl
    def on_task_created(self, task_id: str, role: str, title: str) -> None:
        self.calls.append(("on_task_created", {"task_id": task_id, "role": role, "title": title}))

    @hookimpl
    def on_task_completed(self, task_id: str, role: str, result_summary: str) -> None:
        self.calls.append(("on_task_completed", {"task_id": task_id, "role": role, "result_summary": result_summary}))

    @hookimpl
    def on_task_failed(self, task_id: str, role: str, error: str) -> None:
        self.calls.append(("on_task_failed", {"task_id": task_id, "role": role, "error": error}))

    @hookimpl
    def on_agent_spawned(self, session_id: str, role: str, model: str) -> None:
        self.calls.append(("on_agent_spawned", {"session_id": session_id, "role": role, "model": model}))

    @hookimpl
    def on_agent_reaped(self, session_id: str, role: str, outcome: str) -> None:
        self.calls.append(("on_agent_reaped", {"session_id": session_id, "role": role, "outcome": outcome}))

    @hookimpl
    def on_evolve_proposal(self, proposal_id: str, title: str, verdict: str) -> None:
        self.calls.append(("on_evolve_proposal", {"proposal_id": proposal_id, "title": title, "verdict": verdict}))


class _PartialPlugin:
    """Plugin that only implements a single hook."""

    def __init__(self) -> None:
        self.fired = False

    @hookimpl
    def on_task_completed(self, task_id: str, role: str, result_summary: str) -> None:
        self.fired = True


class _BrokenPlugin:
    """Plugin whose hook implementations always raise."""

    @hookimpl
    def on_task_created(self, task_id: str, role: str, title: str) -> None:
        raise RuntimeError("intentional test error")


@pytest.fixture()
def pm(tmp_path: Path) -> PluginManager:
    """Fresh PluginManager anchored to a trusted workspace.

    Hook dispatch is fail-closed: it only runs in a workspace whose trust has
    been granted.  These tests exercise dispatch mechanics, not the trust
    policy, so they run against an explicitly-trusted temp workspace.
    """
    grant_workspace_trust(tmp_path)
    return PluginManager(workdir=tmp_path)


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def test_manual_registration(pm: PluginManager) -> None:
    """Manually registered plugins appear in registered_names."""
    plugin = _CollectorPlugin()
    pm.register(plugin, name="collector")
    assert "collector" in pm.registered_names


def test_plugin_hooks_returns_implemented(pm: PluginManager) -> None:
    """plugin_hooks() returns the hook names implemented by a plugin."""
    plugin = _PartialPlugin()
    pm.register(plugin, name="partial")
    hooks = pm.plugin_hooks("partial")
    assert hooks == ["on_task_completed"]


def test_plugin_hooks_unknown_name(pm: PluginManager) -> None:
    """plugin_hooks() returns an empty list for unknown plugin names."""
    assert pm.plugin_hooks("nonexistent") == []


# ---------------------------------------------------------------------------
# Fire methods
# ---------------------------------------------------------------------------


def test_fire_task_created(pm: PluginManager) -> None:
    plugin = _CollectorPlugin()
    pm.register(plugin, name="c")

    pm.fire_task_created(task_id="t1", role="backend", title="Build auth")

    assert len(plugin.calls) == 1
    name, kwargs = plugin.calls[0]
    assert name == "on_task_created"
    assert kwargs == {"task_id": "t1", "role": "backend", "title": "Build auth"}


def test_fire_task_completed(pm: PluginManager) -> None:
    plugin = _CollectorPlugin()
    pm.register(plugin, name="c")

    pm.fire_task_completed(task_id="t2", role="qa", result_summary="All tests passed")

    assert len(plugin.calls) == 1
    name, kwargs = plugin.calls[0]
    assert name == "on_task_completed"
    assert kwargs["result_summary"] == "All tests passed"


def test_fire_task_failed(pm: PluginManager) -> None:
    plugin = _CollectorPlugin()
    pm.register(plugin, name="c")

    pm.fire_task_failed(task_id="t3", role="backend", error="ImportError")

    assert plugin.calls[0][0] == "on_task_failed"


def test_fire_agent_spawned(pm: PluginManager) -> None:
    plugin = _CollectorPlugin()
    pm.register(plugin, name="c")

    pm.fire_agent_spawned(session_id="s1", role="security", model="claude-sonnet")

    assert plugin.calls[0][0] == "on_agent_spawned"


def test_fire_agent_reaped(pm: PluginManager) -> None:
    plugin = _CollectorPlugin()
    pm.register(plugin, name="c")

    pm.fire_agent_reaped(session_id="s1", role="security", outcome="completed")

    assert plugin.calls[0][0] == "on_agent_reaped"


def test_fire_evolve_proposal(pm: PluginManager) -> None:
    plugin = _CollectorPlugin()
    pm.register(plugin, name="c")

    pm.fire_evolve_proposal(proposal_id="p1", title="Improve logging", verdict="accepted")

    assert plugin.calls[0][0] == "on_evolve_proposal"


# ---------------------------------------------------------------------------
# Partial-hook plugins - unimplemented hooks must not crash
# ---------------------------------------------------------------------------


def test_unimplemented_hooks_do_not_crash(pm: PluginManager) -> None:
    """Hooks not implemented by a plugin must be silently skipped."""
    plugin = _PartialPlugin()
    pm.register(plugin, name="partial")

    # These hooks are NOT implemented by _PartialPlugin - must not raise.
    pm.fire_task_created(task_id="x", role="r", title="t")
    pm.fire_task_failed(task_id="x", role="r", error="e")
    pm.fire_agent_spawned(session_id="s", role="r", model="m")
    pm.fire_agent_reaped(session_id="s", role="r", outcome="o")
    pm.fire_evolve_proposal(proposal_id="p", title="t", verdict="v")

    # The one implemented hook should still fire.
    pm.fire_task_completed(task_id="x", role="r", result_summary="ok")
    assert plugin.fired is True


# ---------------------------------------------------------------------------
# Broken plugins - exceptions must not propagate
# ---------------------------------------------------------------------------


def test_broken_plugin_does_not_crash_fire(pm: PluginManager) -> None:
    """An exception inside a plugin hook must be caught, not re-raised."""
    broken = _BrokenPlugin()
    pm.register(broken, name="broken")

    # Should not raise despite the plugin throwing RuntimeError internally.
    pm.fire_task_created(task_id="t", role="r", title="title")


# ---------------------------------------------------------------------------
# Entry-point discovery
# ---------------------------------------------------------------------------


def test_discover_entry_points_loads_plugin(pm: PluginManager) -> None:
    """Entry-point plugins are registered when discovered."""
    fake_ep = _make_fake_entry_point(name="test_ep", plugin=_CollectorPlugin())

    with patch("bernstein.plugins.manager.entry_points", return_value=[fake_ep]):
        pm.discover_entry_points()

    assert "test_ep" in pm.registered_names


def test_discover_entry_points_bad_ep_warns(pm: PluginManager) -> None:
    """A failing entry-point load emits a warning and does not crash."""

    class _BadEP:
        name = "bad_ep"
        value = "does.not.exist:Plugin"

        def load(self) -> None:
            raise ImportError("module not found")

    with patch("bernstein.plugins.manager.entry_points", return_value=[_BadEP()]):
        with warnings.catch_warnings(record=True) as w:
            warnings.simplefilter("always")
            pm.discover_entry_points()
        assert any("bad_ep" in str(x.message) for x in w)

    assert "bad_ep" not in pm.registered_names


# ---------------------------------------------------------------------------
# Config-plugin discovery
# ---------------------------------------------------------------------------


def test_discover_config_plugins(pm: PluginManager) -> None:
    """Config plugins specified as 'module:Class' strings are loaded."""
    pm.discover_config_plugins(["bernstein.plugins.manager:PluginManager"])
    # PluginManager itself is registered (not a useful plugin, but valid).
    assert "bernstein.plugins.manager:PluginManager" in pm.registered_names


def test_discover_config_plugins_bad_path_warns(pm: PluginManager) -> None:
    """A bad import path emits a warning and does not crash."""
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        pm.discover_config_plugins(["this.does.not.exist:Nope"])
    assert any("this.does.not.exist" in str(x.message) for x in w)


# ---------------------------------------------------------------------------
# bernstein.yaml integration
# ---------------------------------------------------------------------------


def test_load_from_workdir_reads_plugins_key(tmp_path: Path) -> None:
    """load_from_workdir() picks up plugins listed in bernstein.yaml."""
    yaml_content = "plugins:\n  - bernstein.plugins.manager:PluginManager\n"
    (tmp_path / "bernstein.yaml").write_text(yaml_content)

    local_pm = PluginManager()
    with patch("bernstein.plugins.manager.entry_points", return_value=[]):
        local_pm.load_from_workdir(tmp_path)

    assert "bernstein.plugins.manager:PluginManager" in local_pm.registered_names


def test_load_from_workdir_no_yaml(tmp_path: Path) -> None:
    """load_from_workdir() succeeds even when bernstein.yaml is absent."""
    local_pm = PluginManager()
    with patch("bernstein.plugins.manager.entry_points", return_value=[]):
        local_pm.load_from_workdir(tmp_path)  # no bernstein.yaml - must not raise
    assert local_pm.registered_names == []


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------


def test_plugins_cmd_no_plugins(tmp_path: Path) -> None:
    """bernstein plugins prints a helpful message when no plugins are found."""
    from click.testing import CliRunner

    from bernstein.cli.main import plugins_cmd

    runner = CliRunner()
    with patch("bernstein.plugins.manager.entry_points", return_value=[]):
        result = runner.invoke(plugins_cmd, ["--workdir", str(tmp_path)])

    assert result.exit_code == 0
    assert "No plugins" in result.output


def test_plugins_cmd_with_plugin(tmp_path: Path) -> None:
    """bernstein plugins lists registered plugins in a table."""
    import json as _json

    from click.testing import CliRunner

    from bernstein.cli.main import plugins_cmd

    # Create a plugin directory with meta.json so plugins_cmd discovers it
    plugin_dir = tmp_path / ".bernstein" / "plugins" / "logging_test"
    plugin_dir.mkdir(parents=True)
    (plugin_dir / "meta.json").write_text(_json.dumps({"version": "1.0", "type": "collector"}))

    runner = CliRunner()
    result = runner.invoke(plugins_cmd, ["--workdir", str(tmp_path)])

    assert result.exit_code == 0
    assert "logging_test" in result.output


# ---------------------------------------------------------------------------
# get_plugin_manager singleton
# ---------------------------------------------------------------------------


def test_get_plugin_manager_singleton() -> None:
    """get_plugin_manager returns the same instance on repeated calls."""
    from bernstein.plugins.manager import get_plugin_manager

    with patch("bernstein.plugins.manager.entry_points", return_value=[]):
        pm1 = get_plugin_manager(reload=True)
        pm2 = get_plugin_manager()
    assert pm1 is pm2


def test_get_plugin_manager_reload() -> None:
    """get_plugin_manager(reload=True) returns a fresh instance."""
    from bernstein.plugins.manager import get_plugin_manager

    with patch("bernstein.plugins.manager.entry_points", return_value=[]):
        pm1 = get_plugin_manager(reload=True)
        pm2 = get_plugin_manager(reload=True)
    assert pm1 is not pm2


class TestWorkspaceTrustGating:
    """Tests for workspace trust gating of hook execution (T456)."""

    def test_hooks_run_when_trusted(self, tmp_path: Path) -> None:
        """Hooks execute normally when workspace trust is granted."""
        import json as _json

        # Grant trust
        trust_dir = tmp_path / ".sdd" / "runtime"
        trust_dir.mkdir(parents=True)
        (trust_dir / "workspace_trust.json").write_text(
            _json.dumps({"trusted": True, "granted_by": "test", "granted_at": 0}),
            encoding="utf-8",
        )

        pm = PluginManager(workdir=tmp_path)
        plugin = _CollectorPlugin()
        pm.register(plugin, name="c")

        pm.fire_task_created(task_id="t1", role="backend", title="Build auth")

        assert len(plugin.calls) == 1
        assert plugin.calls[0][0] == "on_task_created"

    def test_hooks_skipped_when_untrusted(self, tmp_path: Path) -> None:
        """Hooks are no-ops when workspace is not trusted."""
        # No trust file - workspace is untrusted
        pm = PluginManager(workdir=tmp_path)
        plugin = _CollectorPlugin()
        pm.register(plugin, name="c")

        pm.fire_task_created(task_id="t1", role="backend", title="Build auth")

        # Must not have fired any hooks
        assert len(plugin.calls) == 0

    def test_hooks_gated_without_workdir(self) -> None:
        """Hooks are gated when the workspace root is indeterminate.

        A ``None`` workdir must fail closed rather than auto-trust: an
        indeterminate root cannot be shown to be trusted, so hooks do not run.
        """
        pm = PluginManager()
        assert pm.workdir is None
        plugin = _CollectorPlugin()
        pm.register(plugin, name="c")

        pm.fire_task_completed(task_id="t2", role="qa", result_summary="ok")

        assert len(plugin.calls) == 0

    def test_hooks_gated_when_cwd_untrusted(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Being *inside* a workspace does not by itself grant trust.

        The old short-circuit trusted any workspace equal to ``Path.cwd()``.
        That is the workspace-trust bypass: hooks must consult the recorded
        trust state even when the workdir is the current directory.
        """
        monkeypatch.chdir(tmp_path)
        pm = PluginManager(workdir=tmp_path)  # == Path.cwd(), but untrusted
        plugin = _CollectorPlugin()
        pm.register(plugin, name="c")

        pm.fire_task_completed(task_id="t2", role="qa", result_summary="ok")

        assert len(plugin.calls) == 0

    def test_fire_permission_denied_gated(self, tmp_path: Path) -> None:
        """fire_permission_denied returns None when trust is gated."""
        pm = PluginManager(workdir=tmp_path)  # untrusted

        result = pm.fire_permission_denied(
            task_id="t1",
            reason="blocked",
            tool="shell",
            args={"cmd": {"type": "#file/edit"}},
        )
        assert result is None

    def test_fire_permission_denied_trusted(self, tmp_path: Path) -> None:
        """fire_permission_denied runs hooks when trusted."""
        import json as _json

        trust_dir = tmp_path / ".sdd" / "runtime"
        trust_dir.mkdir(parents=True)
        (trust_dir / "workspace_trust.json").write_text(
            _json.dumps({"trusted": True, "granted_by": "test", "granted_at": 0}),
            encoding="utf-8",
        )

        class _HintPlugin:
            @hookimpl
            def on_permission_denied(self, task_id: str, reason: str, tool: str, args: dict[str, Any]) -> str:
                return "use safe command"

        pm = PluginManager(workdir=tmp_path)
        pm.register(_HintPlugin(), name="hint")

        result = pm.fire_permission_denied(
            task_id="t1",
            reason="blocked",
            tool="shell",
            args={"cmd": "#file/edit"},
        )
        assert result == "use safe command"


class TestCommittedHookExecutionGate:
    """End-to-end trust gate for committed ``.bernstein/hooks`` scripts.

    A committed hook script is local code-execution: it must run only when the
    workspace it lives in has been explicitly trusted.  These tests drive the
    real ``get_plugin_manager`` / ``subprocess`` path an operator would hit.
    """

    @staticmethod
    def _install_hook(workdir: Path, sentinel: Path) -> None:
        """Write a committed on_task_created hook that touches *sentinel*."""
        import os
        import stat

        hook_dir = workdir / ".bernstein" / "hooks" / "on_task_created"
        hook_dir.mkdir(parents=True)
        script = hook_dir / "touch.sh"
        script.write_text(f"#!/bin/sh\ntouch {sentinel}\n", encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        assert os.access(script, os.X_OK)

    @staticmethod
    def _set_trust(workdir: Path, *, trusted: bool) -> None:
        import json as _json

        trust_dir = workdir / ".sdd" / "runtime"
        trust_dir.mkdir(parents=True, exist_ok=True)
        (trust_dir / "workspace_trust.json").write_text(
            _json.dumps({"trusted": trusted, "granted_by": "test", "granted_at": 0}),
            encoding="utf-8",
        )

    def test_committed_hook_not_executed_when_untrusted(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A committed hook must NOT run when workspace_trust.json is false.

        Reproduces the production bug faithfully: the caller invokes
        ``get_plugin_manager()`` with *no* workdir while sitting inside the
        untrusted project (exactly what the task route did).  The side effect
        must never happen and the shell must never be invoked.
        """
        from bernstein.plugins.manager import get_plugin_manager

        sentinel = tmp_path / "hook_ran.marker"
        self._install_hook(tmp_path, sentinel)
        self._set_trust(tmp_path, trusted=False)
        monkeypatch.chdir(tmp_path)

        with (
            patch("bernstein.plugins.manager.subprocess.run") as mock_run,
            patch("bernstein.plugins.manager.entry_points", return_value=[]),
        ):
            pm = get_plugin_manager(reload=True)  # no workdir -> the vulnerable path
            pm.fire_task_created(task_id="t1", role="backend", title="Build auth")

        assert not sentinel.exists(), "untrusted committed hook must not run"
        mock_run.assert_not_called()

    def test_committed_hook_not_executed_when_untrusted_explicit_workdir(self, tmp_path: Path) -> None:
        """The exec-boundary gate also holds when the workdir is passed explicitly."""
        from bernstein.plugins.manager import get_plugin_manager

        sentinel = tmp_path / "hook_ran.marker"
        self._install_hook(tmp_path, sentinel)
        self._set_trust(tmp_path, trusted=False)

        with (
            patch("bernstein.plugins.manager.subprocess.run") as mock_run,
            patch("bernstein.plugins.manager.entry_points", return_value=[]),
        ):
            pm = get_plugin_manager(tmp_path, reload=True)
            pm.fire_task_created(task_id="t1", role="backend", title="Build auth")

        assert not sentinel.exists(), "untrusted committed hook must not run"
        mock_run.assert_not_called()

    def test_committed_hook_executed_when_trusted(self, tmp_path: Path) -> None:
        """The same committed hook DOES run once trust is granted (no regression)."""
        from bernstein.plugins.manager import get_plugin_manager

        sentinel = tmp_path / "hook_ran.marker"
        self._install_hook(tmp_path, sentinel)
        self._set_trust(tmp_path, trusted=True)

        with patch("bernstein.plugins.manager.entry_points", return_value=[]):
            pm = get_plugin_manager(tmp_path, reload=True)
            pm.fire_task_created(task_id="t1", role="backend", title="Build auth")

        assert sentinel.exists(), "trusted committed hook must run"

    def test_get_plugin_manager_receives_real_workdir(self, tmp_path: Path) -> None:
        """get_plugin_manager on the hook path anchors to a real workdir, not None."""
        from bernstein.plugins.manager import get_plugin_manager

        with patch("bernstein.plugins.manager.entry_points", return_value=[]):
            pm = get_plugin_manager(tmp_path, reload=True)

        assert pm.workdir == tmp_path

    def test_concrete_workdir_supersedes_indeterminate_singleton(self, tmp_path: Path) -> None:
        """A workdir-less first caller must not pin the singleton to an untrusted root.

        Mirrors production ordering: an internal caller (e.g. guardrails) builds
        the singleton with no workdir, then a request-scoped caller supplies the
        real project root.  The later concrete workdir must win so trust is
        evaluated against the real tree.
        """
        from bernstein.plugins.manager import get_plugin_manager

        sentinel = tmp_path / "hook_ran.marker"
        self._install_hook(tmp_path, sentinel)
        self._set_trust(tmp_path, trusted=True)

        with patch("bernstein.plugins.manager.entry_points", return_value=[]):
            first = get_plugin_manager(reload=True)  # no workdir (internal caller)
            second = get_plugin_manager(tmp_path)  # real project root (request path)
            second.fire_task_created(task_id="t1", role="backend", title="Build auth")

        assert second.workdir == tmp_path
        assert first is not second
        assert sentinel.exists(), "hook must run against the adopted, trusted workdir"


# ---------------------------------------------------------------------------
# Plugin subsystem isolation
# ---------------------------------------------------------------------------


def test_command_hooks_failure_doesnt_break_config_plugins(tmp_path: Path) -> None:
    """Command hooks subsystem failure does not prevent config plugins from loading."""
    yaml_content = "plugins:\n  - bernstein.plugins.manager:PluginManager\n"
    (tmp_path / "bernstein.yaml").write_text(yaml_content)

    from bernstein.plugins.manager import CommandHook

    with patch("bernstein.plugins.manager.CommandHook", side_effect=RuntimeError("hooks exploded")):
        with patch("bernstein.plugins.manager.entry_points", return_value=[]):
            # Create a hooks dir so _load_command_hooks_subsystem tries to load
            hooks_dir = tmp_path / ".bernstein" / "hooks"
            hooks_dir.mkdir(parents=True)
            local_pm = PluginManager()
            local_pm.load_from_workdir(tmp_path)

    # Config plugin must still be registered despite CommandHook failure
    assert "bernstein.plugins.manager:PluginManager" in local_pm.registered_names
    _ = CommandHook  # silence unused import warning


def test_config_plugins_failure_doesnt_break_entry_points(tmp_path: Path) -> None:
    """Config plugins subsystem failure does not prevent entry-point plugins from loading."""
    fake_ep = _make_fake_entry_point(name="test_ep", plugin=_CollectorPlugin())

    def _raise_yaml_broken(_root: object) -> None:
        raise RuntimeError("yaml broken")

    with patch("bernstein.plugins.manager.entry_points", return_value=[fake_ep]):
        local_pm = PluginManager()
        # Simulate yaml loading failure for config plugins subsystem
        local_pm._load_config_plugins_subsystem = _raise_yaml_broken  # type: ignore[assignment]
        local_pm.load_from_workdir(tmp_path)

    # Entry-point plugin must still be registered
    assert "test_ep" in local_pm.registered_names


# ---------------------------------------------------------------------------
# Plugin-provided MCP servers (collect_plugin_mcp_servers)
# ---------------------------------------------------------------------------


class _MCPServerPlugin:
    """Test plugin that provides MCP servers via provide_mcp_servers."""

    def __init__(self, servers: list[dict[str, Any]]) -> None:
        self._servers = servers

    @hookimpl
    def provide_mcp_servers(self) -> list[dict[str, Any]]:
        return self._servers


def test_collect_plugin_mcp_servers_namespaces(pm: PluginManager) -> None:
    """Servers from provide_mcp_servers are namespaced with the plugin name."""
    from bernstein.core.mcp_registry import MCPRegistry

    plugin = _MCPServerPlugin([{"name": "db", "package": "my-db-pkg"}])
    pm.register(plugin, name="acme")

    registry = MCPRegistry(config_path=None)
    pm.collect_plugin_mcp_servers(registry)

    config = registry.build_mcp_config(registry.servers)
    assert config is not None
    assert "acme__db" in config["mcpServers"]
    assert "db" not in config["mcpServers"]


def test_collect_plugin_mcp_servers_two_plugins_no_collision(pm: PluginManager) -> None:
    """Two plugins providing the same server name don't collide after namespacing."""
    from bernstein.core.mcp_registry import MCPRegistry

    pm.register(_MCPServerPlugin([{"name": "db", "package": "pkg-a"}]), name="plugin_a")
    pm.register(_MCPServerPlugin([{"name": "db", "package": "pkg-b"}]), name="plugin_b")

    registry = MCPRegistry(config_path=None)
    pm.collect_plugin_mcp_servers(registry)

    config = registry.build_mcp_config(registry.servers)
    assert config is not None
    assert "plugin_a__db" in config["mcpServers"]
    assert "plugin_b__db" in config["mcpServers"]
    assert config["mcpServers"]["plugin_a__db"]["args"] == ["-y", "pkg-a"]
    assert config["mcpServers"]["plugin_b__db"]["args"] == ["-y", "pkg-b"]


def test_collect_plugin_mcp_servers_broken_plugin_does_not_crash(pm: PluginManager) -> None:
    """A plugin whose provide_mcp_servers raises does not break collection."""
    from bernstein.core.mcp_registry import MCPRegistry

    class _BrokenMCPPlugin:
        @hookimpl
        def provide_mcp_servers(self) -> list[dict[str, Any]]:
            raise RuntimeError("oops")

    pm.register(_BrokenMCPPlugin(), name="broken_mcp")
    pm.register(_MCPServerPlugin([{"name": "ok-server", "package": "ok-pkg"}]), name="ok_plugin")

    registry = MCPRegistry(config_path=None)
    pm.collect_plugin_mcp_servers(registry)  # must not raise

    config = registry.build_mcp_config(registry.servers)
    assert config is not None
    assert "ok_plugin__ok-server" in config["mcpServers"]


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _make_fake_entry_point(name: str, plugin: Any) -> Any:
    """Create a fake entry point that loads *plugin* when called."""
    plugin_class: type[Any] = cast("type[Any]", type(plugin))

    class _FakeEP:
        def __init__(self) -> None:
            self.name = name
            self.value = f"fake.module:{plugin_class.__name__}"

        def load(self) -> type[Any]:
            return plugin_class  # return the class; manager will instantiate

    return _FakeEP()
