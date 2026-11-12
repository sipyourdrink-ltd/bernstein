"""MQTT audit-event sink (worked example).

The sink subscribes to bernstein's ``on_audit_event`` hookspec and
forwards each event as a single-line JSON blob to a configurable MQTT
topic. The broker connection itself is stubbed in this example -
replace :meth:`_publish` with a real client (e.g. ``paho.mqtt``) when
wiring this into your environment.

The plugin reads its configuration from environment variables so an
operator can drop the package into a CI image and toggle the sink
on/off without touching code:

* ``BERNSTEIN_AUDIT_MQTT_URL`` - broker URL (``mqtt://host:1883``).
* ``BERNSTEIN_AUDIT_MQTT_TOPIC`` - topic name (default ``bernstein/audit``).
* ``BERNSTEIN_AUDIT_MQTT_ENABLED`` - gate flag; the sink no-ops when unset.

The hook is registered with ``@hookspec(background=True)`` upstream so
the publish path runs off the main orchestrator tick - a slow broker
cannot block the orchestrator's main loop.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from typing import Any, ClassVar

from bernstein.plugins import hookimpl

logger = logging.getLogger(__name__)

DEFAULT_TOPIC = "bernstein/audit"


@dataclass
class SinkConfig:
    """Configuration knobs read from the operator's environment.

    Attributes:
        url: Broker URL. Empty string disables the sink.
        topic: Topic name. Defaults to :data:`DEFAULT_TOPIC`.
        enabled: When False, the sink is loaded but does nothing.
    """

    url: str = ""
    topic: str = DEFAULT_TOPIC
    enabled: bool = False
    extra_labels: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> SinkConfig:
        """Build a config from ``BERNSTEIN_AUDIT_MQTT_*`` env vars."""
        return cls(
            url=os.environ.get("BERNSTEIN_AUDIT_MQTT_URL", ""),
            topic=os.environ.get("BERNSTEIN_AUDIT_MQTT_TOPIC", DEFAULT_TOPIC),
            enabled=os.environ.get("BERNSTEIN_AUDIT_MQTT_ENABLED", "").strip() == "1",
        )


class MqttAuditSink:
    """Mirror :func:`on_audit_event` payloads to an MQTT broker.

    The sink is fail-soft: a broker outage logs at WARNING but never
    raises into the orchestrator's main loop. Tests inject a fake
    publish callable to verify the hook is wired correctly without
    needing a live broker.
    """

    # Stable metadata for tests that verify the plugin and hook names.
    plugin_name: ClassVar[str] = "custom-audit-sink"
    hook_target: ClassVar[str] = "on_audit_event"

    def __init__(
        self,
        *,
        config: SinkConfig | None = None,
        publish_fn: object | None = None,
    ) -> None:
        self._config = config or SinkConfig.from_env()
        # Tests inject a callable here so they can assert on the
        # serialised payload without needing an MQTT broker.
        self._publish_fn = publish_fn

    @hookimpl
    def on_audit_event(
        self,
        event_type: str,
        actor: str,
        payload: dict[str, Any],
    ) -> None:
        """Mirror a single audit event to the configured MQTT topic."""
        if not self._config.enabled or not self._config.url:
            # Sink is dormant - operator hasn't enabled it yet. Hooks
            # still fire but the publish path is a no-op so a partial
            # configuration cannot crash the orchestrator.
            return
        envelope = {
            "event_type": event_type,
            "actor": actor,
            "payload": payload,
        }
        if self._config.extra_labels:
            envelope["labels"] = dict(self._config.extra_labels)
        try:
            self._publish(self._config.topic, json.dumps(envelope, sort_keys=True))
        except Exception as exc:
            # NEVER let a broker failure escape - audit sinks are
            # downstream of orchestrator decisions and should not block
            # them. Operators rely on the bernstein audit log itself
            # for correctness; this sink is best-effort mirroring.
            logger.warning(
                "MqttAuditSink: publish to %s failed: %s",
                self._config.topic,
                exc,
            )

    def _publish(self, topic: str, body: str) -> None:
        """Hand off the serialised payload to the publish backend.

        Replace this method with a real MQTT client call when wiring
        the plugin into your environment. The method is split out so
        tests can substitute :attr:`_publish_fn` without touching
        the public hook surface.
        """
        if self._publish_fn is not None:
            self._publish_fn(topic, body)  # type: ignore[operator]
            return
        # Default behaviour: log at DEBUG so downstream consumers can
        # see what would have been published in dry-run mode.
        logger.debug("MqttAuditSink (stub): topic=%s body=%s", topic, body)
