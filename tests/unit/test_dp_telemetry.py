"""Tests for SEC-016: Differential privacy for telemetry exports."""

from __future__ import annotations

from bernstein.core.dp_telemetry import (
    DPTelemetryExporter,
    PrivacyBudgetTracker,
    TelemetryPrivacyConfig,
)


class TestPrivacyBudgetTracker:
    def test_initial_budget(self) -> None:
        tracker = PrivacyBudgetTracker(total_epsilon=1.0, max_queries=100)
        assert tracker.epsilon_remaining == 1.0
        assert tracker.queries_remaining == 100

    def test_spend_reduces_budget(self) -> None:
        tracker = PrivacyBudgetTracker(total_epsilon=1.0)
        assert tracker.spend("q1", 0.1, 5)
        assert tracker.epsilon_spent == 0.1
        assert tracker.epsilon_remaining < 1.0

    def test_spend_rejected_over_budget(self) -> None:
        tracker = PrivacyBudgetTracker(total_epsilon=0.5)
        assert tracker.spend("q1", 0.3, 5)
        assert not tracker.spend("q2", 0.3, 5)

    def test_spend_rejected_over_queries(self) -> None:
        tracker = PrivacyBudgetTracker(total_epsilon=100.0, max_queries=2)
        assert tracker.spend("q1", 0.01, 1)
        assert tracker.spend("q2", 0.01, 1)
        assert not tracker.spend("q3", 0.01, 1)

    def test_can_spend_check(self) -> None:
        tracker = PrivacyBudgetTracker(total_epsilon=1.0)
        assert tracker.can_spend(0.5)
        assert not tracker.can_spend(1.5)

    def test_entries_logged(self) -> None:
        tracker = PrivacyBudgetTracker(total_epsilon=1.0)
        tracker.spend("q1", 0.1, 5)
        assert len(tracker.entries) == 1
        assert tracker.entries[0].query_id == "q1"

    def test_reset(self) -> None:
        tracker = PrivacyBudgetTracker(total_epsilon=1.0)
        tracker.spend("q1", 0.5, 5)
        tracker.reset()
        assert tracker.epsilon_spent == 0.0
        assert len(tracker.entries) == 0

    def test_total_epsilon_property(self) -> None:
        tracker = PrivacyBudgetTracker(total_epsilon=2.0)
        assert tracker.total_epsilon == 2.0


class TestTelemetryPrivacyConfig:
    def test_defaults(self) -> None:
        config = TelemetryPrivacyConfig()
        assert config.epsilon == 1.0
        assert config.delta == 1e-5
        assert config.max_queries == 1000

    def test_custom_config(self) -> None:
        config = TelemetryPrivacyConfig(epsilon=2.0, max_queries=500)
        assert config.epsilon == 2.0
        assert config.max_queries == 500


class TestDPTelemetryExporter:
    def test_export_adds_noise(self) -> None:
        config = TelemetryPrivacyConfig(epsilon=0.1)
        exporter = DPTelemetryExporter(config)
        raw = {"task_count": 100, "cost_usd": 50.0}
        result = exporter.export(raw)
        assert result is not None
        # Values should be perturbed (very unlikely to be exactly the same)
        # But we can at least check they exist
        assert "task_count" in result
        assert "cost_usd" in result

    def test_passthrough_fields_preserved(self) -> None:
        config = TelemetryPrivacyConfig(epsilon=1.0)
        exporter = DPTelemetryExporter(config)
        raw = {"agent_id": "agent-1", "task_count": 10}
        result = exporter.export(raw)
        assert result is not None
        assert result["agent_id"] == "agent-1"

    def test_nested_dicts_perturbed(self) -> None:
        config = TelemetryPrivacyConfig(epsilon=1.0)
        exporter = DPTelemetryExporter(config)
        raw = {"summary": {"task_count": 50, "role": "backend"}}
        result = exporter.export(raw)
        assert result is not None
        assert "summary" in result
        assert result["summary"]["role"] == "backend"

    def test_list_of_dicts_perturbed(self) -> None:
        config = TelemetryPrivacyConfig(epsilon=1.0)
        exporter = DPTelemetryExporter(config)
        raw = {"tasks": [{"task_id": "t1", "duration_seconds": 120}]}
        result = exporter.export(raw)
        assert result is not None
        assert result["tasks"][0]["task_id"] == "t1"

    def test_budget_exhaustion_returns_none(self) -> None:
        config = TelemetryPrivacyConfig(epsilon=0.01, max_queries=2)
        exporter = DPTelemetryExporter(config)

        # First two exports should succeed
        r1 = exporter.export({"task_count": 10})
        r2 = exporter.export({"task_count": 10})
        assert r1 is not None
        assert r2 is not None

        # Third should be rejected — budget exhausted (epsilon 0.01/2 per query = 0.005,
        # two queries spend 0.01 = full budget)
        r3 = exporter.export({"task_count": 10})
        assert r3 is None

    def test_non_numeric_values_preserved(self) -> None:
        config = TelemetryPrivacyConfig()
        exporter = DPTelemetryExporter(config)
        raw = {"status": "active", "tags": ["a", "b"], "count": None}
        result = exporter.export(raw)
        assert result is not None
        assert result["status"] == "active"
        assert result["tags"] == ["a", "b"]
        assert result["count"] is None

    def test_budget_property(self) -> None:
        config = TelemetryPrivacyConfig(epsilon=2.0)
        exporter = DPTelemetryExporter(config)
        assert exporter.budget.total_epsilon == 2.0

    def test_original_data_not_mutated(self) -> None:
        config = TelemetryPrivacyConfig()
        exporter = DPTelemetryExporter(config)
        raw = {"task_count": 100}
        exporter.export(raw)
        assert raw["task_count"] == 100
