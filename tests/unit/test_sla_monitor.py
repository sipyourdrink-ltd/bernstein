"""Tests for ENT-005: SLA monitoring with breach alerting."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from bernstein.core.sla_monitor import (
    SLAAlert,
    SLADefinition,
    SLAEvaluation,
    SLAMetricKind,
    SLAMonitor,
    SLAStatus,
    default_sla_definitions,
)


def _alert_collector(target: list[SLAAlert]) -> Any:
    """Return a typed callback that appends alerts to *target*."""

    def _cb(alert: SLAAlert) -> None:
        target.append(alert)

    return _cb


@pytest.fixture()
def monitor() -> SLAMonitor:
    """Create an SLA monitor with default definitions."""
    return SLAMonitor(definitions=default_sla_definitions())


@pytest.fixture()
def simple_monitor() -> SLAMonitor:
    """Create a monitor with a single simple SLA."""
    return SLAMonitor(
        definitions=[
            SLADefinition(
                name="completion",
                metric=SLAMetricKind.TASK_COMPLETION_RATE,
                target=0.90,
                warning_threshold=0.92,
                window_seconds=3600,
            ),
        ]
    )


class TestSLADefinition:
    """Test SLA definition creation."""

    def test_defaults(self) -> None:
        d = SLADefinition(
            name="test",
            metric=SLAMetricKind.TASK_COMPLETION_RATE,
            target=0.95,
            warning_threshold=0.97,
        )
        assert d.window_seconds == 3600
        assert d.severity == "critical"

    def test_custom_values(self) -> None:
        d = SLADefinition(
            name="custom",
            metric=SLAMetricKind.ERROR_RATE,
            target=0.05,
            warning_threshold=0.04,
            window_seconds=1800,
            severity="warning",
        )
        assert d.window_seconds == 1800
        assert d.severity == "warning"


class TestDefaultDefinitions:
    """Test default SLA definitions."""

    def test_has_definitions(self) -> None:
        defs = default_sla_definitions()
        assert len(defs) > 0

    def test_definitions_have_names(self) -> None:
        for d in default_sla_definitions():
            assert d.name
            assert d.target > 0


class TestSLAMonitor:
    """Test the SLA monitor core functionality."""

    def test_add_definition(self) -> None:
        mon = SLAMonitor()
        mon.add_definition(
            SLADefinition(
                name="test",
                metric=SLAMetricKind.TASK_COMPLETION_RATE,
                target=0.90,
                warning_threshold=0.92,
            )
        )
        results = mon.evaluate()
        assert len(results) == 1
        assert results[0].sla_name == "test"

    def test_remove_definition(self) -> None:
        mon = SLAMonitor(
            definitions=[
                SLADefinition(
                    name="test",
                    metric=SLAMetricKind.TASK_COMPLETION_RATE,
                    target=0.90,
                    warning_threshold=0.92,
                )
            ]
        )
        assert mon.remove_definition("test") is True
        assert mon.remove_definition("nonexistent") is False
        assert len(mon.evaluate()) == 0

    def test_evaluate_no_data(self, simple_monitor: SLAMonitor) -> None:
        results = simple_monitor.evaluate()
        assert len(results) == 1
        assert results[0].status == SLAStatus.UNKNOWN

    def test_evaluate_met(self, simple_monitor: SLAMonitor) -> None:
        # Record enough successful observations
        now = 1000.0
        for i in range(20):
            simple_monitor.record_observation(SLAMetricKind.TASK_COMPLETION_RATE, 1.0, timestamp=now + i)
        results = simple_monitor.evaluate(now=now + 20)
        assert results[0].status == SLAStatus.MET
        assert results[0].current_value == pytest.approx(1.0)

    def test_evaluate_warning(self) -> None:
        # For higher-is-better: warning zone is [warning_threshold, target).
        # Set warning_threshold < target so there's a warning zone.
        mon = SLAMonitor(
            definitions=[
                SLADefinition(
                    name="completion",
                    metric=SLAMetricKind.TASK_COMPLETION_RATE,
                    target=0.95,
                    warning_threshold=0.90,
                    window_seconds=3600,
                ),
            ]
        )
        now = 1000.0
        # 91% success rate — between warning (0.90) and target (0.95)
        for i in range(91):
            mon.record_observation(SLAMetricKind.TASK_COMPLETION_RATE, 1.0, timestamp=now + i)
        for i in range(9):
            mon.record_observation(SLAMetricKind.TASK_COMPLETION_RATE, 0.0, timestamp=now + 91 + i)
        results = mon.evaluate(now=now + 100)
        assert results[0].status == SLAStatus.WARNING

    def test_evaluate_breached(self, simple_monitor: SLAMonitor) -> None:
        now = 1000.0
        # 80% success rate — below target (0.90)
        for i in range(80):
            simple_monitor.record_observation(SLAMetricKind.TASK_COMPLETION_RATE, 1.0, timestamp=now + i)
        for i in range(20):
            simple_monitor.record_observation(SLAMetricKind.TASK_COMPLETION_RATE, 0.0, timestamp=now + 80 + i)
        results = simple_monitor.evaluate(now=now + 100)
        assert results[0].status == SLAStatus.BREACHED

    def test_breach_duration_tracking(self, simple_monitor: SLAMonitor) -> None:
        now = 1000.0
        # Create a breach
        for i in range(10):
            simple_monitor.record_observation(SLAMetricKind.TASK_COMPLETION_RATE, 0.0, timestamp=now + i)
        simple_monitor.evaluate(now=now + 10)  # First evaluation triggers breach start
        results = simple_monitor.evaluate(now=now + 70)  # 60 seconds later
        assert results[0].breach_duration_s >= 60.0

    def test_window_pruning(self) -> None:
        mon = SLAMonitor(
            definitions=[
                SLADefinition(
                    name="short",
                    metric=SLAMetricKind.TASK_COMPLETION_RATE,
                    target=0.90,
                    warning_threshold=0.92,
                    window_seconds=60,
                )
            ]
        )
        # Old observations that should be pruned
        for i in range(10):
            mon.record_observation(SLAMetricKind.TASK_COMPLETION_RATE, 0.0, timestamp=100.0 + i)
        # Recent observations
        now = 1000.0
        for i in range(10):
            mon.record_observation(SLAMetricKind.TASK_COMPLETION_RATE, 1.0, timestamp=now + i)
        results = mon.evaluate(now=now + 10)
        assert results[0].status == SLAStatus.MET  # Only recent data counts


class TestSLAAlerts:
    """Test alert generation."""

    def test_breach_alert(self) -> None:
        alerts: list[SLAAlert] = []
        mon = SLAMonitor(
            definitions=[
                SLADefinition(
                    name="test_sla",
                    metric=SLAMetricKind.TASK_COMPLETION_RATE,
                    target=0.90,
                    warning_threshold=0.92,
                )
            ],
            alert_callback=_alert_collector(alerts),
        )
        now = 1000.0
        for i in range(10):
            mon.record_observation(SLAMetricKind.TASK_COMPLETION_RATE, 0.0, timestamp=now + i)
        mon.evaluate(now=now + 10)
        assert len(alerts) == 1
        assert alerts[0].alert_type == "breached"
        assert alerts[0].sla_name == "test_sla"

    def test_warning_alert(self) -> None:
        alerts: list[SLAAlert] = []
        mon = SLAMonitor(
            definitions=[
                SLADefinition(
                    name="test_sla",
                    metric=SLAMetricKind.TASK_COMPLETION_RATE,
                    target=0.95,
                    warning_threshold=0.90,
                )
            ],
            alert_callback=_alert_collector(alerts),
        )
        now = 1000.0
        # 91% — between warning_threshold (0.90) and target (0.95)
        for i in range(91):
            mon.record_observation(SLAMetricKind.TASK_COMPLETION_RATE, 1.0, timestamp=now + i)
        for i in range(9):
            mon.record_observation(SLAMetricKind.TASK_COMPLETION_RATE, 0.0, timestamp=now + 91 + i)
        mon.evaluate(now=now + 100)
        assert len(alerts) == 1
        assert alerts[0].alert_type == "imminent"

    def test_recovery_alert(self) -> None:
        alerts: list[SLAAlert] = []
        mon = SLAMonitor(
            definitions=[
                SLADefinition(
                    name="test_sla",
                    metric=SLAMetricKind.TASK_COMPLETION_RATE,
                    target=0.90,
                    warning_threshold=0.92,
                    window_seconds=60,
                )
            ],
            alert_callback=_alert_collector(alerts),
        )
        now = 1000.0
        # First: breach
        for i in range(10):
            mon.record_observation(SLAMetricKind.TASK_COMPLETION_RATE, 0.0, timestamp=now + i)
        mon.evaluate(now=now + 10)

        # Then: recover (new window)
        now2 = now + 120
        for i in range(20):
            mon.record_observation(SLAMetricKind.TASK_COMPLETION_RATE, 1.0, timestamp=now2 + i)
        mon.evaluate(now=now2 + 20)
        recovery_alerts = [a for a in alerts if a.alert_type == "recovered"]
        assert len(recovery_alerts) == 1

    def test_no_duplicate_alerts(self) -> None:
        alerts: list[SLAAlert] = []
        mon = SLAMonitor(
            definitions=[
                SLADefinition(
                    name="test_sla",
                    metric=SLAMetricKind.TASK_COMPLETION_RATE,
                    target=0.90,
                    warning_threshold=0.92,
                )
            ],
            alert_callback=_alert_collector(alerts),
        )
        now = 1000.0
        for i in range(10):
            mon.record_observation(SLAMetricKind.TASK_COMPLETION_RATE, 0.0, timestamp=now + i)
        mon.evaluate(now=now + 10)
        mon.evaluate(now=now + 20)  # Second eval with same breach
        breach_alerts = [a for a in alerts if a.alert_type == "breached"]
        assert len(breach_alerts) == 1  # Only one breach alert

    def test_acknowledge_alert(self, simple_monitor: SLAMonitor) -> None:
        now = 1000.0
        for i in range(10):
            simple_monitor.record_observation(SLAMetricKind.TASK_COMPLETION_RATE, 0.0, timestamp=now + i)
        simple_monitor.evaluate(now=now + 10)
        alerts = simple_monitor.get_alerts(unacknowledged_only=True)
        assert len(alerts) == 1
        simple_monitor.acknowledge_alert(0)
        alerts = simple_monitor.get_alerts(unacknowledged_only=True)
        assert len(alerts) == 0

    def test_alert_to_dict(self) -> None:
        evaluation = SLAEvaluation(
            sla_name="test",
            metric=SLAMetricKind.TASK_COMPLETION_RATE,
            target=0.90,
            current_value=0.80,
            status=SLAStatus.BREACHED,
        )
        alert = SLAAlert(
            sla_name="test",
            alert_type="breached",
            severity="critical",
            message="test breach",
            evaluation=evaluation,
            created_at=1000.0,
        )
        d = alert.to_dict()
        assert d["sla_name"] == "test"
        assert d["alert_type"] == "breached"
        assert d["evaluation"]["current_value"] == pytest.approx(0.80)


class TestLowerIsBetter:
    """Test SLAs where lower values are better (durations, error rates)."""

    def test_error_rate_met(self) -> None:
        mon = SLAMonitor(
            definitions=[
                SLADefinition(
                    name="errors",
                    metric=SLAMetricKind.ERROR_RATE,
                    target=0.10,
                    warning_threshold=0.08,
                )
            ]
        )
        now = 1000.0
        for i in range(20):
            mon.record_observation(SLAMetricKind.ERROR_RATE, 0.05, timestamp=now + i)
        results = mon.evaluate(now=now + 20)
        assert results[0].status == SLAStatus.MET

    def test_error_rate_breached(self) -> None:
        mon = SLAMonitor(
            definitions=[
                SLADefinition(
                    name="errors",
                    metric=SLAMetricKind.ERROR_RATE,
                    target=0.10,
                    warning_threshold=0.08,
                )
            ]
        )
        now = 1000.0
        for i in range(20):
            mon.record_observation(SLAMetricKind.ERROR_RATE, 0.20, timestamp=now + i)
        results = mon.evaluate(now=now + 20)
        assert results[0].status == SLAStatus.BREACHED

    def test_duration_p95_met(self) -> None:
        mon = SLAMonitor(
            definitions=[
                SLADefinition(
                    name="duration",
                    metric=SLAMetricKind.TASK_DURATION_P95,
                    target=1800.0,
                    warning_threshold=1500.0,
                )
            ]
        )
        now = 1000.0
        for i in range(20):
            mon.record_observation(SLAMetricKind.TASK_DURATION_P95, 600.0, timestamp=now + i)
        results = mon.evaluate(now=now + 20)
        assert results[0].status == SLAStatus.MET


class TestSLADashboard:
    """Test dashboard output."""

    def test_dashboard_structure(self, simple_monitor: SLAMonitor) -> None:
        dash = simple_monitor.get_dashboard()
        assert "slas" in dash
        assert "active_alerts" in dash
        assert "total_alerts" in dash
        assert isinstance(dash["slas"], list)


class TestSLAPersistence:
    """Test state persistence."""

    def test_save_state(self, simple_monitor: SLAMonitor, tmp_path: Path) -> None:
        path = tmp_path / "sla_state.json"
        simple_monitor.save_state(path)
        assert path.exists()
        data = json.loads(path.read_text())
        assert "definitions" in data
        assert "completion" in data["definitions"]


class TestSLAFromConfig:
    """Test creating monitor from config dicts."""

    def test_from_config(self) -> None:
        config: list[dict[str, Any]] = [
            {
                "name": "sla1",
                "metric": "task_completion_rate",
                "target": 0.95,
                "warning_threshold": 0.97,
            },
            {
                "name": "sla2",
                "metric": "error_rate",
                "target": 0.05,
            },
        ]
        mon = SLAMonitor.from_config(config)
        results = mon.evaluate()
        assert len(results) == 2

    def test_from_config_with_callback(self) -> None:
        alerts: list[SLAAlert] = []
        config: list[dict[str, Any]] = [
            {
                "name": "sla1",
                "metric": "task_completion_rate",
                "target": 0.95,
            },
        ]
        mon = SLAMonitor.from_config(config, alert_callback=lambda a: alerts.append(a))
        now = 1000.0
        for i in range(10):
            mon.record_observation(SLAMetricKind.TASK_COMPLETION_RATE, 0.0, timestamp=now + i)
        mon.evaluate(now=now + 10)
        assert len(alerts) > 0
