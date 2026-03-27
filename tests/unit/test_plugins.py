"""Tests for the Bernstein plugin system."""

from __future__ import annotations

import warnings
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

import pytest

if TYPE_CHECKING:
    from pathlib import Path

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
def pm() -> PluginManager:
    """Fresh PluginManager with no external plugins loaded."""
    return PluginManager()


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
# Partial-hook plugins — unimplemented hooks must not crash
# ---------------------------------------------------------------------------


def test_unimplemented_hooks_do_not_crash(pm: PluginManager) -> None:
    """Hooks not implemented by a plugin must be silently skipped."""
    plugin = _PartialPlugin()
    pm.register(plugin, name="partial")

    # These hooks are NOT implemented by _PartialPlugin — must not raise.
    pm.fire_task_created(task_id="x", role="r", title="t")
    pm.fire_task_failed(task_id="x", role="r", error="e")
    pm.fire_agent_spawned(session_id="s", role="r", model="m")
    pm.fire_agent_reaped(session_id="s", role="r", outcome="o")
    pm.fire_evolve_proposal(proposal_id="p", title="t", verdict="v")

    # The one implemented hook should still fire.
    pm.fire_task_completed(task_id="x", role="r", result_summary="ok")
    assert plugin.fired is True


# ---------------------------------------------------------------------------
# Broken plugins — exceptions must not propagate
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
        local_pm.load_from_workdir(tmp_path)  # no bernstein.yaml — must not raise
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
