"""ScalingBackend registry - first-party backends + pluggy entry points.

The registry is the single lookup surface for cluster autoscaling backends.
It loads first-party backends eagerly (``noop``, ``kubernetes-hpa``) and
third-party backends lazily via the ``bernstein.scaling_backends``
entry-point group. Third-party packages register new backends by declaring
an entry point in their ``pyproject.toml``::

    [project.entry-points."bernstein.scaling_backends"]
    nomad = "my_package.scaling:NomadScalingBackend"

This mirrors the pattern used by ``bernstein.core.sandbox.registry`` for
sandbox backends: every other pluggable surface in Bernstein (sandbox
backends, storage sinks, notification sinks, ...) resolves third-party
implementations through ``importlib.metadata`` entry points, and
``ScalingBackend`` previously did not - this module brings it in line.

Backends resolve lazily on first :meth:`ScalingBackendRegistry.get` so that
importing the registry never requires an optional backend's runtime
dependency (e.g. the Kubernetes CLI) to be present.
"""

from __future__ import annotations

import inspect
import logging
import threading
from importlib.metadata import entry_points
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bernstein.core.protocols.cluster.cluster_autoscaler import ScalingBackend

logger = logging.getLogger(__name__)

_ENTRY_POINT_GROUP = "bernstein.scaling_backends"

# Configuration written before this registry existed may spell the HPA
# backend with an underscore (``kubernetes_hpa``) rather than the backend's
# canonical hyphenated ``name`` attribute (``kubernetes-hpa``). Both must
# keep resolving to the same backend.
_LEGACY_NAME_ALIASES: dict[str, str] = {
    "kubernetes_hpa": "kubernetes-hpa",
}


class ScalingBackendRegistry:
    """Thread-safe mutable registry of cluster scaling backends.

    Module-level state isn't kept on the class so tests can rebuild a
    fresh registry without monkey-patching ``importlib.metadata``.
    """

    def __init__(self) -> None:
        self._backends: dict[str, ScalingBackend] = {}
        self._factories: dict[str, type[ScalingBackend]] = {}
        self._lock = threading.RLock()
        self._builtins_loaded = False
        self._entrypoints_loaded = False

    def register(
        self,
        name: str,
        backend: ScalingBackend | type[ScalingBackend],
    ) -> None:
        """Register a backend instance or class under *name*.

        Args:
            name: Canonical backend name; must match the backend's
                ``name`` attribute when an instance is supplied. Must be
                non-empty and unique.
            backend: Backend instance, or a zero-arg-constructible class.
                Classes are instantiated lazily on first :meth:`get`.

        Raises:
            ValueError: If *name* is empty or already registered.
        """
        normalized = name.strip()
        if not normalized:
            raise ValueError("Scaling backend name must be non-empty")
        with self._lock:
            if normalized in self._backends or normalized in self._factories:
                raise ValueError(f"Duplicate scaling backend: {normalized!r}")
            if inspect.isclass(backend):
                self._factories[normalized] = backend
            else:
                self._backends[normalized] = backend

    def unregister(self, name: str) -> None:
        """Remove *name* from the registry if present.

        Primarily useful for tests.
        """
        with self._lock:
            self._backends.pop(name, None)
            self._factories.pop(name, None)

    def get(self, name: str) -> ScalingBackend:
        """Return the backend registered under *name*.

        Loads built-in backends and entry-point backends on first call.
        Accepts legacy aliases (see :data:`_LEGACY_NAME_ALIASES`) so
        pre-registry configuration keeps working unchanged.

        Raises:
            KeyError: If no backend with that name is installed.
        """
        self.load_all()
        normalized = _LEGACY_NAME_ALIASES.get(name, name)
        with self._lock:
            if normalized in self._backends:
                return self._backends[normalized]
            factory = self._factories.get(normalized)
            if factory is None:
                available = ", ".join(sorted(self._all_names())) or "(none)"
                raise KeyError(f"Unknown scaling backend {name!r}. Available: {available}")
            instance = factory()
            self._backends[normalized] = instance
            return instance

    def list_names(self) -> list[str]:
        """Return the names of all registered backends, sorted."""
        self.load_all()
        with self._lock:
            return sorted(self._all_names())

    def list_backends(self) -> list[ScalingBackend]:
        """Return instantiated backends for every registered name.

        Factories are materialised on demand. Backends whose
        instantiation raises are skipped with a warning so one broken
        optional extra can't block the rest of the catalog.
        """
        self.load_all()
        results: list[ScalingBackend] = []
        for name in self.list_names():
            try:
                results.append(self.get(name))
            except Exception as exc:
                logger.warning("Scaling backend %r could not be instantiated: %s", name, exc)
        return results

    def load_all(self) -> None:
        """Ensure built-in and entry-point backends are registered.

        Idempotent - safe to call repeatedly. ``get``/``list_names``/
        ``list_backends`` all call this internally, so most callers never
        need to call it directly.
        """
        with self._lock:
            if not self._builtins_loaded:
                self._load_builtins()
                self._builtins_loaded = True
            if not self._entrypoints_loaded:
                self._load_entrypoints()
                self._entrypoints_loaded = True

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _all_names(self) -> set[str]:
        return set(self._backends.keys()) | set(self._factories.keys())

    def _load_builtins(self) -> None:
        # Local import to avoid a module-load cycle: cluster_autoscaler
        # imports this module (for ``create_backend``), so this module
        # must not import cluster_autoscaler at top level.
        from bernstein.core.protocols.cluster.cluster_autoscaler import (
            KubernetesHPABackend,
            NoOpBackend,
        )

        from bernstein.core.protocols.cluster.http_scaling_backend import (
            HTTPScalingBackend,
        )

        builtins: tuple[tuple[str, type[ScalingBackend]], ...] = (
            ("noop", NoOpBackend),
            ("kubernetes-hpa", KubernetesHPABackend),
            ("http", HTTPScalingBackend),
        )
        for name, cls in builtins:
            if name in self._all_names():
                continue
            self._factories[name] = cls

    def _load_entrypoints(self) -> None:
        try:
            eps = entry_points(group=_ENTRY_POINT_GROUP)
        except Exception as exc:
            logger.warning("Failed to enumerate scaling backend entry points: %s", exc)
            return
        for ep in eps:
            name = ep.name
            if name in self._all_names():
                logger.debug("Scaling entry-point %r shadows an earlier registration; skipping", name)
                continue
            try:
                loaded = ep.load()
            except Exception as exc:
                logger.warning("Failed to load scaling backend entry-point %r: %s", name, exc)
                continue
            if inspect.isclass(loaded):
                self._factories[name] = loaded  # type: ignore[assignment]  # dynamic registry
            else:
                self._backends[name] = loaded


_default_registry_instance = ScalingBackendRegistry()


def default_registry() -> ScalingBackendRegistry:
    """Return the process-wide default scaling backend registry."""
    return _default_registry_instance


def register_backend(
    name: str,
    backend: ScalingBackend | type[ScalingBackend],
) -> None:
    """Register *backend* under *name* in the default registry.

    Convenience wrapper for callers that don't want to import the
    ``ScalingBackendRegistry`` class directly.
    """
    _default_registry_instance.register(name, backend)


def get_backend(name: str) -> ScalingBackend:
    """Look up *name* in the default registry.

    Raises:
        KeyError: If no backend with that name is installed.
    """
    return _default_registry_instance.get(name)


def list_backends() -> list[ScalingBackend]:
    """Return installed backends from the default registry."""
    return _default_registry_instance.list_backends()


def list_backend_names() -> list[str]:
    """Return installed backend names from the default registry."""
    return _default_registry_instance.list_names()


def _reset_for_tests() -> None:
    """Drop cached state. Tests only - not part of the public API.

    Intentionally referenced by the test fixture in
    ``tests/unit/test_scaling_registry.py``. Pyright would flag it as
    unused at the module level otherwise.
    """
    global _default_registry_instance
    _default_registry_instance = ScalingBackendRegistry()


__all__ = [
    "ScalingBackendRegistry",
    "_reset_for_tests",
    "default_registry",
    "get_backend",
    "list_backend_names",
    "list_backends",
    "register_backend",
]
