"""Registry of external-directory adapters.

A directory adapter is vendor code by definition, so it must be addable
without a change to ``bernstein.core``. Adapters therefore arrive here from
two sources:

1. **Plugin-contributed adapters** discovered via the
   ``provide_directory_adapter`` pluggy hookspec. Out-of-tree adapters (the
   ones that carry the vendor SDK) register this way.
2. **Programmatic registrations** through :func:`register_directory_adapter`,
   used by tests and by third parties that load their adapter without pluggy.

There is deliberately no built-in set: shipping a concrete directory adapter is
a separate slice, and the registry is what lets that slice land outside core.

The registry stores construction factories only; instantiating an adapter with
credentials and endpoints is the caller's job. Two adapters claiming the same
name raise :class:`DuplicateDirectoryAdapterError` rather than shadowing each
other, so a plugin cannot quietly take over a name another adapter answers to.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterator

    from bernstein.core.security.directory_bridge import DirectoryAdapter

log = logging.getLogger(__name__)

__all__ = [
    "DirectoryAdapterFactory",
    "DirectoryAdapterRegistration",
    "DirectoryAdapterRegistry",
    "DuplicateDirectoryAdapterError",
    "discover_plugin_directory_adapters",
    "get_registry",
    "register_directory_adapter",
    "reset_registry_for_tests",
]


class DirectoryAdapterFactory(Protocol):
    """Callable that constructs a directory adapter.

    A factory accepts arbitrary keyword arguments (base URL, tenant, credential
    handle) and returns something satisfying the bridge contract. The registry
    treats the factory as opaque.
    """

    def __call__(self, **kwargs: Any) -> DirectoryAdapter:
        """Construct a directory adapter with the supplied configuration."""


class DuplicateDirectoryAdapterError(ValueError):
    """Raised when two adapters register under the same name."""


@dataclass(frozen=True, slots=True)
class DirectoryAdapterRegistration:
    """A single registered directory adapter.

    Attributes:
        name: Short stable identifier used to select the adapter.
        factory: Callable that constructs the adapter.
        summary: One-line human-readable description.
        source: ``"plugin"`` or ``"programmatic"``.
        provenance: Plugin name or module path the adapter came from.
    """

    name: str
    factory: DirectoryAdapterFactory
    summary: str = ""
    source: str = "programmatic"
    provenance: str | None = None


class DirectoryAdapterRegistry:
    """In-process registry of directory adapter factories.

    Not a singleton by design: tests construct their own isolated instances.
    Module-level helpers wrap a process-wide default reached through
    :func:`get_registry`.
    """

    def __init__(self) -> None:
        self._entries: dict[str, DirectoryAdapterRegistration] = {}

    def register(
        self,
        name: str,
        factory: DirectoryAdapterFactory,
        *,
        summary: str = "",
        source: str = "programmatic",
        provenance: str | None = None,
        overwrite: bool = False,
    ) -> DirectoryAdapterRegistration:
        """Register ``factory`` under ``name``.

        Args:
            name: Stable identifier (case-sensitive).
            factory: Callable that constructs the adapter.
            summary: One-line human-readable summary.
            source: Origin label (``"plugin"`` or ``"programmatic"``).
            provenance: Plugin name or module path.
            overwrite: Replace an existing registration instead of raising.

        Returns:
            The stored :class:`DirectoryAdapterRegistration`.

        Raises:
            DuplicateDirectoryAdapterError: When ``name`` is already
                registered and ``overwrite`` is False.
        """
        if name in self._entries and not overwrite:
            existing = self._entries[name]
            msg = (
                f"Directory adapter {name!r} is already registered "
                f"(source={existing.source}, provenance={existing.provenance!r}). "
                "Pass overwrite=True to replace it."
            )
            raise DuplicateDirectoryAdapterError(msg)
        entry = DirectoryAdapterRegistration(
            name=name,
            factory=factory,
            summary=summary,
            source=source,
            provenance=provenance,
        )
        self._entries[name] = entry
        return entry

    def unregister(self, name: str) -> None:
        """Remove ``name`` from the registry (no-op if absent)."""
        self._entries.pop(name, None)

    def get(self, name: str) -> DirectoryAdapterRegistration:
        """Return the registration for ``name``.

        Raises:
            KeyError: When no adapter is registered under ``name``.
        """
        try:
            return self._entries[name]
        except KeyError as exc:
            known = ", ".join(sorted(self._entries)) or "(none)"
            msg = f"Unknown directory adapter {name!r}. Registered: {known}."
            raise KeyError(msg) from exc

    def create(self, name: str, **kwargs: Any) -> DirectoryAdapter:
        """Construct an adapter instance, forwarding ``kwargs`` to its factory."""
        return self.get(name).factory(**kwargs)

    def names(self) -> tuple[str, ...]:
        """Return the registered names, plugin entries before programmatic ones."""
        priority = {"plugin": 0, "programmatic": 1}

        def sort_key(item: tuple[str, DirectoryAdapterRegistration]) -> tuple[int, str]:
            return (priority.get(item[1].source, 2), item[0])

        return tuple(name for name, _ in sorted(self._entries.items(), key=sort_key))

    def __iter__(self) -> Iterator[DirectoryAdapterRegistration]:
        for name in self.names():
            yield self._entries[name]

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._entries

    def __len__(self) -> int:
        return len(self._entries)


# ---------------------------------------------------------------------------
# Process-wide default registry
# ---------------------------------------------------------------------------


_registry: DirectoryAdapterRegistry | None = None


def get_registry() -> DirectoryAdapterRegistry:
    """Return the process-wide default registry, creating it on first use.

    Plugin discovery is *not* triggered here; callers that want plugin
    adapters also call :func:`discover_plugin_directory_adapters`.
    """
    global _registry
    if _registry is None:
        _registry = DirectoryAdapterRegistry()
    return _registry


def reset_registry_for_tests() -> None:
    """Clear the process-wide registry (test helper only)."""
    global _registry
    _registry = None


def register_directory_adapter(
    name: str,
    factory: DirectoryAdapterFactory,
    *,
    summary: str = "",
    source: str = "programmatic",
    provenance: str | None = None,
    overwrite: bool = False,
) -> DirectoryAdapterRegistration:
    """Register ``factory`` on the process-wide default registry."""
    return get_registry().register(
        name,
        factory,
        summary=summary,
        source=source,
        provenance=provenance,
        overwrite=overwrite,
    )


def discover_plugin_directory_adapters(plugin_manager: Any | None = None) -> int:
    """Populate the registry with plugin-contributed directory adapters.

    Iterates over loaded pluggy plugins, calls ``provide_directory_adapter``
    on each that implements it, and registers every returned
    :class:`DirectoryAdapterRegistration` (or ``(name, factory)`` tuple).

    Args:
        plugin_manager: Optional explicit plugin manager. When None the
            orchestrator's default manager is fetched from
            :func:`bernstein.plugins.manager.get_plugin_manager`.

    Returns:
        Number of plugin adapters newly registered.
    """
    pm = plugin_manager
    if pm is None:
        try:
            from bernstein.plugins.manager import get_plugin_manager

            pm = get_plugin_manager()
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("Could not obtain plugin manager: %s", exc)
            return 0

    registry = get_registry()
    added = 0
    registered_names = getattr(pm, "_registered_names", [])
    inner_pm = getattr(pm, "_pm", None)

    if inner_pm is None:
        log.debug("Plugin manager has no inner pluggy manager; skipping discovery.")
        return 0

    for plugin_name in registered_names:
        plugin = inner_pm.get_plugin(plugin_name)
        if plugin is None or not hasattr(plugin, "provide_directory_adapter"):
            continue
        try:
            result = plugin.provide_directory_adapter()
        except Exception as exc:
            log.warning("Plugin %r provide_directory_adapter raised: %s", plugin_name, exc)
            continue
        added += _register_plugin_result(registry, plugin_name, result)
    return added


def _register_plugin_result(
    registry: DirectoryAdapterRegistry,
    plugin_name: str,
    result: Any,
) -> int:
    """Register one plugin's return value; see the hookspec for the shapes."""
    if result is None:
        return 0
    items = result if isinstance(result, list) else [result]
    count = 0
    for raw in items:
        registration = _coerce_registration(raw, plugin_name)
        if registration is None:
            continue
        try:
            registry.register(
                registration.name,
                registration.factory,
                summary=registration.summary,
                source="plugin",
                provenance=registration.provenance or plugin_name,
                overwrite=False,
            )
            count += 1
        except DuplicateDirectoryAdapterError as exc:
            log.warning(
                "Plugin %r tried to register duplicate directory adapter %r: %s",
                plugin_name,
                registration.name,
                exc,
            )
    return count


def _coerce_registration(raw: Any, plugin_name: str) -> DirectoryAdapterRegistration | None:
    """Normalise a plugin-supplied registration into a registration object."""
    if isinstance(raw, DirectoryAdapterRegistration):
        return raw
    if isinstance(raw, tuple) and len(raw) == 2:
        name, factory = raw
        if isinstance(name, str) and callable(factory):
            return DirectoryAdapterRegistration(
                name=name,
                factory=factory,
                source="plugin",
                provenance=plugin_name,
            )
    log.warning(
        "Plugin %r provide_directory_adapter returned unrecognised value %r; ignoring.",
        plugin_name,
        raw,
    )
    return None
