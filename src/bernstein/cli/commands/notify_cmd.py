"""``bernstein notify ...`` CLI commands (release 1.9).

The user-facing surface is intentionally tiny: ``notify test --sink``
fires a synthetic event end-to-end through the configured driver so an
operator can verify their YAML before going live. Listing and
inspection live alongside.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import click
import yaml

from bernstein.core.lifecycle.notify_bridge import (
    NotifyLifecycleBridge,
    build_bridge_from_config,
)
from bernstein.core.notifications.config import NotificationsConfig
from bernstein.core.notifications.protocol import (
    NotificationEvent,
    NotificationEventKind,
)

__all__ = ["notify_group"]


def _load_notifications_config(config_path: Path) -> NotificationsConfig:
    """Load and validate the ``notifications`` block from ``bernstein.yaml``."""
    if not config_path.exists():
        raise click.ClickException(f"config not found: {config_path}")
    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise click.ClickException(f"invalid YAML in {config_path}: {exc}") from exc
    if not isinstance(raw, dict):
        raise click.ClickException(f"top-level YAML in {config_path} must be a mapping")
    return NotificationsConfig.from_raw(raw.get("notifications"))


def _resolve_runtime_dir(config_path: Path) -> Path:
    """Return the ``.sdd/runtime`` directory next to the config file."""
    return config_path.parent / ".sdd" / "runtime"


def _build_synthetic_event(kind: NotificationEventKind, sink_id: str) -> NotificationEvent:
    return NotificationEvent(
        event_id=f"synthetic-{sink_id}-{int(time.time() * 1000)}",
        kind=kind,
        title=f"bernstein notify test ({sink_id})",
        body="This is a synthetic event sent by `bernstein notify test`.",
        severity="info",
        timestamp=time.time(),
        labels={"source": "cli"},
        details={"command": "bernstein notify test"},
    )


@click.group(name="notify")
def notify_group() -> None:
    """Outbound notification commands (release 1.9)."""


@notify_group.command(name="test")
@click.option(
    "--sink",
    "sink_id",
    required=True,
    help="Sink id from bernstein.yaml::notifications.sinks[*].id",
)
@click.option(
    "--event",
    "event_kind",
    default="synthetic",
    show_default=True,
    type=click.Choice([k.value for k in NotificationEventKind]),
    help="Event kind to emit.",
)
@click.option(
    "--config",
    "config_path",
    default="bernstein.yaml",
    show_default=True,
    type=click.Path(dir_okay=False, path_type=Path),
)
def notify_test(sink_id: str, event_kind: str, config_path: Path) -> None:
    """Fire a synthetic event end-to-end through ``--sink``."""
    config = _load_notifications_config(config_path)
    sink_cfg = next((s for s in config.sinks if s.id == sink_id), None)
    if sink_cfg is None:
        raise click.ClickException(
            f"sink {sink_id!r} not found in {config_path}. "
            f"Available: {', '.join(s.id for s in config.sinks) or '(none)'}",
        )
    if not sink_cfg.enabled:
        raise click.ClickException(f"sink {sink_id!r} is disabled in config")

    runtime_dir = _resolve_runtime_dir(config_path)
    runtime_dir.mkdir(parents=True, exist_ok=True)

    # Build a single-sink config so we don't accidentally fan out to
    # other configured sinks during a CLI smoke test.
    isolated = NotificationsConfig(
        enabled=True,
        retry=config.retry,
        sinks=[sink_cfg],
        dedup_lru_size=config.dedup_lru_size,
        dedup_window_seconds=config.dedup_window_seconds,
    )
    bridge = build_bridge_from_config(
        isolated,
        runtime_dir=runtime_dir,
        register_in_registry=False,
    )
    if not bridge.sinks:
        raise click.ClickException(f"failed to build sink {sink_id!r}; check logs")

    event = _build_synthetic_event(NotificationEventKind(event_kind), sink_id)
    asyncio.run(_run_test(bridge, event))
    click.echo(json.dumps({"sink_id": sink_id, "event_id": event.event_id, "outcome": "dispatched"}))


async def _run_test(bridge: NotifyLifecycleBridge, event: NotificationEvent) -> None:
    try:
        await bridge.dispatch_event(event)
    finally:
        await bridge.aclose()


@notify_group.command(name="list")
@click.option(
    "--config",
    "config_path",
    default="bernstein.yaml",
    show_default=True,
    type=click.Path(dir_okay=False, path_type=Path),
)
def notify_list(config_path: Path) -> None:
    """List configured sinks from ``bernstein.yaml``."""
    config = _load_notifications_config(config_path)
    if not config.sinks:
        click.echo("(no sinks configured)")
        return
    for sink in config.sinks:
        click.echo(
            json.dumps(
                {
                    "id": sink.id,
                    "kind": sink.kind,
                    "enabled": sink.enabled,
                    "events": sink.events,
                    "severities": sink.severities,
                },
            ),
        )
