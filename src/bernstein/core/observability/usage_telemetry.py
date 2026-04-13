"""Opt-in usage telemetry with local consent management.

Provides a consent layer on top of the existing OpenTelemetry integration
in :mod:`bernstein.core.telemetry`.  Events are only recorded when the user
has explicitly opted in; otherwise all recording functions are no-ops.

Consent state is persisted in ``<config_dir>/telemetry.json`` (typically
``~/.bernstein/telemetry.json``).
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_CONSENT_FILENAME = "telemetry.json"


class TelemetryConsent(Enum):
    """User consent state for usage telemetry.

    Attributes:
        OPT_IN: User has explicitly opted in to telemetry.
        OPT_OUT: User has explicitly opted out.
        UNDECIDED: User has not yet made a choice (treated as opt-out).
    """

    OPT_IN = "opt_in"
    OPT_OUT = "opt_out"
    UNDECIDED = "undecided"


@dataclass
class TelemetryConfig:
    """Runtime telemetry configuration.

    Attributes:
        consent: Current consent state.
        anonymous_id: Stable anonymous identifier for this installation.
        events_endpoint: Optional remote endpoint URL (unused when logging
            locally; reserved for future use).
    """

    consent: TelemetryConsent
    anonymous_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    events_endpoint: str = ""


def load_consent(config_dir: Path) -> TelemetryConsent:
    """Load the telemetry consent state from disk.

    Args:
        config_dir: Directory containing ``telemetry.json``
            (e.g. ``~/.bernstein``).

    Returns:
        The persisted consent value, or :attr:`TelemetryConsent.UNDECIDED`
        if the file is missing or unreadable.
    """
    path = config_dir / _CONSENT_FILENAME
    if not path.exists():
        return TelemetryConsent.UNDECIDED

    try:
        data: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
        raw = data.get("consent", "undecided")
        return TelemetryConsent(raw)
    except (OSError, ValueError):
        logger.debug("Could not read telemetry consent from %s", path)
        return TelemetryConsent.UNDECIDED


def save_consent(config_dir: Path, consent: TelemetryConsent) -> None:
    """Persist the telemetry consent state to disk.

    Creates the config directory if it does not exist.

    Args:
        config_dir: Directory to write ``telemetry.json`` into.
        consent: Consent state to persist.
    """
    config_dir.mkdir(parents=True, exist_ok=True)
    path = config_dir / _CONSENT_FILENAME
    data = {"consent": consent.value}
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    logger.info("Telemetry consent saved: %s", consent.value)


def record_usage_event(
    config: TelemetryConfig,
    event: str,
    properties: dict[str, Any],
    *,
    log_dir: Path | None = None,
) -> None:
    """Record a usage event if the user has opted in.

    When consent is not :attr:`TelemetryConsent.OPT_IN`, this function is a
    complete no-op and returns immediately.  Otherwise the event is appended
    as a JSON line to ``<log_dir>/usage_events.jsonl``.

    Args:
        config: Telemetry configuration (checked for consent).
        event: Event name (e.g. ``"run.start"``, ``"task.complete"``).
        properties: Arbitrary key-value properties attached to the event.
        log_dir: Directory for the event log file.  Defaults to the current
            working directory if not specified.
    """
    if config.consent is not TelemetryConsent.OPT_IN:
        return

    record: dict[str, Any] = {
        "event": event,
        "anonymous_id": config.anonymous_id,
        "timestamp": time.time(),
        "properties": properties,
    }

    target_dir = log_dir or Path.cwd()
    target_dir.mkdir(parents=True, exist_ok=True)
    log_path = target_dir / "usage_events.jsonl"

    try:
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")
    except OSError as exc:
        logger.warning("Failed to write usage event: %s", exc)
