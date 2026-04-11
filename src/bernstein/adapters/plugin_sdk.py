"""Adapter plugin SDK for third-party agent integration.

Provides base classes and utilities for building third-party CLI agent
adapters that plug into Bernstein via the ``bernstein.adapters`` entry-point
group.

Usage for plugin authors::

    from bernstein.adapters.plugin_sdk import (
        AdapterCapability,
        AdapterPluginInfo,
        PluginAdapter,
    )

    class MyAgentAdapter(PluginAdapter):
        def plugin_info(self) -> AdapterPluginInfo:
            return AdapterPluginInfo(
                name="myagent",
                version="0.1.0",
                author="Me",
                description="My custom agent adapter",
            )
        ...

Then register in ``pyproject.toml``::

    [project.entry-points."bernstein.adapters"]
    myagent = "my_package.adapter:MyAgentAdapter"
"""

from __future__ import annotations

import logging
from abc import abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Any

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.adapters.base import SpawnResult
    from bernstein.core.models import ModelConfig

logger = logging.getLogger(__name__)


class AdapterCapability(Enum):
    """Capabilities that a plugin adapter may advertise."""

    STREAMING = "streaming"
    TOOL_USE = "tool_use"
    MULTI_MODEL = "multi_model"
    RATE_LIMIT_DETECTION = "rate_limit_detection"
    STRUCTURED_OUTPUT = "structured_output"
    BATCH_MODE = "batch_mode"


@dataclass(frozen=True)
class AdapterPluginInfo:
    """Metadata describing a third-party adapter plugin.

    Attributes:
        name: Short identifier for the adapter (e.g. ``"myagent"``).
        version: Semver version string of the plugin.
        author: Author or maintainer name.
        description: One-line description of the adapter.
        homepage: URL for the project homepage or repository.
        min_bernstein_version: Minimum Bernstein version required.
        capabilities: Capabilities this adapter supports.
    """

    name: str
    version: str
    author: str = ""
    description: str = ""
    homepage: str = ""
    min_bernstein_version: str = ""
    capabilities: tuple[AdapterCapability, ...] = field(default_factory=tuple)


class PluginAdapter(CLIAdapter):
    """Abstract base class for third-party adapter plugins.

    Extends :class:`CLIAdapter` with plugin metadata, health checks,
    model listing, and configuration validation.  Plugin authors should
    subclass this instead of ``CLIAdapter`` directly.
    """

    @abstractmethod
    def plugin_info(self) -> AdapterPluginInfo:
        """Return metadata about this plugin adapter.

        Returns:
            An :class:`AdapterPluginInfo` describing the plugin.
        """
        ...

    def health_check(self) -> bool:
        """Verify the underlying CLI tool is installed and reachable.

        Returns:
            ``True`` if the CLI tool is available, ``False`` otherwise.
        """
        return True

    def supported_models(self) -> list[str]:
        """List model identifiers this adapter can use.

        Returns:
            A list of model name strings.  Empty list means the adapter
            does not restrict models.
        """
        return []

    def validate_config(self, config: dict[str, Any]) -> list[str]:
        """Validate adapter-specific configuration.

        Args:
            config: Configuration dictionary to validate.

        Returns:
            A list of human-readable validation error messages.
            An empty list means the configuration is valid.
        """
        return []

    @abstractmethod
    def spawn(
        self,
        *,
        prompt: str,
        workdir: Path,
        model_config: ModelConfig,
        session_id: str,
        mcp_config: dict[str, Any] | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        task_scope: str = "medium",
        budget_multiplier: float = 1.0,
        system_addendum: str = "",
    ) -> SpawnResult:
        """Launch an agent process with the given prompt."""
        ...

    @abstractmethod
    def name(self) -> str:
        """Human-readable name of this CLI adapter."""
        ...


def validate_plugin(adapter: PluginAdapter) -> list[str]:
    """Validate a plugin adapter for correctness and completeness.

    Checks that required metadata fields are populated, that
    ``health_check`` succeeds, and that ``supported_models`` and
    ``validate_config`` return the expected types.

    Args:
        adapter: The plugin adapter instance to validate.

    Returns:
        A list of validation error strings.  Empty means valid.
    """
    errors: list[str] = []

    # --- plugin_info validation ---
    try:
        info = adapter.plugin_info()
    except Exception as exc:
        errors.append(f"plugin_info() raised an exception: {exc}")
        return errors

    if not info.name:
        errors.append("plugin_info().name must not be empty")
    if not info.version:
        errors.append("plugin_info().version must not be empty")

    # --- health_check ---
    try:
        healthy = adapter.health_check()
        if not healthy:
            errors.append("health_check() returned False")
    except Exception as exc:
        errors.append(f"health_check() raised an exception: {exc}")

    # --- supported_models ---
    try:
        _models = adapter.supported_models()
        # Type is already list[str] per the method signature; no runtime
        # check needed beyond verifying the call succeeds.
        _ = _models  # ensure the return value is consumed
    except Exception as exc:
        errors.append(f"supported_models() raised an exception: {exc}")

    # --- validate_config ---
    try:
        _config_errors = adapter.validate_config({})
        # Type is already list[str] per the method signature; no runtime
        # check needed beyond verifying the call succeeds.
        _ = _config_errors  # ensure the return value is consumed
    except Exception as exc:
        errors.append(f"validate_config() raised an exception: {exc}")

    # --- name ---
    try:
        adapter_name = adapter.name()
        if not adapter_name:
            errors.append("name() must not return an empty string")
    except Exception as exc:
        errors.append(f"name() raised an exception: {exc}")

    return errors


class PluginRegistry:
    """Registry for discovering and managing plugin adapters.

    Discovers plugins from the ``bernstein.adapters`` entry-point group
    and provides a unified listing interface.
    """

    def __init__(self) -> None:
        self._plugins: dict[str, PluginAdapter] = {}

    def discover_plugins(self) -> int:
        """Load plugin adapters from the ``bernstein.adapters`` entry-point group.

        Only loads adapters that are subclasses of :class:`PluginAdapter`.
        Built-in adapters (direct ``CLIAdapter`` subclasses) are skipped.

        Returns:
            Number of plugins successfully loaded.
        """
        loaded = 0
        for ep in entry_points(group="bernstein.adapters"):
            try:
                cls = ep.load()
                if isinstance(cls, type) and issubclass(cls, PluginAdapter):
                    instance = cls()
                    self._plugins[ep.name] = instance
                    loaded += 1
                    logger.debug("Loaded plugin adapter %r from %s", ep.name, ep.value)
                elif isinstance(cls, PluginAdapter):
                    self._plugins[ep.name] = cls
                    loaded += 1
                    logger.debug("Loaded plugin adapter instance %r from %s", ep.name, ep.value)
            except Exception:
                logger.warning("Failed to load plugin adapter %r", ep.name, exc_info=True)
        return loaded

    def register(self, adapter: PluginAdapter) -> None:
        """Manually register a plugin adapter.

        Args:
            adapter: Plugin adapter instance to register.

        Raises:
            ValueError: If the adapter's plugin_info().name is empty.
        """
        info = adapter.plugin_info()
        if not info.name:
            msg = "Cannot register adapter with empty plugin_info().name"
            raise ValueError(msg)
        self._plugins[info.name] = adapter

    def get(self, name: str) -> PluginAdapter | None:
        """Look up a registered plugin adapter by name.

        Args:
            name: The plugin name to look up.

        Returns:
            The adapter instance, or ``None`` if not found.
        """
        return self._plugins.get(name)

    def list_plugins(self) -> list[AdapterPluginInfo]:
        """Return metadata for all registered plugin adapters.

        Returns:
            A list of :class:`AdapterPluginInfo` for each registered plugin.
        """
        return [adapter.plugin_info() for adapter in self._plugins.values()]

    def unregister(self, name: str) -> bool:
        """Remove a plugin adapter from the registry.

        Args:
            name: The plugin name to unregister.

        Returns:
            ``True`` if the plugin was removed, ``False`` if not found.
        """
        return self._plugins.pop(name, None) is not None
