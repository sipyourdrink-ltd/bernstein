"""Plugin error collection and reporting.

Collects errors encountered during plugin discovery, loading, and execution
so they can be surfaced in ``bernstein doctor`` and ``bernstein status``.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class PluginError:
    """A single plugin loading or execution error.

    Attributes:
        plugin_name: Name or path of the plugin that failed.
        phase: Phase where the error occurred (discover, load, execute, hook).
        message: Human-readable error description.
        traceback: Optional full traceback for debugging.
    """

    plugin_name: str
    phase: str
    message: str
    traceback: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "plugin_name": self.plugin_name,
            "phase": self.phase,
            "message": self.message,
            "traceback": self.traceback,
        }


class PluginErrorRegistry:
    """Thread-safe registry of plugin errors.

    Errors are added during plugin loading and read by doctor/status
    for display.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._errors: list[PluginError] = []

    def add(self, error: PluginError) -> None:
        """Record a plugin error."""
        with self._lock:
            self._errors.append(error)

    def add_simple(self, plugin_name: str, phase: str, message: str, exc: Exception | None = None) -> None:
        """Convenience: add an error with optional exception details.

        Args:
            plugin_name: Plugin name or path.
            phase: Loading phase (discover, load, execute, hook).
            message: Human-readable description.
            exc: Optional exception to extract traceback from.
        """
        tb = ""
        if exc is not None:
            import traceback as _tb

            tb = "".join(_tb.format_exception(type(exc), exc, exc.__traceback__))
        self.add(PluginError(plugin_name=plugin_name, phase=phase, message=message, traceback=tb))

    def get_errors(self) -> list[PluginError]:
        """Return a copy of all recorded errors."""
        with self._lock:
            return list(self._errors)

    def clear(self) -> None:
        """Clear all recorded errors."""
        with self._lock:
            self._errors.clear()

    def has_errors(self) -> bool:
        """Return True if any plugin errors have been recorded."""
        with self._lock:
            return bool(self._errors)

    def count(self) -> int:
        """Return the number of recorded plugin errors."""
        with self._lock:
            return len(self._errors)


# ---------------------------------------------------------------------------
# Module-level singleton
# ---------------------------------------------------------------------------

_registry = PluginErrorRegistry()


def get_plugin_errors() -> PluginErrorRegistry:
    """Get the global plugin error registry."""
    return _registry


def report_plugin_error(plugin_name: str, phase: str, message: str, exc: Exception | None = None) -> None:
    """Record a plugin error for later display.

    Args:
        plugin_name: Plugin name or path.
        phase: Loading phase.
        message: Human-readable description.
        exc: Optional exception for traceback.
    """
    _registry.add_simple(plugin_name, phase, message, exc)
