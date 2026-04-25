"""Pydantic schema for the ``notifications`` block in ``bernstein.yaml``.

Loaded eagerly at startup so a misconfigured sink fails fast before
any agent is spawned. The schema is designed so a brand-new install
that doesn't set ``notifications`` at all is valid — every field is
optional.

Example::

    notifications:
      enabled: true
      retry:
        max_attempts: 4
        initial_delay_ms: 250
      sinks:
        - id: slack-ops
          kind: slack
          enabled: true
          webhook_url: ${SLACK_WEBHOOK_URL}
          events: [post_task, post_merge]
          severities: [warning, error]
        - id: ops-pager
          kind: webhook
          url: https://hooks.example.com/bernstein
          headers:
            X-Token: ${OPS_PAGER_TOKEN}
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from bernstein.core.notifications.bridge import (
    DEFAULT_INITIAL_DELAY_MS,
    DEFAULT_MAX_ATTEMPTS,
)

__all__ = [
    "NotificationsConfig",
    "RetrySchema",
    "SinkSchema",
]


_VALID_SEVERITIES = {"info", "warning", "error"}
_VALID_EVENTS = {
    "pre_task",
    "post_task",
    "pre_merge",
    "post_merge",
    "pre_spawn",
    "post_spawn",
    "synthetic",
}


class RetrySchema(BaseModel):
    """Retry tuning for the dispatcher."""

    model_config = ConfigDict(extra="forbid")

    max_attempts: int = Field(default=DEFAULT_MAX_ATTEMPTS, ge=1, le=20)
    initial_delay_ms: int = Field(default=DEFAULT_INITIAL_DELAY_MS, ge=1, le=60_000)
    backoff_factor: float = Field(default=2.0, ge=1.0, le=10.0)
    max_delay_ms: int = Field(default=30_000, ge=1, le=600_000)


class SinkSchema(BaseModel):
    """One configured notification sink.

    The schema is permissive (``extra='allow'``) because driver-specific
    keys (e.g. ``webhook_url`` for Slack, ``smtp_host`` for email) live
    on the same object. Drivers do their own validation in their
    constructor; this schema only enforces the shape required by the
    dispatcher itself.
    """

    model_config = ConfigDict(extra="allow")

    id: str = Field(..., min_length=1)
    kind: str = Field(..., min_length=1)
    enabled: bool = True
    events: list[str] | None = Field(
        default=None,
        description="Lifecycle events this sink subscribes to. None = all.",
    )
    severities: list[str] | None = Field(
        default=None,
        description="Severity allow-list. None = all.",
    )
    labels: dict[str, str] = Field(default_factory=dict)

    @field_validator("events")
    @classmethod
    def _check_events(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        bad = [v for v in value if v not in _VALID_EVENTS]
        if bad:
            allowed = ", ".join(sorted(_VALID_EVENTS))
            raise ValueError(f"unknown event(s) {bad!r}; allowed: {allowed}")
        return value

    @field_validator("severities")
    @classmethod
    def _check_severities(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        bad = [v for v in value if v not in _VALID_SEVERITIES]
        if bad:
            allowed = ", ".join(sorted(_VALID_SEVERITIES))
            raise ValueError(f"unknown severity {bad!r}; allowed: {allowed}")
        return value


class NotificationsConfig(BaseModel):
    """Top-level ``notifications`` schema.

    Example::

        notifications = NotificationsConfig.model_validate(raw_yaml.get("notifications", {}))
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    retry: RetrySchema = Field(default_factory=RetrySchema)
    sinks: list[SinkSchema] = Field(default_factory=list)
    dedup_lru_size: int = Field(default=2048, ge=16, le=1_000_000)
    dedup_window_seconds: int = Field(default=6 * 3600, ge=1, le=30 * 24 * 3600)

    @field_validator("sinks")
    @classmethod
    def _check_unique_ids(cls, value: list[SinkSchema]) -> list[SinkSchema]:
        seen: set[str] = set()
        for sink in value:
            if sink.id in seen:
                raise ValueError(f"duplicate sink id: {sink.id!r}")
            seen.add(sink.id)
        return value

    @classmethod
    def from_raw(cls, raw: Any) -> NotificationsConfig:
        """Build from an arbitrary ``Mapping`` value or ``None``.

        ``None`` and missing keys yield a default-disabled config so a
        bare ``bernstein.yaml`` without a ``notifications`` block stays
        valid.
        """
        if raw is None:
            return cls(enabled=False)
        if not isinstance(raw, dict):
            raise TypeError(f"notifications block must be a mapping, got {type(raw).__name__}")
        return cls.model_validate(raw)
