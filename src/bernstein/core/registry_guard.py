"""Shared duplicate-registration guard for the registry classes under ``bernstein``.

At least 20 independent registry classes exist under ``src/bernstein``
(adapters, trackers, tunnels, sandbox backends, storage sinks, MCP servers,
trigger sources, agent definitions, and more), and each has historically
reinvented its own duplicate-id handling -- some correctly (raising
``ValueError`` under a lock), some not (``AgentRegistry.register_definition``
promised ``ValueError`` in its own docstring and logged a warning instead).

``DuplicateGuard`` is the one piece of that logic every registry composes
into its own ``register`` method, so the check exists once, is tested once,
and a registry that adopts it cannot silently regress back to warn-and-overwrite.
A registry keeps everything else about how it stores its own values --
``DuplicateGuard`` only tracks, per key, which module registered it first::

    class WidgetRegistry:
        def __init__(self) -> None:
            self._widgets: dict[str, Widget] = {}
            self._guard = DuplicateGuard("widget")

        def register(self, name: str, widget: Widget) -> None:
            self._guard.register(name, caller_module_name())
            self._widgets[name] = widget

A registry that already raises its own exception type (for example
``core/trackers/registry.py``'s ``DuplicateTrackerError``) keeps doing so by
passing ``error_type=`` -- ``DuplicateGuard`` only requires that the type
accept a single message string, which is true of any plain ``Exception`` or
``ValueError`` subclass that does not override ``__init__``.
"""

from __future__ import annotations

import sys
import threading

__all__ = ["DuplicateGuard", "DuplicateRegistrationError", "caller_module_name"]


class DuplicateRegistrationError(ValueError):
    """Raised when a registry key is registered a second time.

    A plain ``ValueError`` subclass, so registries (and their callers) that
    already ``except ValueError`` around a ``register`` call keep working
    unchanged. The message always names both the module that registered the
    key first and the module whose second registration was refused.
    """


def caller_module_name(depth: int = 1) -> str:
    """Return the ``__name__`` of the module that called the caller of this function.

    Call this with no arguments from inside a registry's public ``register``
    method to capture *its* caller's module -- the code that actually
    invoked ``register`` -- without every call site having to pass its own
    ``__name__`` explicitly::

        def register(self, name: str, value: Any) -> None:
            module_path = caller_module_name()
            self._guard.register(name, module_path)
            ...

    ``depth`` counts additional frames to skip beyond the immediate caller,
    for a registry that wraps its public ``register`` in another layer
    (a decorator or a mixin's ``register`` calling a private ``_register``)
    before reaching the code that should be named.

    Returns ``"<unknown>"`` if the call stack is shallower than requested
    (for example when called directly from a REPL) rather than raising --
    naming a mis-detected caller in an error message is far less costly
    than the guard itself crashing on a legitimate registration.
    """
    frame = sys._getframe(depth + 1)  # pyright: ignore[reportPrivateUsage]
    try:
        return frame.f_globals.get("__name__", "<unknown>")
    finally:
        del frame


class DuplicateGuard:
    """Thread-safe bookkeeping of which module registered which key first.

    Args:
        registry_name: Short human-readable label used in error messages
            (for example ``"agent definition"`` or ``"sandbox backend"``).
    """

    def __init__(self, registry_name: str) -> None:
        self._registry_name = registry_name
        self._modules: dict[str, str] = {}
        self._lock = threading.RLock()

    def register(
        self,
        key: str,
        module_path: str,
        *,
        error_type: type[Exception] = DuplicateRegistrationError,
    ) -> None:
        """Record that ``module_path`` registered ``key``.

        Args:
            key: The registry id being registered.
            module_path: The ``__name__`` of the module performing the
                registration, typically obtained via :func:`caller_module_name`.
            error_type: Exception type to raise on a duplicate. Must be
                constructible from a single message string. Defaults to
                :class:`DuplicateRegistrationError`.

        Raises:
            error_type: If ``key`` is already registered, naming both the
                existing and the incoming module path.
        """
        with self._lock:
            existing_module = self._modules.get(key)
            if existing_module is not None:
                raise error_type(
                    f"{self._registry_name} {key!r} is already registered from "
                    f"{existing_module!r}; refusing second registration from {module_path!r}"
                )
            self._modules[key] = module_path

    def forget(self, key: str) -> None:
        """Drop bookkeeping for ``key``, allowing it to be registered again.

        Call this from a registry's ``unregister``/``remove`` method so a
        legitimate re-registration after removal is not mistaken for a
        duplicate.
        """
        with self._lock:
            self._modules.pop(key, None)

    def module_path_for(self, key: str) -> str | None:
        """Return the module path that registered ``key``, or ``None``."""
        with self._lock:
            return self._modules.get(key)
