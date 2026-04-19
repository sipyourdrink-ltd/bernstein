"""ArtifactSink registry — first-party sinks + pluggy entry points.

The registry is the single lookup surface for artifact sinks. It loads
the first-party sinks eagerly (``local_fs``) and the cloud sinks lazily
— the cloud sink classes are imported only on first :meth:`get`, so
their optional provider SDKs never block import when the extras are
missing. Third-party packages add sinks via the
``bernstein.storage_sinks`` entry-point group::

    [project.entry-points."bernstein.storage_sinks"]
    my_sink = "mypkg.storage:MyArtifactSink"

The shape mirrors
:mod:`bernstein.core.sandbox.registry` so plugin authors familiar with
sandbox backends feel at home.
"""

from __future__ import annotations

import inspect
import logging
import threading
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from bernstein.core.storage.sink import ArtifactSink

logger = logging.getLogger(__name__)

_ENTRY_POINT_GROUP = "bernstein.storage_sinks"


class _Registry:
    """Thread-safe mutable registry of artifact sinks.

    Mirrors
    :class:`bernstein.core.sandbox.registry._Registry` so operators
    familiar with the sandbox surface find the storage surface
    predictable. Sinks can be registered as instances or as zero-arg
    factory classes; factories are instantiated lazily on first
    :meth:`get`.
    """

    def __init__(self) -> None:
        self._sinks: dict[str, ArtifactSink] = {}
        self._factories: dict[str, Any] = {}
        self._lock = threading.RLock()
        self._builtins_loaded = False
        self._entrypoints_loaded = False

    def register(
        self,
        name: str,
        sink: ArtifactSink | Any,
    ) -> None:
        """Register *sink* under *name*.

        Args:
            name: Canonical sink name. Must match the sink's ``name``
                attribute when an instance is provided.
            sink: Instance or zero-arg factory class.

        Raises:
            ValueError: If *name* is empty or already registered.
        """
        normalised = name.strip()
        if not normalised:
            raise ValueError("Artifact sink name must be non-empty")
        with self._lock:
            if normalised in self._sinks or normalised in self._factories:
                raise ValueError(f"Duplicate artifact sink: {normalised!r}")
            if inspect.isclass(sink):
                self._factories[normalised] = sink
            else:
                self._sinks[normalised] = sink

    def unregister(self, name: str) -> None:
        """Remove *name* from the registry if present (tests only)."""
        with self._lock:
            self._sinks.pop(name, None)
            self._factories.pop(name, None)

    def get(self, name: str) -> ArtifactSink:
        """Return the sink registered under *name*.

        Loads built-in sinks and entry-point sinks on first call.

        Raises:
            KeyError: If no sink with that name is installed.
        """
        self._ensure_loaded()
        with self._lock:
            if name in self._sinks:
                return self._sinks[name]
            factory = self._factories.get(name)
            if factory is None:
                available = ", ".join(sorted(self._all_names())) or "(none)"
                raise KeyError(f"Unknown artifact sink {name!r}. Available: {available}")
            instance = factory()
            self._sinks[name] = instance
            return instance

    def list_names(self) -> list[str]:
        """Return the names of all registered sinks, sorted."""
        self._ensure_loaded()
        with self._lock:
            return sorted(self._all_names())

    def list_sinks(self) -> list[ArtifactSink]:
        """Return instantiated sinks for every registered name.

        Sinks whose factory raises (e.g. missing optional SDK) are
        skipped with a warning so one broken extra can't block the
        rest of the catalogue.
        """
        self._ensure_loaded()
        results: list[ArtifactSink] = []
        for name in self.list_names():
            try:
                results.append(self.get(name))
            except Exception as exc:
                logger.warning("Artifact sink %r could not be instantiated: %s", name, exc)
        return results

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _all_names(self) -> set[str]:
        return set(self._sinks.keys()) | set(self._factories.keys())

    def _ensure_loaded(self) -> None:
        with self._lock:
            if not self._builtins_loaded:
                self._load_builtins()
                self._builtins_loaded = True
            if not self._entrypoints_loaded:
                self._load_entrypoints()
                self._entrypoints_loaded = True

    def _load_builtins(self) -> None:
        # Import LocalFsSink eagerly — the class has no optional deps.
        # Cloud sinks are registered lazily via ``_register_cloud_factory``
        # so their SDK imports do not fire when the extras aren't
        # installed (the factory itself raises a clear error on call).
        from bernstein.core.storage.sinks.local_fs import LocalFsSink

        if "local_fs" not in self._all_names():
            self._factories["local_fs"] = LocalFsSink

        self._register_cloud_factory(
            "s3",
            "bernstein.core.storage.sinks.s3",
            "S3ArtifactSink",
        )
        self._register_cloud_factory(
            "gcs",
            "bernstein.core.storage.sinks.gcs",
            "GCSArtifactSink",
        )
        self._register_cloud_factory(
            "azure_blob",
            "bernstein.core.storage.sinks.azure_blob",
            "AzureBlobArtifactSink",
        )
        self._register_cloud_factory(
            "r2",
            "bernstein.core.storage.sinks.r2",
            "R2ArtifactSink",
        )

    def _register_cloud_factory(self, name: str, module: str, attr: str) -> None:
        """Register a cloud-sink factory that imports lazily.

        The factory is a small closure that imports the sink class only
        when invoked; this keeps the registry usable even when the
        optional SDK for the sink is not installed. The factory ignores
        arguments so the registry can instantiate it with no config —
        callers that need non-default wiring should construct the sink
        directly and register the instance.
        """
        if name in self._all_names():
            return

        def _factory() -> ArtifactSink:
            import importlib

            try:
                mod = importlib.import_module(module)
            except ImportError as exc:  # pragma: no cover - provider-specific
                raise RuntimeError(f"Sink {name!r} unavailable: {exc}") from exc
            cls = getattr(mod, attr, None)
            if cls is None:
                raise RuntimeError(f"Module {module} does not expose {attr}")
            return cls()  # type: ignore[no-any-return]

        self._factories[name] = _factory

    def _load_entrypoints(self) -> None:
        try:
            eps = entry_points(group=_ENTRY_POINT_GROUP)
        except Exception as exc:
            logger.warning("Failed to enumerate artifact sink entry points: %s", exc)
            return
        for ep in eps:
            name = ep.name
            if name in self._all_names():
                logger.debug("Artifact sink entry-point %r shadows existing; skipping", name)
                continue
            try:
                loaded = ep.load()
            except Exception as exc:
                logger.warning("Failed to load artifact sink entry-point %r: %s", name, exc)
                continue
            if inspect.isclass(loaded):
                self._factories[name] = loaded
            else:
                self._sinks[name] = loaded


_default_registry_instance = _Registry()


def default_registry() -> _Registry:
    """Return the process-wide default sink registry."""
    return _default_registry_instance


def register_sink(name: str, sink: ArtifactSink | Any) -> None:
    """Register *sink* under *name* in the default registry."""
    _default_registry_instance.register(name, sink)


def get_sink(name: str) -> ArtifactSink:
    """Look up *name* in the default registry."""
    return _default_registry_instance.get(name)


def list_sinks() -> list[ArtifactSink]:
    """Return installed sinks from the default registry."""
    return _default_registry_instance.list_sinks()


def list_sink_names() -> list[str]:
    """Return installed sink names from the default registry."""
    return _default_registry_instance.list_names()


def _reset_for_tests() -> None:
    """Drop cached state. Tests only — not part of the public API."""
    global _default_registry_instance
    _default_registry_instance = _Registry()


__all__ = [
    "_reset_for_tests",
    "default_registry",
    "get_sink",
    "list_sink_names",
    "list_sinks",
    "register_sink",
]
