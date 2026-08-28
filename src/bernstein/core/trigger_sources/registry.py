"""Trigger-source registry for built-in and third-party source classes."""

from __future__ import annotations

import inspect
import logging
import threading
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, cast

from bernstein.core.trigger_sources.artifact import ArtifactSource
from bernstein.core.trigger_sources.file_watch import FileWatchSource
from bernstein.core.trigger_sources.odata_poll import OdataPollSource

if TYPE_CHECKING:
    from bernstein.core.trigger_sources import TriggerSource

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "bernstein.triggers"

_BUILTIN_SOURCES: dict[str, type[TriggerSource]] = {
    "artifact": ArtifactSource,
    "file_watch": FileWatchSource,
    "odata_poll": OdataPollSource,
}


class Registry:
    """Thread-safe registry of trigger-source classes.

    Sources are returned as classes because their construction requirements
    differ: some are stateless while others need connection configuration.
    """

    def __init__(self) -> None:
        self._sources: dict[str, type[TriggerSource]] = {}
        self._lock = threading.RLock()
        self._builtins_loaded = False
        self._entrypoints_loaded = False

    def register(self, name: str, source: type[TriggerSource]) -> None:
        """Register a source class under *name*."""
        normalised = name.strip()
        if not normalised:
            raise ValueError("Trigger source name must be non-empty")
        if not inspect.isclass(source) or not callable(getattr(source, "normalize", None)):
            raise TypeError("Trigger source must be a class with a normalize method")
        with self._lock:
            if normalised in self._sources:
                raise ValueError(f"Duplicate trigger source: {normalised!r}")
            self._sources[normalised] = source

    def get(self, name: str) -> type[TriggerSource]:
        """Return the registered source class named *name*."""
        self._ensure_loaded()
        with self._lock:
            try:
                return self._sources[name]
            except KeyError:
                available = ", ".join(sorted(self._sources)) or "(none)"
                raise KeyError(f"Unknown trigger source {name!r}. Available: {available}") from None

    def list_names(self) -> list[str]:
        """Return all registered source names in sorted order."""
        self._ensure_loaded()
        with self._lock:
            return sorted(self._sources)

    def _ensure_loaded(self) -> None:
        with self._lock:
            if not self._builtins_loaded:
                for name, source in _BUILTIN_SOURCES.items():
                    self._sources.setdefault(name, source)
                self._builtins_loaded = True
            if not self._entrypoints_loaded:
                self._load_entrypoints()
                self._entrypoints_loaded = True

    def _load_entrypoints(self) -> None:
        try:
            plugins = entry_points(group=ENTRY_POINT_GROUP)
        except Exception as exc:
            logger.warning("Failed to enumerate trigger-source entry points: %s", exc)
            return
        for entry_point in plugins:
            if entry_point.name in self._sources:
                logger.debug("Trigger entry-point %r shadows an existing source; skipping", entry_point.name)
                continue
            try:
                source = cast("type[TriggerSource]", entry_point.load())
                self.register(entry_point.name, source)
            except Exception as exc:
                logger.warning("Failed to load trigger entry-point %r: %s", entry_point.name, exc)


_default_registry_instance = Registry()


def default_registry() -> Registry:
    """Return the process-wide trigger-source registry."""
    return _default_registry_instance


def get_trigger_source(name: str) -> type[TriggerSource]:
    """Look up a trigger-source class by name."""
    return _default_registry_instance.get(name)


def list_trigger_source_names() -> list[str]:
    """Return all built-in and installed trigger-source names."""
    return _default_registry_instance.list_names()


def _reset_for_tests() -> None:
    """Replace the process-wide registry. Tests only."""
    global _default_registry_instance
    _default_registry_instance = Registry()


__all__ = [
    "ENTRY_POINT_GROUP",
    "Registry",
    "_reset_for_tests",
    "default_registry",
    "get_trigger_source",
    "list_trigger_source_names",
]
