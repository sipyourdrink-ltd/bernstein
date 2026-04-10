"""Unit tests for the adapter plugin SDK."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

from bernstein.adapters.base import CLIAdapter, SpawnResult
from bernstein.adapters.plugin_sdk import (
    AdapterCapability,
    AdapterPluginInfo,
    PluginAdapter,
    PluginRegistry,
    validate_plugin,
)

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ModelConfig


# ---------------------------------------------------------------------------
# Helpers — concrete PluginAdapter for testing
# ---------------------------------------------------------------------------


class _StubPluginAdapter(PluginAdapter):
    """Minimal concrete PluginAdapter for test use."""

    def __init__(
        self,
        *,
        info_name: str = "stub",
        info_version: str = "1.0.0",
        healthy: bool = True,
        models: list[str] | None = None,
    ) -> None:
        super().__init__()
        self._info_name = info_name
        self._info_version = info_version
        self._healthy = healthy
        self._models = models or []

    def plugin_info(self) -> AdapterPluginInfo:
        return AdapterPluginInfo(
            name=self._info_name,
            version=self._info_version,
            author="Test Author",
            description="A stub adapter for testing",
            homepage="https://example.com",
            min_bernstein_version="1.0.0",
            capabilities=(AdapterCapability.STREAMING, AdapterCapability.TOOL_USE),
        )

    def health_check(self) -> bool:
        return self._healthy

    def supported_models(self) -> list[str]:
        return list(self._models)

    def spawn(
        self,
        *,
        prompt: str,
        workdir: Path,
        model_config: ModelConfig,
        session_id: str,
        mcp_config: dict[str, Any] | None = None,
        timeout_seconds: int = 1800,
    ) -> SpawnResult:
        raise NotImplementedError("stub")

    def name(self) -> str:
        return self._info_name


# ---------------------------------------------------------------------------
# AdapterPluginInfo
# ---------------------------------------------------------------------------


class TestAdapterPluginInfo:
    """Tests for the AdapterPluginInfo frozen dataclass."""

    def test_create_minimal(self) -> None:
        info = AdapterPluginInfo(name="myagent", version="0.1.0")
        assert info.name == "myagent"
        assert info.version == "0.1.0"
        assert info.author == ""
        assert info.description == ""
        assert info.homepage == ""
        assert info.min_bernstein_version == ""
        assert info.capabilities == ()

    def test_create_full(self) -> None:
        info = AdapterPluginInfo(
            name="myagent",
            version="2.0.0",
            author="Jane Doe",
            description="Custom agent",
            homepage="https://github.com/example",
            min_bernstein_version="1.5.0",
            capabilities=(AdapterCapability.MULTI_MODEL, AdapterCapability.BATCH_MODE),
        )
        assert info.name == "myagent"
        assert info.version == "2.0.0"
        assert info.author == "Jane Doe"
        assert info.description == "Custom agent"
        assert info.homepage == "https://github.com/example"
        assert info.min_bernstein_version == "1.5.0"
        assert AdapterCapability.MULTI_MODEL in info.capabilities
        assert AdapterCapability.BATCH_MODE in info.capabilities

    def test_frozen(self) -> None:
        info = AdapterPluginInfo(name="a", version="1.0.0")
        with pytest.raises(AttributeError):
            info.name = "b"  # type: ignore[misc]

    def test_equality(self) -> None:
        a = AdapterPluginInfo(name="x", version="1.0.0")
        b = AdapterPluginInfo(name="x", version="1.0.0")
        assert a == b

    def test_inequality(self) -> None:
        a = AdapterPluginInfo(name="x", version="1.0.0")
        b = AdapterPluginInfo(name="y", version="1.0.0")
        assert a != b


# ---------------------------------------------------------------------------
# AdapterCapability enum
# ---------------------------------------------------------------------------


class TestAdapterCapability:
    """Tests for the AdapterCapability enum."""

    def test_all_members_exist(self) -> None:
        expected = {
            "STREAMING",
            "TOOL_USE",
            "MULTI_MODEL",
            "RATE_LIMIT_DETECTION",
            "STRUCTURED_OUTPUT",
            "BATCH_MODE",
        }
        assert set(AdapterCapability.__members__.keys()) == expected

    def test_values_are_strings(self) -> None:
        for cap in AdapterCapability:
            assert isinstance(cap.value, str)

    def test_specific_values(self) -> None:
        assert AdapterCapability.STREAMING.value == "streaming"
        assert AdapterCapability.TOOL_USE.value == "tool_use"
        assert AdapterCapability.MULTI_MODEL.value == "multi_model"
        assert AdapterCapability.RATE_LIMIT_DETECTION.value == "rate_limit_detection"
        assert AdapterCapability.STRUCTURED_OUTPUT.value == "structured_output"
        assert AdapterCapability.BATCH_MODE.value == "batch_mode"


# ---------------------------------------------------------------------------
# PluginAdapter (ABC contract)
# ---------------------------------------------------------------------------


class TestPluginAdapter:
    """Tests for PluginAdapter abstract base class."""

    def test_is_subclass_of_cli_adapter(self) -> None:
        assert issubclass(PluginAdapter, CLIAdapter)

    def test_stub_adapter_instantiates(self) -> None:
        adapter = _StubPluginAdapter()
        assert adapter.name() == "stub"
        assert adapter.health_check() is True
        assert adapter.supported_models() == []

    def test_validate_config_default_returns_empty(self) -> None:
        adapter = _StubPluginAdapter()
        assert adapter.validate_config({"key": "value"}) == []

    def test_plugin_info_returns_correct_data(self) -> None:
        adapter = _StubPluginAdapter(info_name="custom", info_version="3.2.1")
        info = adapter.plugin_info()
        assert info.name == "custom"
        assert info.version == "3.2.1"
        assert info.author == "Test Author"


# ---------------------------------------------------------------------------
# validate_plugin()
# ---------------------------------------------------------------------------


class TestValidatePlugin:
    """Tests for the validate_plugin() function."""

    def test_valid_plugin_no_errors(self) -> None:
        adapter = _StubPluginAdapter()
        errors = validate_plugin(adapter)
        assert errors == []

    def test_empty_name_is_error(self) -> None:
        adapter = _StubPluginAdapter(info_name="")
        errors = validate_plugin(adapter)
        assert any("name must not be empty" in e for e in errors)

    def test_empty_version_is_error(self) -> None:
        adapter = _StubPluginAdapter(info_version="")
        errors = validate_plugin(adapter)
        assert any("version must not be empty" in e for e in errors)

    def test_unhealthy_adapter_is_error(self) -> None:
        adapter = _StubPluginAdapter(healthy=False)
        errors = validate_plugin(adapter)
        assert any("health_check() returned False" in e for e in errors)

    def test_plugin_info_exception_captured(self) -> None:
        adapter = _StubPluginAdapter()
        adapter.plugin_info = MagicMock(side_effect=RuntimeError("boom"))  # type: ignore[method-assign]
        errors = validate_plugin(adapter)
        assert len(errors) == 1
        assert "plugin_info() raised an exception" in errors[0]

    def test_health_check_exception_captured(self) -> None:
        adapter = _StubPluginAdapter()
        adapter.health_check = MagicMock(side_effect=OSError("no cli"))  # type: ignore[method-assign]
        errors = validate_plugin(adapter)
        assert any("health_check() raised an exception" in e for e in errors)

    def test_supported_models_bad_return_type(self) -> None:
        adapter = _StubPluginAdapter()
        adapter.supported_models = MagicMock(return_value="not-a-list")  # type: ignore[method-assign]
        errors = validate_plugin(adapter)
        assert any("supported_models() must return list" in e for e in errors)

    def test_validate_config_bad_return_type(self) -> None:
        adapter = _StubPluginAdapter()
        adapter.validate_config = MagicMock(return_value="not-a-list")  # type: ignore[method-assign]
        errors = validate_plugin(adapter)
        assert any("validate_config() must return list" in e for e in errors)

    def test_name_exception_captured(self) -> None:
        adapter = _StubPluginAdapter()
        adapter.name = MagicMock(side_effect=RuntimeError("no name"))  # type: ignore[method-assign]
        errors = validate_plugin(adapter)
        assert any("name() raised an exception" in e for e in errors)


# ---------------------------------------------------------------------------
# PluginRegistry
# ---------------------------------------------------------------------------


class TestPluginRegistry:
    """Tests for PluginRegistry."""

    def test_register_and_list(self) -> None:
        registry = PluginRegistry()
        adapter = _StubPluginAdapter(info_name="alpha", info_version="1.0.0")
        registry.register(adapter)

        plugins = registry.list_plugins()
        assert len(plugins) == 1
        assert plugins[0].name == "alpha"

    def test_register_multiple(self) -> None:
        registry = PluginRegistry()
        registry.register(_StubPluginAdapter(info_name="one"))
        registry.register(_StubPluginAdapter(info_name="two"))
        registry.register(_StubPluginAdapter(info_name="three"))

        plugins = registry.list_plugins()
        names = {p.name for p in plugins}
        assert names == {"one", "two", "three"}

    def test_register_empty_name_raises(self) -> None:
        registry = PluginRegistry()
        adapter = _StubPluginAdapter(info_name="")
        with pytest.raises(ValueError, match="empty"):
            registry.register(adapter)

    def test_get_registered(self) -> None:
        registry = PluginRegistry()
        adapter = _StubPluginAdapter(info_name="beta")
        registry.register(adapter)

        result = registry.get("beta")
        assert result is adapter

    def test_get_missing_returns_none(self) -> None:
        registry = PluginRegistry()
        assert registry.get("nonexistent") is None

    def test_unregister_existing(self) -> None:
        registry = PluginRegistry()
        registry.register(_StubPluginAdapter(info_name="temp"))
        assert registry.unregister("temp") is True
        assert registry.get("temp") is None

    def test_unregister_missing(self) -> None:
        registry = PluginRegistry()
        assert registry.unregister("ghost") is False

    def test_list_empty(self) -> None:
        registry = PluginRegistry()
        assert registry.list_plugins() == []

    def test_discover_plugins_loads_from_entrypoints(self) -> None:
        """discover_plugins() loads PluginAdapter subclasses from entry-points."""
        mock_ep = MagicMock()
        mock_ep.name = "fakeplugin"
        mock_ep.value = "fake_package:FakeAdapter"
        mock_ep.load.return_value = _StubPluginAdapter

        with patch(
            "bernstein.adapters.plugin_sdk.entry_points",
            return_value=[mock_ep],
        ):
            registry = PluginRegistry()
            count = registry.discover_plugins()

        assert count == 1
        plugins = registry.list_plugins()
        assert len(plugins) == 1
        assert plugins[0].name == "stub"

    def test_discover_plugins_skips_plain_cli_adapter(self) -> None:
        """discover_plugins() ignores CLIAdapter subclasses that are not PluginAdapter."""
        mock_ep = MagicMock()
        mock_ep.name = "plainadapter"
        mock_ep.value = "some_package:PlainAdapter"
        mock_ep.load.return_value = CLIAdapter  # Abstract, not a PluginAdapter

        with patch(
            "bernstein.adapters.plugin_sdk.entry_points",
            return_value=[mock_ep],
        ):
            registry = PluginRegistry()
            count = registry.discover_plugins()

        assert count == 0
        assert registry.list_plugins() == []

    def test_discover_plugins_handles_load_error(self) -> None:
        """discover_plugins() gracefully skips entry-points that fail to load."""
        mock_ep = MagicMock()
        mock_ep.name = "broken"
        mock_ep.load.side_effect = ImportError("no such module")

        with patch(
            "bernstein.adapters.plugin_sdk.entry_points",
            return_value=[mock_ep],
        ):
            registry = PluginRegistry()
            count = registry.discover_plugins()

        assert count == 0
        assert registry.list_plugins() == []

    def test_discover_plugins_accepts_instance(self) -> None:
        """discover_plugins() accepts pre-instantiated PluginAdapter instances."""
        instance = _StubPluginAdapter(info_name="prebuilt")

        mock_ep = MagicMock()
        mock_ep.name = "prebuilt"
        mock_ep.value = "pkg:instance"
        mock_ep.load.return_value = instance

        with patch(
            "bernstein.adapters.plugin_sdk.entry_points",
            return_value=[mock_ep],
        ):
            registry = PluginRegistry()
            count = registry.discover_plugins()

        assert count == 1
        assert registry.get("prebuilt") is instance
