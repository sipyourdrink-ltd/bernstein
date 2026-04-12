"""Backward-compat shim — re-exports from bernstein.core.observability.alert_rules."""

from bernstein.core.observability.alert_rules import (
    AlertChannel,
    AlertConfig,
    AlertManager,
    AlertMetric,
    AlertRule,
    load_alert_config,
    logger,
)

__all__ = [
    "AlertChannel",
    "AlertConfig",
    "AlertManager",
    "AlertMetric",
    "AlertRule",
    "load_alert_config",
    "logger",
]
