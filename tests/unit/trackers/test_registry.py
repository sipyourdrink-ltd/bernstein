"""Tests for the tracker registry and ``provide_tracker_adapter`` hookspec."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from bernstein.core.trackers.contract import AbstractTrackerAdapter
from bernstein.core.trackers.registry import (
    DuplicateTrackerError,
    TrackerRegistration,
    TrackerRegistry,
    discover_plugin_trackers,
    get_registry,
    register_tracker,
    reset_registry_for_tests,
)
from tests.fixtures.trackers.in_memory_tracker import InMemoryTracker

if TYPE_CHECKING:
    from pathlib import Path


@pytest.fixture(autouse=True)
def _reset_registry() -> Any:
    reset_registry_for_tests()
    yield
    reset_registry_for_tests()


def test_registry_default_lists_builtins() -> None:
    registry = get_registry()
    names = registry.names()
    # The built-ins shipped in core/trackers/ + core/trackers/builtin/.
    assert "linear" in names
    assert "servicenow" in names
    assert "github_projects_v2" in names
    assert "jira_cloud" in names
    # Built-ins all carry the ``builtin`` source label.
    for entry in registry:
        if entry.name in {
            "linear",
            "servicenow",
            "github_projects_v2",
            "jira_cloud",
            "jira_dc",
            "gitlab",
            "clickup",
            "asana",
            "plane",
        }:
            assert entry.source == "builtin"


def test_register_tracker_appends_programmatic_entry() -> None:
    entry = register_tracker(
        "in_memory",
        InMemoryTracker,
        summary="In-memory reference adapter.",
        source="programmatic",
    )
    assert entry.name == "in_memory"
    assert "in_memory" in get_registry()
    assert get_registry().get("in_memory").factory is InMemoryTracker


def test_register_tracker_duplicate_without_overwrite_raises() -> None:
    register_tracker("in_memory", InMemoryTracker)
    with pytest.raises(DuplicateTrackerError):
        register_tracker("in_memory", InMemoryTracker)


def test_register_tracker_overwrite_replaces() -> None:
    register_tracker("in_memory", InMemoryTracker, summary="first")
    register_tracker("in_memory", InMemoryTracker, summary="second", overwrite=True)
    assert get_registry().get("in_memory").summary == "second"


def test_registry_create_constructs_via_factory() -> None:
    register_tracker("in_memory", InMemoryTracker)
    adapter = get_registry().create("in_memory")
    assert isinstance(adapter, AbstractTrackerAdapter)


def test_registry_ordering_is_deterministic() -> None:
    # Built-ins come before plugin and programmatic in deterministic
    # alphabetical order within each tier.
    register_tracker(
        "zzz_in_memory",
        InMemoryTracker,
        source="programmatic",
    )
    names = get_registry().names()
    # The programmatic entry must appear after every built-in.
    builtin_count = sum(1 for n in names if get_registry().get(n).source == "builtin")
    assert names[builtin_count] == "zzz_in_memory" or "zzz_in_memory" in names[builtin_count:]


# ---------------------------------------------------------------------------
# Plugin discovery shape
# ---------------------------------------------------------------------------


class _FakePlugin:
    """Plugin object implementing ``provide_tracker_adapter``."""

    def __init__(self, payload: Any) -> None:
        self._payload = payload

    def provide_tracker_adapter(self) -> Any:
        return self._payload


class _FakePluginManager:
    """Minimal stand-in for the orchestrator's plugin manager."""

    def __init__(self, plugins: dict[str, Any]) -> None:
        self._registered_names = list(plugins)
        self._inner = _FakeInner(plugins)

    @property
    def _pm(self) -> _FakeInner:
        return self._inner


class _FakeInner:
    def __init__(self, plugins: dict[str, Any]) -> None:
        self._plugins = plugins

    def get_plugin(self, name: str) -> Any:
        return self._plugins.get(name)


def test_discover_plugin_trackers_accepts_registration_object() -> None:
    payload = TrackerRegistration(
        name="acme_tracker",
        factory=InMemoryTracker,
        summary="Acme custom tracker.",
    )
    pm = _FakePluginManager({"acme": _FakePlugin(payload)})
    added = discover_plugin_trackers(pm)
    assert added == 1
    entry = get_registry().get("acme_tracker")
    assert entry.source == "plugin"
    assert entry.provenance == "acme"


def test_discover_plugin_trackers_accepts_tuple() -> None:
    pm = _FakePluginManager({"acme": _FakePlugin(("tuple_tracker", InMemoryTracker))})
    added = discover_plugin_trackers(pm)
    assert added == 1
    assert "tuple_tracker" in get_registry()


def test_discover_plugin_trackers_accepts_list() -> None:
    payload = [
        TrackerRegistration(name="t1", factory=InMemoryTracker),
        ("t2", InMemoryTracker),
    ]
    pm = _FakePluginManager({"acme": _FakePlugin(payload)})
    added = discover_plugin_trackers(pm)
    assert added == 2


def test_discover_plugin_trackers_skips_none() -> None:
    pm = _FakePluginManager({"acme": _FakePlugin(None)})
    added = discover_plugin_trackers(pm)
    assert added == 0


def test_discover_plugin_trackers_warns_on_duplicate(caplog: pytest.LogCaptureFixture) -> None:
    register_tracker("dup", InMemoryTracker)
    pm = _FakePluginManager({"acme": _FakePlugin(TrackerRegistration(name="dup", factory=InMemoryTracker))})
    with caplog.at_level("WARNING"):
        added = discover_plugin_trackers(pm)
    assert added == 0
    assert any("duplicate tracker" in r.message.lower() for r in caplog.records)


def test_discover_plugin_trackers_isolated_registry() -> None:
    """A bespoke registry can be populated independently of the global."""
    isolated = TrackerRegistry()
    isolated.register("custom", InMemoryTracker, source="programmatic")
    assert isolated.names() == ("custom",)
    assert "custom" not in get_registry()


def test_hookspec_declares_provide_tracker_adapter() -> None:
    """The hookspec is wired into the BernsteinSpec class."""
    from bernstein.plugins.hookspecs import BernsteinSpec

    assert hasattr(BernsteinSpec, "provide_tracker_adapter")


def test_runtime_tracker_hook_quarantine_survives_repair(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from bernstein.cli.commands.pipeline_cmd import _instantiate_trackers
    from bernstein.cli.commands.trackers_cmd import _load_registry_with_plugins
    from bernstein.core.security.audit_chain import EVENT_DECLARATION_QUARANTINED, AuditChainStore
    from bernstein.core.security.declaration_quarantine import DeclarationQuarantineStore

    class RepairablePlugin:
        calls = 0
        broken = True

        def provide_tracker_adapter(self) -> tuple[str, type[InMemoryTracker]]:
            self.calls += 1
            if self.broken:
                raise RuntimeError("synthetic tracker hook failure")
            return "repairable", InMemoryTracker

    plugin = RepairablePlugin()
    pm = _FakePluginManager({"repairable": plugin, "healthy": _FakePlugin(("healthy", InMemoryTracker))})
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("bernstein.plugins.manager.get_plugin_manager", lambda *args, **kwargs: pm)
    store = DeclarationQuarantineStore(tmp_path)
    configs = {"repairable": {}, "healthy": {}}

    assert "healthy" in _load_registry_with_plugins()
    assert not store.path.exists()
    assert not (tmp_path / ".sdd" / "audit").exists()

    adapters, _ = _instantiate_trackers(configs, [], workdir=tmp_path)
    assert set(adapters) == {"healthy"}
    (marker,) = store.load()
    assert marker.kind == "tracker"
    assert marker.source_name == "repairable"
    assert marker.reason == "synthetic tracker hook failure"

    plugin.broken = False
    assert "repairable" not in _load_registry_with_plugins()
    adapters, _ = _instantiate_trackers(configs, [], workdir=tmp_path)
    assert set(adapters) == {"healthy"}
    assert plugin.calls == 2
    assert store.reenable(marker.declaration_id, actor="operator")
    adapters, _ = _instantiate_trackers(configs, [], workdir=tmp_path)
    assert set(adapters) == {"repairable", "healthy"}
    assert plugin.calls == 3
    assert len(AuditChainStore(tmp_path / ".sdd" / "audit").query(event_type=EVENT_DECLARATION_QUARANTINED)) == 1


def test_workspace_tracker_discovery_does_not_reuse_other_plugin_factories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bernstein.core.trackers.registry import discover_workspace_trackers

    first = tmp_path / "first"
    second = tmp_path / "second"
    managers = {
        first: _FakePluginManager({"first": _FakePlugin(("first", InMemoryTracker))}),
        second: _FakePluginManager({"second": _FakePlugin(("second", InMemoryTracker))}),
    }
    register_tracker("global_programmatic", InMemoryTracker)
    discover_plugin_trackers(managers[first])
    monkeypatch.setattr("bernstein.plugins.manager.get_plugin_manager", managers.__getitem__)

    workspace_registry = discover_workspace_trackers(second, record_failures=False)
    assert "first" not in workspace_registry
    assert "second" in workspace_registry
    assert "global_programmatic" in workspace_registry
    assert "linear" in workspace_registry
    assert "first" in get_registry()
    assert "second" not in get_registry()


def test_invalid_registration_leaves_healthy_plugins_and_builtins(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from bernstein.core.trackers.registry import discover_workspace_trackers

    invalid = TrackerRegistration("shared", InMemoryTracker, capabilities=None)  # type: ignore[arg-type]
    pm = _FakePluginManager(
        {
            "bad": _FakePlugin([invalid, ("same_hook", InMemoryTracker)]),
            "good": _FakePlugin(("shared", InMemoryTracker)),
        }
    )
    monkeypatch.setattr("bernstein.plugins.manager.get_plugin_manager", lambda _root: pm)
    registry = discover_workspace_trackers(tmp_path, record_failures=True)
    assert registry.get("shared").provenance == "good"
    assert "same_hook" in registry
    assert "linear" in registry
