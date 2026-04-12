"""Tests for plugin_errors — plugin error collection and reporting."""

from __future__ import annotations

import pytest

from bernstein.plugins.plugin_errors import (
    PluginError,
    PluginErrorRegistry,
    get_plugin_errors,
    report_plugin_error,
)

# --- Fixtures ---


@pytest.fixture()
def fresh_registry(monkeypatch: pytest.MonkeyPatch) -> PluginErrorRegistry:
    """Reset the global plugin error registry."""
    from bernstein.plugins import plugin_errors as pe

    pe._registry = PluginErrorRegistry()
    return pe._registry


# --- TestPluginError ---


class TestPluginError:
    def test_to_dict(self) -> None:
        e = PluginError(plugin_name="my-plugin", phase="load", message="import failed")
        d = e.to_dict()
        assert d["plugin_name"] == "my-plugin"
        assert d["phase"] == "load"
        assert d["message"] == "import failed"
        assert "traceback" in d


# --- TestPluginErrorRegistry ---


class TestPluginErrorRegistry:
    def test_add_and_get(self) -> None:
        r = PluginErrorRegistry()
        r.add(PluginError("p1", "load", "oops"))
        errors = r.get_errors()
        assert len(errors) == 1
        assert errors[0].plugin_name == "p1"

    def test_add_simple(self) -> None:
        r = PluginErrorRegistry()
        r.add_simple("p2", "execute", "runtime fail")
        errors = r.get_errors()
        assert len(errors) == 1
        assert errors[0].traceback == ""

    def test_add_simple_with_exception(self) -> None:
        r = PluginErrorRegistry()
        exc = ValueError("boom")
        r.add_simple("p3", "hook", "hook failed", exc)
        errors = r.get_errors()
        assert "ValueError" in errors[0].traceback

    def test_clear(self) -> None:
        r = PluginErrorRegistry()
        r.add(PluginError("p1", "load", "x"))
        r.clear()
        assert r.get_errors() == []

    def test_has_errors(self) -> None:
        r = PluginErrorRegistry()
        assert r.has_errors() is False
        r.add(PluginError("p1", "load", "x"))
        assert r.has_errors() is True

    def test_count(self) -> None:
        r = PluginErrorRegistry()
        assert r.count() == 0
        r.add(PluginError("p1", "load", "x"))
        r.add(PluginError("p2", "discover", "y"))
        assert r.count() == 2

    def test_get_errors_returns_copy(self) -> None:
        r = PluginErrorRegistry()
        r.add(PluginError("p1", "load", "x"))
        errors = r.get_errors()
        errors.clear()
        assert r.count() == 1  # original unaffected


# --- TestModuleLevelAPI ---


class TestModuleLevelAPI:
    def test_get_returns_registry(self, fresh_registry: PluginErrorRegistry) -> None:
        assert get_plugin_errors() is not None

    def test_report_and_retrieve(self, fresh_registry: None) -> None:
        report_plugin_error("broken-plugin", "load", "cannot import")
        errors = get_plugin_errors().get_errors()
        assert len(errors) == 1
        assert errors[0].plugin_name == "broken-plugin"
        assert errors[0].phase == "load"
