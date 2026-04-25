"""Tests for the bernstein.yaml ``notifications`` Pydantic schema."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from bernstein.core.notifications.config import (
    NotificationsConfig,
    RetrySchema,
    SinkSchema,
)


def test_empty_config_is_valid() -> None:
    cfg = NotificationsConfig.from_raw(None)
    assert cfg.enabled is False
    assert cfg.sinks == []


def test_typical_config_round_trip() -> None:
    raw = {
        "enabled": True,
        "retry": {"max_attempts": 5, "initial_delay_ms": 100},
        "sinks": [
            {
                "id": "slack-ops",
                "kind": "slack",
                "webhook_url": "https://hooks.slack.com/x",
                "events": ["post_task", "post_merge"],
                "severities": ["warning", "error"],
            },
            {
                "id": "email-arch",
                "kind": "email_smtp",
                "host": "smtp.example.com",
                "from_addr": "bernstein@example.com",
                "to_addrs": ["arch@example.com"],
            },
        ],
    }
    cfg = NotificationsConfig.from_raw(raw)
    assert cfg.enabled is True
    assert isinstance(cfg.retry, RetrySchema)
    assert cfg.retry.max_attempts == 5
    assert [s.id for s in cfg.sinks] == ["slack-ops", "email-arch"]


def test_unknown_event_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown event"):
        SinkSchema(id="x", kind="slack", events=["not_a_real_event"])


def test_unknown_severity_rejected() -> None:
    with pytest.raises(ValidationError, match="unknown severity"):
        SinkSchema(id="x", kind="slack", severities=["whatever"])


def test_duplicate_sink_id_rejected() -> None:
    with pytest.raises(ValidationError, match="duplicate sink id"):
        NotificationsConfig.from_raw(
            {
                "sinks": [
                    {"id": "dup", "kind": "slack", "webhook_url": "x"},
                    {"id": "dup", "kind": "discord", "webhook_url": "y"},
                ],
            },
        )


def test_non_mapping_root_raises_typeerror() -> None:
    with pytest.raises(TypeError):
        NotificationsConfig.from_raw([1, 2, 3])
