"""SandboxBackend registry — first-party backends + pluggy entry points.

The registry is the single lookup surface for sandbox backends. It
loads first-party backends eagerly (``worktree``, ``docker``) and
cloud backends lazily via the ``bernstein.sandbox_backends`` entry-point
group. Third-party packages register new backends by declaring an
entry point in their ``pyproject.toml``::

    [project.entry-points."bernstein.sandbox_backends"]
    e2b = "bernstein.core.sandbox.backends.e2b:E2BSandboxBackend"

Optional backends (``e2b``, ``modal``) import their provider SDK lazily
inside the backend module, so importing the registry never crashes even
when the SDK is missing — instantiation of the corresponding backend
will raise a clear error instead.
"""

from __future__ import annotations

import inspect
import logging
import threading
from importlib.metadata import entry_points
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bernstein.core.sandbox.backend import SandboxBackend

logger = logging.getLogger(__name__)

_ENTRY_POINT_GROUP = "bernstein.sandbox_backends"


class _Registry:
    """Thread-safe mutable registry of sandbox backends.

    Module-level state isn't kept on the class so tests can rebuild a
    fresh registry without monkey-patching ``importlib.metadata``.
    """

    def __init__(self) -> None:
        self._backends: dict[str, SandboxBackend] = {}
        self._factories: dict[str, type[SandboxBackend]] = {}
        self._lock = threading.RLock()
        self._builtins_loaded = False
        self._entrypoints_loaded = False

    def register(
        self,
        name: str,
        backend: SandboxBackend | type[SandboxBackend],
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
            raise ValueError("Sandbox backend name must be non-empty")
        with self._lock:
            if normalized in self._backends or normalized in self._factories:
                raise ValueError(f"Duplicate sandbox backend: {normalized!r}")
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

    def get(self, name: str) -> SandboxBackend:
        """Return the backend registered under *name*.

        Loads built-in backends and entry-point backends on first call.

        Raises:
            KeyError: If no backend with that name is installed.
        """
        self._ensure_loaded()
        with self._lock:
            if name in self._backends:
                return self._backends[name]
            factory = self._factories.get(name)
            if factory is None:
                available = ", ".join(sorted(self._all_names())) or "(none)"
                raise KeyError(f"Unknown sandbox backend {name!r}. Available: {available}")
            instance = factory()
            self._backends[name] = instance
            return instance

    def list_names(self) -> list[str]:
        """Return the names of all registered backends, sorted."""
        self._ensure_loaded()
        with self._lock:
            return sorted(self._all_names())

    def list_backends(self) -> list[SandboxBackend]:
        """Return instantiated backends for every registered name.

        Factories are materialised on demand. Backends whose
        instantiation raises are skipped with a warning so one broken
        optional extra can't block the rest of the catalog.
        """
        self._ensure_loaded()
        results: list[SandboxBackend] = []
        for name in self.list_names():
            try:
                results.append(self.get(name))
            except Exception as exc:
                logger.warning("Sandbox backend %r could not be instantiated: %s", name, exc)
        return results

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _all_names(self) -> set[str]:
        return set(self._backends.keys()) | set(self._factories.keys())

    def _ensure_loaded(self) -> None:
        with self._lock:
            if not self._builtins_loaded:
                self._load_builtins()
                self._builtins_loaded = True
            if not self._entrypoints_loaded:
                self._load_entrypoints()
                self._entrypoints_loaded = True

    def _load_builtins(self) -> None:
        # Imports are local to keep the module-load graph minimal and to
        # tolerate partial installs (a missing optional dep must never
        # break registry import).
        from bernstein.core.sandbox.backends.docker import DockerSandboxBackend
        from bernstein.core.sandbox.backends.worktree import WorktreeSandboxBackend

        builtins: tuple[tuple[str, type[SandboxBackend]], ...] = (
            ("worktree", WorktreeSandboxBackend),
            ("docker", DockerSandboxBackend),
        )
        for name, cls in builtins:
            if name in self._all_names():
                continue
            self._factories[name] = cls

    def _load_entrypoints(self) -> None:
        try:
            eps = entry_points(group=_ENTRY_POINT_GROUP)
        except Exception as exc:
            logger.warning("Failed to enumerate sandbox backend entry points: %s", exc)
            return
        for ep in eps:
            name = ep.name
            if name in self._all_names():
                logger.debug("Sandbox entry-point %r shadows an earlier registration; skipping", name)
                continue
            try:
                loaded = ep.load()
            except Exception as exc:
                logger.warning("Failed to load sandbox backend entry-point %r: %s", name, exc)
                continue
            if inspect.isclass(loaded):
                self._factories[name] = loaded
            else:
                self._backends[name] = loaded


_default_registry_instance = _Registry()


def default_registry() -> _Registry:
    """Return the process-wide default sandbox registry."""
    return _default_registry_instance


def register_backend(
    name: str,
    backend: SandboxBackend | type[SandboxBackend],
) -> None:
    """Register *backend* under *name* in the default registry.

    Convenience wrapper for callers that don't want to import the
    ``_Registry`` class directly.
    """
    _default_registry_instance.register(name, backend)


def get_backend(name: str) -> SandboxBackend:
    """Look up *name* in the default registry.

    Raises:
        KeyError: If no backend with that name is installed.
    """
    return _default_registry_instance.get(name)


def list_backends() -> list[SandboxBackend]:
    """Return installed backends from the default registry."""
    return _default_registry_instance.list_backends()


def list_backend_names() -> list[str]:
    """Return installed backend names from the default registry."""
    return _default_registry_instance.list_names()


def _reset_for_tests() -> None:
    """Drop cached state. Tests only — not part of the public API.

    Intentionally referenced by the test fixture in
    ``tests/unit/sandbox/test_registry.py``. Pyright would flag it
    as unused at the module level otherwise.
    """
    global _default_registry_instance
    _default_registry_instance = _Registry()


__all__ = [
    "_reset_for_tests",
    "default_registry",
    "get_backend",
    "list_backend_names",
    "list_backends",
    "register_backend",
]
