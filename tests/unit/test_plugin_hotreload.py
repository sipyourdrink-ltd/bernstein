"""Tests for plugin hot-reloading with version rollback — issue #697."""

from __future__ import annotations

import importlib.machinery
import sys
import types
from unittest.mock import patch

import pytest

from bernstein.core.plugins_core.plugin_hotreload import (
    PluginHotReloader,
    PluginVersionHistory,
    RollbackTrigger,
)


def _make_dummy_module(name: str) -> types.ModuleType:
    """Create a dummy module with a proper spec so importlib.reload works."""
    mod = types.ModuleType(name)
    mod.__spec__ = importlib.machinery.ModuleSpec(name, None)
    mod.__loader__ = None  # type: ignore[assignment]
    return mod


# ---------------------------------------------------------------------------
# PluginVersionHistory dataclass
# ---------------------------------------------------------------------------


class TestPluginVersionHistory:
    def test_frozen(self) -> None:
        history = PluginVersionHistory(
            plugin_name="my-plugin",
            current_version="1.0.0",
            previous_version="",
            versions=("1.0.0",),
            last_updated=0.0,
        )
        assert history.plugin_name == "my-plugin"
        assert history.current_version == "1.0.0"
        assert history.previous_version == ""
        assert history.versions == ("1.0.0",)

    def test_immutable(self) -> None:
        history = PluginVersionHistory(
            plugin_name="x",
            current_version="1.0.0",
            previous_version="",
            versions=("1.0.0",),
            last_updated=0.0,
        )
        try:
            history.current_version = "2.0.0"  # type: ignore[misc]
            raised = False
        except AttributeError:
            raised = True
        assert raised


# ---------------------------------------------------------------------------
# RollbackTrigger dataclass
# ---------------------------------------------------------------------------


class TestRollbackTrigger:
    def test_frozen(self) -> None:
        trigger = RollbackTrigger(
            plugin_name="p",
            metric="quality_gate_pass_rate",
            threshold=0.7,
            current_value=0.5,
            triggered=True,
        )
        assert trigger.triggered is True
        assert trigger.current_value == pytest.approx(0.5)
        assert trigger.threshold == pytest.approx(0.7)

    def test_immutable(self) -> None:
        trigger = RollbackTrigger(
            plugin_name="p",
            metric="m",
            threshold=0.7,
            current_value=1.0,
            triggered=False,
        )
        try:
            trigger.triggered = True  # type: ignore[misc]
            raised = False
        except AttributeError:
            raised = True
        assert raised


# ---------------------------------------------------------------------------
# PluginHotReloader.hot_reload
# ---------------------------------------------------------------------------


class TestHotReload:
    def test_reload_existing_module(self) -> None:
        """Reloading a module already in sys.modules succeeds."""
        reloader = PluginHotReloader()
        dummy = _make_dummy_module("_test_hotreload_dummy")
        sys.modules["_test_hotreload_dummy"] = dummy
        try:
            with patch("bernstein.core.plugins_core.plugin_hotreload.importlib.reload", return_value=dummy):
                result = reloader.hot_reload("_test_hotreload_dummy", "1.0.0")
            assert result is True
            history = reloader.get_history("_test_hotreload_dummy")
            assert history is not None
            assert history.current_version == "1.0.0"
        finally:
            sys.modules.pop("_test_hotreload_dummy", None)

    def test_reload_updates_version_store(self) -> None:
        """Successive reloads track version progression."""
        reloader = PluginHotReloader()
        dummy = _make_dummy_module("_test_hotreload_v")
        sys.modules["_test_hotreload_v"] = dummy
        try:
            with patch("bernstein.core.plugins_core.plugin_hotreload.importlib.reload", return_value=dummy):
                reloader.hot_reload("_test_hotreload_v", "1.0.0")
                reloader.hot_reload("_test_hotreload_v", "2.0.0")

            history = reloader.get_history("_test_hotreload_v")
            assert history is not None
            assert history.current_version == "2.0.0"
            assert history.previous_version == "1.0.0"
            assert history.versions == ("1.0.0", "2.0.0")
        finally:
            sys.modules.pop("_test_hotreload_v", None)

    def test_reload_nonexistent_module_fails(self) -> None:
        """Importing a module that does not exist returns False."""
        reloader = PluginHotReloader()
        result = reloader.hot_reload("_nonexistent_plugin_xyz_697", "1.0.0")
        assert result is False
        assert reloader.get_history("_nonexistent_plugin_xyz_697") is None

    def test_reload_exception_during_reload(self) -> None:
        """When importlib.reload raises, version store is not updated."""
        reloader = PluginHotReloader()
        dummy = _make_dummy_module("_test_hotreload_fail")
        sys.modules["_test_hotreload_fail"] = dummy

        try:
            with patch("bernstein.core.plugins_core.plugin_hotreload.importlib.reload", return_value=dummy):
                reloader.hot_reload("_test_hotreload_fail", "1.0.0")

            with patch(
                "bernstein.core.plugins_core.plugin_hotreload.importlib.reload",
                side_effect=ImportError("boom"),
            ):
                result = reloader.hot_reload("_test_hotreload_fail", "2.0.0")

            assert result is False
            history = reloader.get_history("_test_hotreload_fail")
            assert history is not None
            assert history.current_version == "1.0.0"
        finally:
            sys.modules.pop("_test_hotreload_fail", None)

    def test_hyphen_to_underscore_conversion(self) -> None:
        """Plugin names with hyphens are converted to underscores for import."""
        reloader = PluginHotReloader()
        dummy = _make_dummy_module("my_plugin")
        sys.modules["my_plugin"] = dummy
        try:
            with patch("bernstein.core.plugins_core.plugin_hotreload.importlib.reload", return_value=dummy):
                result = reloader.hot_reload("my-plugin", "1.0.0")
            assert result is True
            # Version store uses the original name (with hyphens)
            history = reloader.get_history("my-plugin")
            assert history is not None
            assert history.current_version == "1.0.0"
        finally:
            sys.modules.pop("my_plugin", None)


# ---------------------------------------------------------------------------
# PluginHotReloader.detect_degradation
# ---------------------------------------------------------------------------


class TestDetectDegradation:
    def test_no_metrics_returns_no_trigger(self) -> None:
        """When no metrics exist, pass rate defaults to 1.0 (no degradation)."""
        reloader = PluginHotReloader()
        trigger = reloader.detect_degradation("unknown-plugin")
        assert trigger.triggered is False
        assert trigger.current_value == pytest.approx(1.0)

    def test_all_passes_no_degradation(self) -> None:
        reloader = PluginHotReloader()
        for _ in range(10):
            reloader.record_quality_gate("p", passed=True)
        trigger = reloader.detect_degradation("p")
        assert trigger.triggered is False
        assert trigger.current_value == pytest.approx(1.0)

    def test_all_failures_triggers(self) -> None:
        reloader = PluginHotReloader()
        for _ in range(5):
            reloader.record_quality_gate("p", passed=False)
        trigger = reloader.detect_degradation("p")
        assert trigger.triggered is True
        assert trigger.current_value == pytest.approx(0.0)

    def test_below_threshold_triggers(self) -> None:
        """Pass rate below threshold triggers rollback."""
        reloader = PluginHotReloader(default_pass_rate_threshold=0.7)
        # 2 passes, 8 failures = 20% pass rate
        for _ in range(2):
            reloader.record_quality_gate("p", passed=True)
        for _ in range(8):
            reloader.record_quality_gate("p", passed=False)

        trigger = reloader.detect_degradation("p")
        assert trigger.triggered is True
        assert trigger.current_value < 0.7

    def test_above_threshold_no_trigger(self) -> None:
        """Pass rate above threshold does not trigger."""
        reloader = PluginHotReloader(default_pass_rate_threshold=0.5)
        for _ in range(8):
            reloader.record_quality_gate("p", passed=True)
        for _ in range(2):
            reloader.record_quality_gate("p", passed=False)

        trigger = reloader.detect_degradation("p")
        assert trigger.triggered is False
        assert trigger.current_value == pytest.approx(0.8)

    def test_trigger_includes_metric_name(self) -> None:
        reloader = PluginHotReloader()
        trigger = reloader.detect_degradation("p")
        assert trigger.metric == "quality_gate_pass_rate"
        assert trigger.plugin_name == "p"

    def test_threshold_from_constructor(self) -> None:
        reloader = PluginHotReloader(default_pass_rate_threshold=0.9)
        trigger = reloader.detect_degradation("p")
        assert trigger.threshold == pytest.approx(0.9)


# ---------------------------------------------------------------------------
# PluginHotReloader.rollback
# ---------------------------------------------------------------------------


class TestRollback:
    def test_rollback_swaps_versions(self) -> None:
        reloader = PluginHotReloader()
        dummy = _make_dummy_module("_test_rb")
        sys.modules["_test_rb"] = dummy
        try:
            with patch("bernstein.core.plugins_core.plugin_hotreload.importlib.reload", return_value=dummy):
                reloader.hot_reload("_test_rb", "1.0.0")
                reloader.hot_reload("_test_rb", "2.0.0")
            result = reloader.rollback("_test_rb")

            assert result is True
            history = reloader.get_history("_test_rb")
            assert history is not None
            assert history.current_version == "1.0.0"
            assert history.previous_version == "2.0.0"
        finally:
            sys.modules.pop("_test_rb", None)

    def test_rollback_no_previous_version(self) -> None:
        """Rollback fails when only one version exists (no previous)."""
        reloader = PluginHotReloader()
        dummy = _make_dummy_module("_test_rb_one")
        sys.modules["_test_rb_one"] = dummy
        try:
            with patch("bernstein.core.plugins_core.plugin_hotreload.importlib.reload", return_value=dummy):
                reloader.hot_reload("_test_rb_one", "1.0.0")
            result = reloader.rollback("_test_rb_one")
            assert result is False
        finally:
            sys.modules.pop("_test_rb_one", None)

    def test_rollback_unknown_plugin(self) -> None:
        """Rollback fails for a plugin never loaded."""
        reloader = PluginHotReloader()
        result = reloader.rollback("never-loaded")
        assert result is False

    def test_double_rollback_swaps_back(self) -> None:
        """Two consecutive rollbacks return to the original version."""
        reloader = PluginHotReloader()
        dummy = _make_dummy_module("_test_rb_double")
        sys.modules["_test_rb_double"] = dummy
        try:
            with patch("bernstein.core.plugins_core.plugin_hotreload.importlib.reload", return_value=dummy):
                reloader.hot_reload("_test_rb_double", "1.0.0")
                reloader.hot_reload("_test_rb_double", "2.0.0")

            reloader.rollback("_test_rb_double")
            reloader.rollback("_test_rb_double")

            history = reloader.get_history("_test_rb_double")
            assert history is not None
            assert history.current_version == "2.0.0"
            assert history.previous_version == "1.0.0"
        finally:
            sys.modules.pop("_test_rb_double", None)


# ---------------------------------------------------------------------------
# PluginHotReloader.get_history
# ---------------------------------------------------------------------------


class TestGetHistory:
    def test_returns_none_for_unknown(self) -> None:
        reloader = PluginHotReloader()
        assert reloader.get_history("does-not-exist") is None

    def test_returns_frozen_snapshot(self) -> None:
        reloader = PluginHotReloader()
        dummy = _make_dummy_module("_test_hist")
        sys.modules["_test_hist"] = dummy
        try:
            with patch("bernstein.core.plugins_core.plugin_hotreload.importlib.reload", return_value=dummy):
                reloader.hot_reload("_test_hist", "1.0.0")
            history = reloader.get_history("_test_hist")
            assert history is not None
            assert isinstance(history, PluginVersionHistory)
            # Verify it is frozen
            try:
                history.current_version = "nope"  # type: ignore[misc]
                raised = False
            except AttributeError:
                raised = True
            assert raised
        finally:
            sys.modules.pop("_test_hist", None)

    def test_versions_tuple_grows(self) -> None:
        reloader = PluginHotReloader()
        dummy = _make_dummy_module("_test_grow")
        sys.modules["_test_grow"] = dummy
        try:
            with patch("bernstein.core.plugins_core.plugin_hotreload.importlib.reload", return_value=dummy):
                reloader.hot_reload("_test_grow", "1.0.0")
                reloader.hot_reload("_test_grow", "2.0.0")
                reloader.hot_reload("_test_grow", "3.0.0")

            history = reloader.get_history("_test_grow")
            assert history is not None
            assert history.versions == ("1.0.0", "2.0.0", "3.0.0")
            assert history.current_version == "3.0.0"
            assert history.previous_version == "2.0.0"
        finally:
            sys.modules.pop("_test_grow", None)


# ---------------------------------------------------------------------------
# PluginHotReloader.record_quality_gate
# ---------------------------------------------------------------------------


class TestRecordQualityGate:
    def test_record_pass(self) -> None:
        reloader = PluginHotReloader()
        reloader.record_quality_gate("p", passed=True)
        trigger = reloader.detect_degradation("p")
        assert trigger.current_value == pytest.approx(1.0)

    def test_record_fail(self) -> None:
        reloader = PluginHotReloader()
        reloader.record_quality_gate("p", passed=False)
        trigger = reloader.detect_degradation("p")
        assert trigger.current_value == pytest.approx(0.0)
        assert trigger.triggered is True
