"""Configurable alert rules for Bernstein."""

from __future__ import annotations

import logging
import operator as _op
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


AlertMetric = Literal["error_rate", "cost_usd", "task_failure_rate", "agent_stalled", "queue_depth"]
AlertChannel = Literal["slack", "email", "webhook"]


@dataclass
class AlertRule:
    """Single alert rule configuration."""

    name: str
    metric: AlertMetric
    threshold: float
    operator: Literal["gt", "lt", "eq", "gte", "lte"] = "gt"
    channel: AlertChannel = "slack"
    channel_config: dict[str, Any] = field(default_factory=dict)
    cooldown_seconds: int = 300
    enabled: bool = True


@dataclass
class AlertConfig:
    """Alert configuration from bernstein.yaml."""

    rules: list[AlertRule] = field(default_factory=list)
    default_cooldown: int = 300
    enabled: bool = True


def load_alert_config(config_path: Path) -> AlertConfig:
    """Load alert configuration from YAML file.

    Args:
        config_path: Path to bernstein.yaml or alert config file.

    Returns:
        AlertConfig instance.
    """
    import yaml

    if not config_path.exists():
        return AlertConfig()

    try:
        data = yaml.safe_load(config_path.read_text())
        alerts_data = data.get("alerts", {})

        if not alerts_data:
            return AlertConfig()

        rules = []
        for rule_data in alerts_data.get("rules", []):
            rules.append(
                AlertRule(
                    name=rule_data.get("name", "unnamed"),
                    metric=rule_data.get("metric", "error_rate"),
                    threshold=float(rule_data.get("threshold", 0.1)),
                    operator=rule_data.get("operator", "gt"),
                    channel=rule_data.get("channel", "slack"),
                    channel_config=rule_data.get("channel_config", {}),
                    cooldown_seconds=rule_data.get("cooldown_seconds", 300),
                    enabled=rule_data.get("enabled", True),
                )
            )

        return AlertConfig(
            rules=rules,
            default_cooldown=alerts_data.get("default_cooldown", 300),
            enabled=alerts_data.get("enabled", True),
        )

    except Exception as exc:
        logger.warning("Failed to load alert config: %s", exc)
        return AlertConfig()


class AlertManager:
    """Manage and evaluate alert rules."""

    def __init__(self, config: AlertConfig) -> None:
        self._config = config
        self._last_triggered: dict[str, float] = {}

    def check_alerts(self, metrics: dict[str, float]) -> list[AlertRule]:
        """Check all alert rules against current metrics.

        Args:
            metrics: Current metric values.

        Returns:
            List of triggered alert rules.
        """
        if not self._config.enabled:
            return []

        import time

        triggered = []
        now = time.time()

        for rule in self._config.rules:
            if not rule.enabled:
                continue

            # Check cooldown
            last_trigger = self._last_triggered.get(rule.name, 0.0)
            if now - last_trigger < rule.cooldown_seconds:
                continue

            # Get metric value
            metric_value = metrics.get(rule.metric)
            if metric_value is None:
                continue

            # Evaluate condition
            if self._evaluate_condition(metric_value, rule.threshold, rule.operator):
                triggered.append(rule)
                self._last_triggered[rule.name] = now

        return triggered

    def _evaluate_condition(
        self,
        value: float,
        threshold: float,
        operator: Literal["gt", "lt", "eq", "gte", "lte"],
    ) -> bool:
        """Evaluate alert condition."""
        ops = {"gt": _op.gt, "lt": _op.lt, "eq": _op.eq, "gte": _op.ge, "lte": _op.le}
        fn = ops.get(operator)
        return fn(value, threshold) if fn else False

    def get_alert_message(self, rule: AlertRule, value: float) -> str:
        """Generate alert message for triggered rule.

        Args:
            rule: Triggered alert rule.
            value: Current metric value.

        Returns:
            Alert message string.
        """
        return f"Alert: {rule.name} - {rule.metric} is {value:.3f} (threshold: {rule.operator} {rule.threshold})"
