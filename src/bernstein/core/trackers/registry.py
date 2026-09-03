"""Tracker adapter registry.

Centralises discovery of tracker adapters from three sources:

1. **Built-in adapters** wired into ``core/trackers/`` and
   ``core/trackers/builtin/`` (Linear, Jira Cloud/DC, GitHub Projects v2,
   GitLab, ClickUp, Asana, Plane, ServiceNow).
2. **Plugin-contributed adapters** discovered via the
   ``provide_tracker_adapter`` pluggy hookspec. Out-of-tree plugins
   (closed-source, enterprise-only) register here.
3. **Programmatic registrations** through :func:`register_tracker`, used
   by tests, the in-memory reference fake, and third parties that load
   their adapter without pluggy.

The registry is intentionally small: it only stores construction
factories. Adapter instantiation (with credentials, base URLs, etc.) is
the caller's job. The registry is used by:

* ``bernstein trackers list`` to enumerate names + summaries.
* ``bernstein trackers test <name>`` to construct an adapter and run a
  smoke check.
* The orchestrator's federation layer to resolve an adapter by name when
  ``bernstein.yaml: trackers.enabled = [...]`` lists it.

Ordering rule: built-ins ship first, plugin adapters next, programmatic
registrations last. Two adapters with the same ``name`` raise
``DuplicateTrackerError`` unless ``overwrite=True`` is passed.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from bernstein.core.trackers.contract import AbstractTrackerAdapter

log = logging.getLogger(__name__)


__all__ = [
    "DuplicateTrackerError",
    "TrackerFactory",
    "TrackerRegistration",
    "TrackerRegistry",
    "discover_plugin_ingest_adapters",
    "discover_plugin_trackers",
    "get_registry",
    "register_tracker",
    "reset_registry_for_tests",
]


class TrackerFactory(Protocol):
    """Callable that constructs a tracker adapter.

    A factory must accept arbitrary keyword arguments and return an
    instance of :class:`AbstractTrackerAdapter`. Implementations
    typically accept a config dataclass or individual credential
    parameters; the registry treats the factory as opaque.
    """

    def __call__(self, **kwargs: Any) -> AbstractTrackerAdapter:
        """Construct a tracker adapter with the supplied configuration."""


class DuplicateTrackerError(ValueError):
    """Raised when two adapters register under the same name."""


@dataclass(frozen=True)
class TrackerRegistration:
    """A single registered tracker adapter.

    Attributes:
        name: Short stable identifier (``"linear"``, ``"jira_cloud"``).
        factory: Callable that constructs the adapter.
        summary: One-line human-readable description for ``trackers list``.
        source: One of ``"builtin"``, ``"plugin"``, ``"programmatic"``;
            used by the CLI to label each row.
        provenance: Optional free-form provenance string (plugin name,
            module path, etc.).
        capabilities: Optional set of capability tags (``"claim"``,
            ``"attach"``, ``"webhook"``); informational only.
    """

    name: str
    factory: TrackerFactory
    summary: str = ""
    source: str = "programmatic"
    provenance: str | None = None
    capabilities: tuple[str, ...] = field(default_factory=tuple)


class TrackerRegistry:
    """In-process registry of tracker adapter factories.

    The registry is not a singleton by design (tests construct their own
    isolated instances). Module-level helpers wrap a process-wide default
    instance accessed via :func:`get_registry`.
    """

    def __init__(self) -> None:
        self._entries: dict[str, TrackerRegistration] = {}

    def register(
        self,
        name: str,
        factory: TrackerFactory,
        *,
        summary: str = "",
        source: str = "programmatic",
        provenance: str | None = None,
        capabilities: tuple[str, ...] = (),
        overwrite: bool = False,
    ) -> TrackerRegistration:
        """Register ``factory`` under ``name``.

        Args:
            name: Stable identifier (case-sensitive).
            factory: Callable that constructs the adapter.
            summary: One-line human-readable summary.
            source: Origin label (``"builtin"``, ``"plugin"``,
                ``"programmatic"``).
            provenance: Optional module path or plugin name.
            capabilities: Tags describing what the adapter supports.
            overwrite: If ``True``, replace an existing registration
                instead of raising.

        Returns:
            The stored :class:`TrackerRegistration`.

        Raises:
            DuplicateTrackerError: When ``name`` is already registered
                and ``overwrite`` is ``False``.
        """
        if name in self._entries and not overwrite:
            existing = self._entries[name]
            msg = (
                f"Tracker {name!r} is already registered "
                f"(source={existing.source}, provenance={existing.provenance!r}). "
                "Pass overwrite=True to replace it."
            )
            raise DuplicateTrackerError(msg)
        entry = TrackerRegistration(
            name=name,
            factory=factory,
            summary=summary,
            source=source,
            provenance=provenance,
            # Normalize caller-supplied iterables to an immutable tuple.
            capabilities=tuple(capabilities),
        )
        self._entries[name] = entry
        return entry

    def unregister(self, name: str) -> None:
        """Remove ``name`` from the registry (no-op if absent)."""
        self._entries.pop(name, None)

    def get(self, name: str) -> TrackerRegistration:
        """Return the registration for ``name``.

        Raises:
            KeyError: When no adapter is registered under ``name``.
        """
        try:
            return self._entries[name]
        except KeyError as exc:
            known = ", ".join(sorted(self._entries)) or "(none)"
            msg = f"Unknown tracker {name!r}. Registered: {known}."
            raise KeyError(msg) from exc

    def create(self, name: str, **kwargs: Any) -> AbstractTrackerAdapter:
        """Construct an adapter instance.

        Args:
            name: Registered tracker name.
            **kwargs: Forwarded verbatim to the registered factory.

        Returns:
            Newly constructed :class:`AbstractTrackerAdapter`.
        """
        return self.get(name).factory(**kwargs)

    def names(self) -> tuple[str, ...]:
        """Return the registered names in deterministic order.

        Order: built-in entries first (alphabetical), then plugin
        entries (alphabetical), then programmatic entries (alphabetical).
        Tests rely on this ordering to be stable.
        """
        priority = {"builtin": 0, "plugin": 1, "programmatic": 2}

        def sort_key(item: tuple[str, TrackerRegistration]) -> tuple[int, str]:
            return (priority.get(item[1].source, 3), item[0])

        return tuple(name for name, _ in sorted(self._entries.items(), key=sort_key))

    def __iter__(self) -> Iterator[TrackerRegistration]:
        for name in self.names():
            yield self._entries[name]

    def __contains__(self, name: object) -> bool:
        return isinstance(name, str) and name in self._entries

    def __len__(self) -> int:
        return len(self._entries)


# ---------------------------------------------------------------------------
# Process-wide default registry
# ---------------------------------------------------------------------------


_registry: TrackerRegistry | None = None


def _builtin_registrations() -> tuple[tuple[str, TrackerFactory, str, tuple[str, ...]], ...]:
    """Return the built-in adapter registrations.

    Imports are deferred so the registry module stays cheap to import
    even when the consumer only needs the registration protocol.
    """
    # Local imports keep the registry import cheap.
    from bernstein.core.trackers.builtin import (
        AsanaAdapter,
        ClickUpAdapter,
        GitHubProjectsV2Adapter,
        GitLabAdapter,
        JiraCloudTracker,
        JiraDataCenterAdapter,
        PlaneAdapter,
    )
    from bernstein.core.trackers.linear import LinearTracker
    from bernstein.core.trackers.servicenow import ServiceNowTracker

    return (
        (
            "linear",
            LinearTracker,
            "Linear (linear.app) GraphQL adapter.",
            ("claim", "comment", "transition"),
        ),
        (
            "servicenow",
            ServiceNowTracker,
            "ServiceNow Table API adapter.",
            ("comment", "transition", "attach"),
        ),
        (
            "github_projects_v2",
            GitHubProjectsV2Adapter,
            "GitHub Projects v2 GraphQL adapter.",
            ("comment", "transition"),
        ),
        (
            "jira_cloud",
            JiraCloudTracker,
            "Jira Cloud REST adapter.",
            ("comment", "transition", "attach"),
        ),
        (
            "jira_dc",
            JiraDataCenterAdapter,
            "Jira Data Center REST adapter.",
            ("comment", "transition", "attach"),
        ),
        (
            "gitlab",
            GitLabAdapter,
            "GitLab Issues REST adapter.",
            ("comment", "transition"),
        ),
        (
            "clickup",
            ClickUpAdapter,
            "ClickUp REST adapter.",
            ("comment", "transition"),
        ),
        (
            "asana",
            AsanaAdapter,
            "Asana REST adapter.",
            ("comment", "transition"),
        ),
        (
            "plane",
            PlaneAdapter,
            "Plane.so REST adapter.",
            ("comment", "transition"),
        ),
    )


def _populate_builtins(registry: TrackerRegistry) -> None:
    """Populate ``registry`` with the built-in adapter set.

    A missing import is logged and skipped so optional adapters (e.g.
    one whose module fails to import on a stripped-down install) do not
    prevent the rest of the registry from loading.
    """
    try:
        registrations = _builtin_registrations()
    except Exception as exc:  # pragma: no cover - defensive
        log.warning("Failed to import built-in trackers: %s", exc)
        return

    for name, factory, summary, caps in registrations:
        provenance = f"{getattr(factory, '__module__', '?')}.{getattr(factory, '__name__', name)}"
        try:
            registry.register(
                name,
                factory,
                summary=summary,
                source="builtin",
                provenance=provenance,
                capabilities=caps,
                overwrite=False,
            )
        except DuplicateTrackerError:
            # Built-ins are loaded once; this only happens in tests that
            # reuse a registry across populate calls.
            continue


def get_registry() -> TrackerRegistry:
    """Return the process-wide default registry, populating it if empty.

    The default registry is lazily seeded with built-in adapters on
    first access. Plugin discovery is *not* triggered here; callers that
    want plugin adapters should also call :func:`discover_plugin_trackers`.
    """
    global _registry
    if _registry is None:
        _registry = TrackerRegistry()
        _populate_builtins(_registry)
    return _registry


def reset_registry_for_tests() -> None:
    """Clear the process-wide registry (test helper only).

    Tests that exercise registration paths call this in setup/teardown
    to avoid cross-test contamination.
    """
    global _registry
    _registry = None


def register_tracker(
    name: str,
    factory: TrackerFactory,
    *,
    summary: str = "",
    source: str = "programmatic",
    provenance: str | None = None,
    capabilities: tuple[str, ...] = (),
    overwrite: bool = False,
) -> TrackerRegistration:
    """Register ``factory`` on the process-wide default registry."""
    return get_registry().register(
        name,
        factory,
        summary=summary,
        source=source,
        provenance=provenance,
        capabilities=capabilities,
        overwrite=overwrite,
    )


def discover_plugin_trackers(plugin_manager: Any | None = None) -> int:
    """Populate the registry with plugin-contributed tracker adapters.

    Iterates over loaded pluggy plugins, calls
    ``provide_tracker_adapter`` on each that implements it, and registers
    every returned :class:`TrackerRegistration` (or ``(name, factory)``
    tuple for plugins that prefer the tuple shape).

    Args:
        plugin_manager: Optional explicit plugin manager. When ``None``
            the orchestrator's default manager is fetched via
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
        if plugin is None or not hasattr(plugin, "provide_tracker_adapter"):
            continue
        try:
            result = plugin.provide_tracker_adapter()
        except Exception as exc:
            log.warning("Plugin %r provide_tracker_adapter raised: %s", plugin_name, exc)
            continue
        added += _register_plugin_result(registry, plugin_name, result)
    return added


def _register_plugin_result(
    registry: TrackerRegistry,
    plugin_name: str,
    result: Any,
) -> int:
    """Register a single plugin's return value.

    The plugin hook may return:

    * ``None`` (plugin opts out for this call).
    * A single :class:`TrackerRegistration`.
    * A ``(name, factory)`` tuple.
    * A list of either of the above.
    """
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
                capabilities=registration.capabilities,
                overwrite=False,
            )
            count += 1
        except DuplicateTrackerError as exc:
            log.warning(
                "Plugin %r tried to register duplicate tracker %r: %s",
                plugin_name,
                registration.name,
                exc,
            )
    return count


def _coerce_registration(raw: Any, plugin_name: str) -> TrackerRegistration | None:
    """Normalise a plugin-supplied registration into a :class:`TrackerRegistration`."""
    if isinstance(raw, TrackerRegistration):
        return raw
    if isinstance(raw, tuple) and len(raw) == 2:
        name, factory = raw
        if isinstance(name, str) and callable(factory):
            return TrackerRegistration(
                name=name,
                factory=factory,
                source="plugin",
                provenance=plugin_name,
            )
    log.warning(
        "Plugin %r provide_tracker_adapter returned unrecognised value %r; ignoring.",
        plugin_name,
        raw,
    )
    return None


def _wrap_callable(callable_: Callable[..., AbstractTrackerAdapter]) -> TrackerFactory:
    """Adapt a plain callable to the ``TrackerFactory`` protocol.

    Kept as a small utility so tests and out-of-tree code can register
    closures without typing gymnastics.
    """

    def factory(**kwargs: Any) -> AbstractTrackerAdapter:
        return callable_(**kwargs)

    return factory


# ---------------------------------------------------------------------------
# Ingest adapter declarations (plugin-provided)
# ---------------------------------------------------------------------------

# Module-level store of plugin-provided ingest adapter declarations,
# keyed by adapter name. Built-in adapters (when any exist) are not yet
# tracked here -- the contract is purely a plugin extension point.
_ingest_declarations: dict[str, Any] = {}


def _coerce_ingest_declaration(raw: Any) -> Any | None:
    """Normalise a plugin-supplied ingest declaration.

    Accepts:
    * An :class:`IngestAdapterDeclaration` instance.
    * A ``(name, version, declared_event_types)`` or
      ``(name, version, declared_event_types, summary)`` tuple.

    Returns:
        The declaration, or ``None`` if *raw* is unrecognised.
    """
    from bernstein.core.observability.ingest_contract import IngestAdapterDeclaration

    if isinstance(raw, IngestAdapterDeclaration):
        return raw
    if isinstance(raw, tuple) and len(raw) in (3, 4):
        try:
            name, version, event_types = raw[0], raw[1], raw[2]
            summary = raw[3] if len(raw) == 4 else ""
        except (ValueError, TypeError):
            return None
        if not isinstance(name, str) or not isinstance(version, str):
            return None
        normalised = tuple(str(e) for e in event_types)
        return IngestAdapterDeclaration(
            name=name,
            version=version,
            declared_event_types=normalised,
            summary=str(summary),
        )
    return None


def discover_plugin_ingest_adapters(plugin_manager: Any | None = None) -> int:
    """Populate the ingest declaration store with plugin-contributed entries.

    Iterates over loaded pluggy plugins, calls
    ``provide_ingest_adapter`` on each that implements it, and registers
    every returned :class:`IngestAdapterDeclaration` (or
    ``(name, version, declared_event_types[, summary])`` tuple) under
    ``_ingest_declarations``.

    Args:
        plugin_manager: Optional explicit plugin manager. When ``None``
            the orchestrator's default manager is fetched via
            :func:`bernstein.plugins.manager.get_plugin_manager`.

    Returns:
        Number of plugin declarations newly registered.
    """
    pm = plugin_manager
    if pm is None:
        try:
            from bernstein.plugins.manager import get_plugin_manager

            pm = get_plugin_manager()
        except Exception as exc:  # pragma: no cover - defensive
            log.warning("Could not obtain plugin manager: %s", exc)
            return 0

    registered_names = getattr(pm, "_registered_names", [])
    inner_pm = getattr(pm, "_pm", None)
    if inner_pm is None:
        log.debug("Plugin manager has no inner pluggy manager; skipping ingest discovery.")
        return 0

    added = 0
    for plugin_name in registered_names:
        plugin = inner_pm.get_plugin(plugin_name)
        if plugin is None or not hasattr(plugin, "provide_ingest_adapter"):
            continue
        try:
            result = plugin.provide_ingest_adapter()
        except Exception as exc:
            log.warning("Plugin %r provide_ingest_adapter raised: %s", plugin_name, exc)
            continue
        if result is None:
            continue
        items = result if isinstance(result, list) else [result]
        for raw in items:
            declaration = _coerce_ingest_declaration(raw)
            if declaration is None:
                log.warning(
                    "Plugin %r provide_ingest_adapter returned unrecognised value %r; ignoring.",
                    plugin_name,
                    raw,
                )
                continue
            if declaration.name in _ingest_declarations:
                log.warning(
                    "Plugin %r tried to register duplicate ingest adapter %r; skipping.",
                    plugin_name,
                    declaration.name,
                )
                continue
            _ingest_declarations[declaration.name] = declaration
            added += 1
    return added
