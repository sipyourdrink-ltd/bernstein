"""Registry of external secret-store backends (issue #4984).

Adding a store must not require a patch to ``bernstein.core``. Stores come
from two sources, mirroring the tracker-adapter registry
(:mod:`bernstein.core.trackers.registry`) so store authors follow a contract
they already know:

1. **Plugin-contributed stores** discovered via the ``provide_secret_store``
   pluggy hookspec. A store that needs a vendor SDK ships this way, keeping
   the SDK import out of core.
2. **Programmatic registrations** through :func:`register_secret_store`, used
   by tests, in-process fakes, and third parties that load a store without
   pluggy.

No store ships built in: each concrete store is its own slice on top of the
contract in :mod:`bernstein.core.security.external_secret_store`.

The registry stores construction factories only. Instantiation -- with a base
URL, a role, a mount, whatever the store needs -- is the caller's job.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Iterator

    from bernstein.core.security.external_secret_store import ExternalSecretStore

log = logging.getLogger(__name__)

__all__ = [
    "DuplicateSecretStoreError",
    "SecretStoreFactory",
    "SecretStoreRegistration",
    "SecretStoreRegistry",
    "discover_plugin_secret_stores",
    "get_registry",
    "register_secret_store",
    "reset_registry_for_tests",
]


class SecretStoreFactory(Protocol):
    """Callable that constructs an :class:`ExternalSecretStore`."""

    def __call__(self, **kwargs: Any) -> ExternalSecretStore:
        """Construct a store with the supplied configuration."""


class DuplicateSecretStoreError(ValueError):
    """Raised when two stores register under the same name."""


@dataclass(frozen=True)
class SecretStoreRegistration:
    """A single registered external secret store.

    Attributes:
        name: Store identity used in a secret reference (``"<name>:<path>"``).
        factory: Callable that constructs the store.
        summary: One-line human-readable description.
        source: ``"plugin"`` or ``"programmatic"``; labels where it came from.
        provenance: Plugin name or module path, recorded so an operator can
            tell which code is holding their credentials.
    """

    name: str
    factory: SecretStoreFactory
    summary: str = ""
    source: str = "programmatic"
    provenance: str | None = None


class SecretStoreRegistry:
    """In-process registry of external secret-store factories.

    Not a singleton by design: tests build isolated instances. Module-level
    helpers wrap a process-wide default reached through :func:`get_registry`.
    """

    def __init__(self) -> None:
        self._entries: dict[str, SecretStoreRegistration] = {}

    def register(
        self,
        name: str,
        factory: SecretStoreFactory,
        *,
        summary: str = "",
        source: str = "programmatic",
        provenance: str | None = None,
        overwrite: bool = False,
    ) -> SecretStoreRegistration:
        """Register ``factory`` under ``name``.

        Raises:
            DuplicateSecretStoreError: When ``name`` is taken and
                ``overwrite`` is False. Silently letting a second store claim
                a name would repoint an operator's secrets without a trace.
        """
        if name in self._entries and not overwrite:
            existing = self._entries[name]
            msg = (
                f"Secret store {name!r} is already registered "
                f"(source={existing.source}, provenance={existing.provenance!r}). "
                "Pass overwrite=True to replace it."
            )
            raise DuplicateSecretStoreError(msg)
        entry = SecretStoreRegistration(
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

    def get(self, name: str) -> SecretStoreRegistration:
        """Return the registration for ``name``.

        Raises:
            KeyError: When no store is registered under ``name``.
        """
        try:
            return self._entries[name]
        except KeyError as exc:
            known = ", ".join(sorted(self._entries)) or "(none)"
            raise KeyError(f"Unknown secret store {name!r}. Registered: {known}.") from exc

    def create(self, name: str, **kwargs: Any) -> ExternalSecretStore:
        """Construct a store instance, forwarding ``kwargs`` to its factory."""
        return self.get(name).factory(**kwargs)

    def names(self) -> tuple[str, ...]:
        """Return registered names, plugin entries before programmatic ones."""
        priority = {"plugin": 0, "programmatic": 1}

        def sort_key(item: tuple[str, SecretStoreRegistration]) -> tuple[int, str]:
            return (priority.get(item[1].source, 2), item[0])

        return tuple(name for name, _ in sorted(self._entries.items(), key=sort_key))

    def __iter__(self) -> Iterator[SecretStoreRegistration]:
        for name in self.names():
            yield self._entries[name]

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._entries

    def __len__(self) -> int:
        return len(self._entries)


# ---------------------------------------------------------------------------
# Process-wide default registry
# ---------------------------------------------------------------------------

_registry: SecretStoreRegistry | None = None


def get_registry() -> SecretStoreRegistry:
    """Return the process-wide default registry, creating it on first use."""
    global _registry
    if _registry is None:
        _registry = SecretStoreRegistry()
    return _registry


def reset_registry_for_tests() -> None:
    """Clear the process-wide registry (test helper only)."""
    global _registry
    _registry = None


def register_secret_store(
    name: str,
    factory: SecretStoreFactory,
    *,
    summary: str = "",
    source: str = "programmatic",
    provenance: str | None = None,
    overwrite: bool = False,
) -> SecretStoreRegistration:
    """Register ``factory`` on the process-wide default registry."""
    return get_registry().register(
        name,
        factory,
        summary=summary,
        source=source,
        provenance=provenance,
        overwrite=overwrite,
    )


def discover_plugin_secret_stores(plugin_manager: Any | None = None) -> int:
    """Populate the registry with plugin-contributed stores.

    Calls ``provide_secret_store`` on every loaded plugin that implements it
    and registers each returned :class:`SecretStoreRegistration` (or
    ``(name, factory)`` tuple).

    Args:
        plugin_manager: Optional explicit manager; defaults to the
            orchestrator's via ``bernstein.plugins.manager.get_plugin_manager``.

    Returns:
        Number of stores newly registered.
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
    inner_pm = getattr(pm, "_pm", None)
    if inner_pm is None:
        log.debug("Plugin manager has no inner pluggy manager; skipping discovery.")
        return 0

    added = 0
    for plugin_name in getattr(pm, "_registered_names", []):
        plugin = inner_pm.get_plugin(plugin_name)
        if plugin is None or not hasattr(plugin, "provide_secret_store"):
            continue
        try:
            result = plugin.provide_secret_store()
        except Exception as exc:
            log.warning("Plugin %r provide_secret_store raised: %s", plugin_name, exc)
            continue
        added += _register_plugin_result(registry, plugin_name, result)
    return added


def _register_plugin_result(registry: SecretStoreRegistry, plugin_name: str, result: Any) -> int:
    """Register one plugin's return value; returns how many entries landed."""
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
        except DuplicateSecretStoreError as exc:
            log.warning(
                "Plugin %r tried to register duplicate secret store %r: %s",
                plugin_name,
                registration.name,
                exc,
            )
    return count


def _coerce_registration(raw: Any, plugin_name: str) -> SecretStoreRegistration | None:
    """Normalise a plugin-supplied registration into a dataclass."""
    if isinstance(raw, SecretStoreRegistration):
        return raw
    if isinstance(raw, tuple) and len(raw) == 2:
        name, factory = raw
        if isinstance(name, str) and callable(factory):
            return SecretStoreRegistration(
                name=name,
                factory=factory,
                source="plugin",
                provenance=plugin_name,
            )
    log.warning(
        "Plugin %r provide_secret_store returned unrecognised value %r; ignoring.",
        plugin_name,
        raw,
    )
    return None
