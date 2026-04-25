"""Notification sink registry — first-party drivers + pluggy entry points.

The registry is the single lookup surface for notification drivers. It
loads first-party drivers eagerly (``telegram``, ``slack``, ``discord``,
``email_smtp``, ``webhook``, ``shell``) and third-party drivers lazily
via the ``bernstein.notification_sinks`` entry-point group. External
packages register a new driver by declaring an entry point in their
``pyproject.toml``::

    [project.entry-points."bernstein.notification_sinks"]
    pagerduty = "my_package.pagerduty:PagerDutyDriver"

The driver may be either a class — instantiated zero-arg lazily — or an
instance. The registry stores a *driver factory*: the user instantiates
a configured sink with :func:`build_sink` against a config dict, then
registers the live instance under a unique ``sink_id``.

Design mirrors :mod:`bernstein.core.sandbox.registry` so plugin authors
can port between the two surfaces with minimal cognitive load.
"""

from __future__ import annotations

import inspect
import logging
import threading
from importlib.metadata import entry_points
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Iterator

    from bernstein.core.notifications.protocol import NotificationSink

logger = logging.getLogger(__name__)

ENTRY_POINT_GROUP = "bernstein.notification_sinks"

#: First-party driver kinds the registry knows about. Each value points
#: at an importable ``module:Class`` whose constructor accepts the sink
#: configuration dict (see :func:`build_sink`).
_BUILTIN_DRIVERS: dict[str, str] = {
    "telegram": "bernstein.core.notifications.sinks.telegram:TelegramSink",
    "slack": "bernstein.core.notifications.sinks.slack:SlackSink",
    "discord": "bernstein.core.notifications.sinks.discord:DiscordSink",
    "email_smtp": "bernstein.core.notifications.sinks.email_smtp:EmailSmtpSink",
    "webhook": "bernstein.core.notifications.sinks.webhook:WebhookSink",
    "shell": "bernstein.core.notifications.sinks.shell:ShellSink",
}


__all__ = [
    "ENTRY_POINT_GROUP",
    "Registry",
    "_reset_for_tests",
    "build_sink",
    "default_registry",
    "get_sink",
    "iter_sinks",
    "list_driver_kinds",
    "register_driver_factory",
    "register_sink",
]


class Registry:
    """Thread-safe registry of notification sinks.

    Two distinct namespaces live here:

    * **Driver factories** (``kind``) — classes that *build* a sink
      from a configuration dict. The kind is the value the user puts
      under ``bernstein.yaml::notifications.sinks[*].kind``.
    * **Live sinks** (``sink_id``) — already-configured instances
      ready to receive events. The id is the value the user puts
      under ``bernstein.yaml::notifications.sinks[*].id``.
    """

    def __init__(self) -> None:
        self._sinks: dict[str, NotificationSink] = {}
        self._driver_factories: dict[str, Any] = {}
        self._lock = threading.RLock()
        self._builtins_loaded = False
        self._entrypoints_loaded = False

    # ------------------------------------------------------------------
    # Driver factories
    # ------------------------------------------------------------------

    def register_driver_factory(self, kind: str, factory: Any) -> None:
        """Register a driver factory under ``kind``.

        Args:
            kind: Driver kind (e.g. ``"slack"``). Must be unique.
            factory: A callable (typically a class) whose signature is
                ``factory(config: dict[str, Any]) -> NotificationSink``.

        Raises:
            ValueError: If *kind* is empty or already registered.
        """
        normalised = kind.strip()
        if not normalised:
            raise ValueError("driver kind must be non-empty")
        with self._lock:
            if normalised in self._driver_factories:
                raise ValueError(f"Duplicate driver kind: {normalised!r}")
            self._driver_factories[normalised] = factory

    def get_driver_factory(self, kind: str) -> Any:
        """Return the factory for ``kind``.

        Raises:
            KeyError: If the kind is unknown.
        """
        self._ensure_loaded()
        with self._lock:
            try:
                return self._driver_factories[kind]
            except KeyError:
                available = ", ".join(sorted(self._driver_factories)) or "(none)"
                raise KeyError(f"Unknown notification driver {kind!r}. Available: {available}") from None

    def list_driver_kinds(self) -> list[str]:
        """Return the names of all known driver kinds, sorted."""
        self._ensure_loaded()
        with self._lock:
            return sorted(self._driver_factories.keys())

    # ------------------------------------------------------------------
    # Live sinks
    # ------------------------------------------------------------------

    def register_sink(self, sink: NotificationSink) -> None:
        """Register a configured sink instance.

        Raises:
            ValueError: If ``sink.sink_id`` is empty or duplicates an
                existing entry.
        """
        sid = sink.sink_id.strip() if isinstance(sink.sink_id, str) else ""
        if not sid:
            raise ValueError("sink.sink_id must be non-empty")
        with self._lock:
            if sid in self._sinks:
                raise ValueError(f"Duplicate sink id: {sid!r}")
            self._sinks[sid] = sink

    def unregister_sink(self, sink_id: str) -> None:
        """Remove ``sink_id`` from the registry if present."""
        with self._lock:
            self._sinks.pop(sink_id, None)

    def get_sink(self, sink_id: str) -> NotificationSink:
        """Return the live sink registered under ``sink_id``.

        Raises:
            KeyError: If no sink with that id is configured.
        """
        with self._lock:
            try:
                return self._sinks[sink_id]
            except KeyError:
                available = ", ".join(sorted(self._sinks)) or "(none)"
                raise KeyError(f"Unknown notification sink {sink_id!r}. Available: {available}") from None

    def iter_sinks(self) -> Iterator[NotificationSink]:
        """Iterate over all registered live sinks (snapshot)."""
        with self._lock:
            return iter(list(self._sinks.values()))

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _ensure_loaded(self) -> None:
        with self._lock:
            if not self._builtins_loaded:
                self._load_builtins()
                self._builtins_loaded = True
            if not self._entrypoints_loaded:
                self._load_entrypoints()
                self._entrypoints_loaded = True

    def _load_builtins(self) -> None:
        for kind, target in _BUILTIN_DRIVERS.items():
            if kind in self._driver_factories:
                continue
            try:
                module_name, attr_name = target.split(":", 1)
                module = __import__(module_name, fromlist=[attr_name])
                self._driver_factories[kind] = getattr(module, attr_name)
            except Exception as exc:
                logger.warning("Failed to load builtin notification driver %r: %s", kind, exc)

    def _load_entrypoints(self) -> None:
        try:
            eps = entry_points(group=ENTRY_POINT_GROUP)
        except Exception as exc:
            logger.warning("Failed to enumerate notification entry points: %s", exc)
            return
        for ep in eps:
            kind = ep.name
            if kind in self._driver_factories:
                logger.debug("notification entry-point %r shadows an earlier registration; skipping", kind)
                continue
            try:
                loaded = ep.load()
            except Exception as exc:
                logger.warning("Failed to load notification entry-point %r: %s", kind, exc)
                continue
            self._driver_factories[kind] = loaded


# ---------------------------------------------------------------------------
# Module-level facade
# ---------------------------------------------------------------------------


_default_registry_instance = Registry()


def default_registry() -> Registry:
    """Return the process-wide default notification registry."""
    return _default_registry_instance


def register_driver_factory(kind: str, factory: Any) -> None:
    """Register a driver factory in the default registry."""
    _default_registry_instance.register_driver_factory(kind, factory)


def register_sink(sink: NotificationSink) -> None:
    """Register a live sink in the default registry."""
    _default_registry_instance.register_sink(sink)


def get_sink(sink_id: str) -> NotificationSink:
    """Look up a live sink in the default registry."""
    return _default_registry_instance.get_sink(sink_id)


def iter_sinks() -> Iterator[NotificationSink]:
    """Iterate over live sinks in the default registry."""
    return _default_registry_instance.iter_sinks()


def list_driver_kinds() -> list[str]:
    """Return the names of all known driver kinds."""
    return _default_registry_instance.list_driver_kinds()


def build_sink(config: dict[str, Any]) -> NotificationSink:
    """Construct a live sink from a configuration dict.

    The config dict mirrors the YAML shape::

        {"id": "slack-ops", "kind": "slack", "enabled": true, ...}

    The ``kind`` is looked up in the driver registry; the rest of the
    dict is passed verbatim to the driver factory.

    Raises:
        ValueError: If ``id`` or ``kind`` is missing.
        KeyError: If the kind is not registered.
    """
    sink_id = config.get("id")
    kind = config.get("kind")
    if not isinstance(sink_id, str) or not sink_id.strip():
        raise ValueError("notification sink config requires a non-empty 'id'")
    if not isinstance(kind, str) or not kind.strip():
        raise ValueError(f"notification sink {sink_id!r} requires a non-empty 'kind'")
    factory = _default_registry_instance.get_driver_factory(kind)
    if inspect.isclass(factory):
        return factory(config)  # type: ignore[no-any-return]
    return factory(config)  # type: ignore[no-any-return]


def _reset_for_tests() -> None:
    """Drop cached state. Tests only — not part of the public API."""
    global _default_registry_instance
    _default_registry_instance = Registry()
