"""Adapter registry — look up CLI adapters by name."""

from __future__ import annotations

import inspect
import logging
from importlib.metadata import entry_points

from bernstein.adapters.aider import AiderAdapter
from bernstein.adapters.amp import AmpAdapter
from bernstein.adapters.base import CLIAdapter
from bernstein.adapters.claude import ClaudeCodeAdapter
from bernstein.adapters.codex import CodexAdapter
from bernstein.adapters.cursor import CursorAdapter
from bernstein.adapters.gemini import GeminiAdapter
from bernstein.adapters.generic import GenericAdapter
from bernstein.adapters.goose import GooseAdapter
from bernstein.adapters.kilo import KiloAdapter
from bernstein.adapters.kiro import KiroAdapter
from bernstein.adapters.mock import MockAgentAdapter
from bernstein.adapters.opencode import OpenCodeAdapter
from bernstein.adapters.qwen import QwenAdapter
from bernstein.adapters.roo_code import RooCodeAdapter

logger = logging.getLogger(__name__)

_ADAPTERS: dict[str, type[CLIAdapter] | CLIAdapter] = {
    "amp": AmpAdapter,
    "aider": AiderAdapter,
    "claude": ClaudeCodeAdapter,
    "codex": CodexAdapter,
    "cursor": CursorAdapter,
    "gemini": GeminiAdapter,
    "goose": GooseAdapter,
    "kilo": KiloAdapter,
    "kiro": KiroAdapter,
    "mock": MockAgentAdapter,
    "opencode": OpenCodeAdapter,
    "qwen": QwenAdapter,
    "roo-code": RooCodeAdapter,
}

_ENTRYPOINTS_LOADED = False


def _load_entrypoint_adapters() -> None:
    """Discover and register adapters from the ``bernstein.adapters`` entry-point group.

    Called once on first use. Silently skips malformed plugins.
    """
    global _ENTRYPOINTS_LOADED
    if _ENTRYPOINTS_LOADED:
        return
    _ENTRYPOINTS_LOADED = True
    for ep in entry_points(group="bernstein.adapters"):
        try:
            loaded = ep.load()
            name = ep.name
            if (inspect.isclass(loaded) and issubclass(loaded, CLIAdapter)) or isinstance(loaded, CLIAdapter):
                _ADAPTERS[name] = loaded
            else:
                logger.warning(
                    "Ignoring entry-point adapter %r: expected CLIAdapter subclass or instance, got %r",
                    name,
                    loaded,
                )
        except Exception as exc:  # pragma: no cover
            logger.warning("Failed to load entry-point adapter %r: %s", ep.name, exc)


def get_adapter(cli_name: str) -> CLIAdapter:
    """Get adapter by name: 'aider', 'claude', 'codex', 'gemini', 'kilo', 'qwen', or 'generic'.

    For 'generic', returns a GenericAdapter with default settings.
    For known adapters, instantiates the corresponding class.
    Third-party adapters are discovered from the ``bernstein.adapters`` entry-point group.

    Args:
        cli_name: Adapter name to look up.

    Returns:
        An instantiated CLIAdapter.

    Raises:
        ValueError: If the adapter name is not recognized.
    """
    if cli_name == "generic":
        return GenericAdapter(cli_command="generic-cli", display_name="Generic CLI")

    _load_entrypoint_adapters()

    adapter_cls = _ADAPTERS.get(cli_name)
    if adapter_cls is None:
        available = ", ".join(sorted([*_ADAPTERS.keys(), "generic"]))
        raise ValueError(f"Unknown adapter '{cli_name}'. Available: {available}")

    if isinstance(adapter_cls, CLIAdapter):
        return adapter_cls
    return adapter_cls()


def register_adapter(name: str, adapter: type[CLIAdapter] | CLIAdapter) -> None:
    """Register a custom adapter by name.

    Args:
        name: Name to register under.
        adapter: Adapter class or instance.
    """
    _ADAPTERS[name] = adapter
