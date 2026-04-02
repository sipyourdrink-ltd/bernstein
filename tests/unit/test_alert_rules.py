"""Tests for alert rules configuration."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from bernstein.core.alert_rules import (
    AlertConfig,
    AlertManager,
    AlertRule,
    load_alert_config,
)


class TestAlertRule:
    """Test AlertRule dataclass."""

    def test_default_rule(self) -> None:
        """Test default rule configuration."""
        rule = AlertRule(name="test", metric="error_rate", threshold=0.1)

        assert rule.operator == "gt"
        assert rule.channel == "slack"
        assert rule.cooldown_seconds == 300
        assert rule.enabled is True

    def test_custom_rule(self) -> None:
        """Test custom rule configuration."""
        rule = AlertRule(
            name="high_cost",
            metric="cost_usd",
            threshold=100.0,
            operator="gte",
            channel="email",
            cooldown_seconds=600,
        )

        assert rule.operator == "gte"
        assert rule.channel == "email"
        assert rule.cooldown_seconds == 600


class TestAlertConfig:
    """Test AlertConfig dataclass."""

    def test_default_config(self) -> None:
        """Test default configuration."""
        config = AlertConfig()

        assert config.rules == []
        assert config.default_cooldown == 300
        assert config.enabled is True

    def test_config_with_rules(self) -> None:
        """Test configuration with rules."""
        rules = [
            AlertRule(name="rule1", metric="error_rate", threshold=0.1),
            AlertRule(name="rule2", metric="cost_usd", threshold=50.0),
        ]
        config = AlertConfig(rules=rules)

        assert len(config.rules) == 2


class TestLoadAlertConfig:
    """Test loading alert configuration from YAML."""

    def test_load_nonexistent_file(self, tmp_path: Path) -> None:
        """Test loading from non-existent file."""
        config_path = tmp_path / "nonexistent.yaml"
        config = load_alert_config(config_path)

        assert config.enabled is True
        assert config.rules == []

    def test_load_empty_config(self, tmp_path: Path) -> None:
        """Test loading empty configuration."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text("{}")

        config = load_alert_config(config_path)

        assert config.rules == []

    def test_load_with_rules(self, tmp_path: Path) -> None:
        """Test loading configuration with rules."""
        config_path = tmp_path / "config.yaml"
        config_path.write_text("""
alerts:
  enabled: true
  default_cooldown: 600
  rules:
    - name: high_error_rate
      metric: error_rate
      threshold: 0.1
      operator: gt
      channel: slack
    - name: high_cost
      metric: cost_usd
      threshold: 100.0
      operator: gte
      channel: email
""")

        config = load_alert_config(config_path)

        assert config.enabled is True
        assert config.default_cooldown == 600
        assert len(config.rules) == 2
        assert config.rules[0].name == "high_error_rate"
        assert config.rules[1].metric == "cost_usd"


class TestAlertManager:
    """Test AlertManager class."""

    def test_manager_creation(self) -> None:
        """Test manager initialization."""
        config = AlertConfig()
        manager = AlertManager(config)

        assert manager._config == config

    def test_check_alerts_disabled(self) -> None:
        """Test alert checking when disabled."""
        config = AlertConfig(enabled=False)
        manager = AlertManager(config)

        triggered = manager.check_alerts({"error_rate": 0.5})

        assert triggered == []

    def test_evaluate_condition_gt(self) -> None:
        """Test greater than condition."""
        config = AlertConfig()
        manager = AlertManager(config)

        assert manager._evaluate_condition(0.2, 0.1, "gt") is True
        assert manager._evaluate_condition(0.1, 0.1, "gt") is False
        assert manager._evaluate_condition(0.05, 0.1, "gt") is False

    def test_evaluate_condition_lte(self) -> None:
        """Test less than or equal condition."""
        config = AlertConfig()
        manager = AlertManager(config)

        assert manager._evaluate_condition(0.05, 0.1, "lte") is True
        assert manager._evaluate_condition(0.1, 0.1, "lte") is True
        assert manager._evaluate_condition(0.2, 0.1, "lte") is False

    def test_check_alerts_triggers(self) -> None:
        """Test alert triggering."""
        rule = AlertRule(name="high_error", metric="error_rate", threshold=0.1)
        config = AlertConfig(rules=[rule])
        manager = AlertManager(config)

        metrics = {"error_rate": 0.2}
        triggered = manager.check_alerts(metrics)

        assert len(triggered) == 1
        assert triggered[0].name == "high_error"

    def test_check_alerts_cooldown(self) -> None:
        """Test alert cooldown."""
        rule = AlertRule(
            name="high_error",
            metric="error_rate",
            threshold=0.1,
            cooldown_seconds=60,
        )
        config = AlertConfig(rules=[rule])
        manager = AlertManager(config)

        # First check should trigger
        metrics = {"error_rate": 0.2}
        triggered = manager.check_alerts(metrics)
        assert len(triggered) == 1

        # Second check immediately should not trigger (cooldown)
        triggered = manager.check_alerts(metrics)
        assert len(triggered) == 0

    def test_get_alert_message(self) -> None:
        """Test alert message generation."""
        config = AlertConfig()
        manager = AlertManager(config)

        rule = AlertRule(name="test", metric="error_rate", threshold=0.1)
        message = manager.get_alert_message(rule, 0.25)

        assert "test" in message
        assert "error_rate" in message
        assert "0.25" in message
        assert "0.1" in message
