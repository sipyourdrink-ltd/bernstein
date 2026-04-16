"""Tests for agent-specific operational metrics."""

from __future__ import annotations

from bernstein.core.observability.agent_metrics import (
    AgentMetrics,
    AgentMetricsCollector,
)


def _make_metric(
    *,
    task_id: str = "t1",
    agent_id: str = "a1",
    model: str = "sonnet",
    role: str = "backend",
    complexity: str = "medium",
    claim_time: float = 100.0,
    first_output_time: float = 102.0,
    completion_time: float = 110.0,
    step_count: int = 3,
    result: str = "pass",
    retry_category: str | None = None,
    cost_usd: float = 0.05,
) -> AgentMetrics:
    return AgentMetrics(
        task_id=task_id,
        agent_id=agent_id,
        model=model,
        role=role,
        complexity=complexity,
        claim_time=claim_time,
        first_output_time=first_output_time,
        completion_time=completion_time,
        step_count=step_count,
        result=result,  # type: ignore[arg-type]
        retry_category=retry_category,  # type: ignore[arg-type]
        cost_usd=cost_usd,
    )


class TestAgentMetrics:
    def test_decision_latency(self) -> None:
        m = _make_metric(claim_time=100.0, first_output_time=103.5)
        assert m.decision_latency_s == 3.5

    def test_decision_latency_zero_when_missing(self) -> None:
        m = _make_metric(claim_time=0.0, first_output_time=0.0)
        assert m.decision_latency_s == 0.0

    def test_execution_duration(self) -> None:
        m = _make_metric(claim_time=100.0, completion_time=115.0)
        assert m.execution_duration_s == 15.0

    def test_execution_duration_zero_when_missing(self) -> None:
        m = _make_metric(claim_time=0.0, completion_time=0.0)
        assert m.execution_duration_s == 0.0


class TestAgentMetricsCollector:
    def test_pass_rate_mixed(self) -> None:
        collector = AgentMetricsCollector()
        collector.record(_make_metric(result="pass"))
        collector.record(_make_metric(result="pass"))
        collector.record(_make_metric(result="fail"))
        collector.record(_make_metric(result="retry"))
        assert collector.pass_rate() == 0.5

    def test_pass_rate_empty(self) -> None:
        collector = AgentMetricsCollector()
        assert collector.pass_rate() == 0.0

    def test_pass_rate_all_pass(self) -> None:
        collector = AgentMetricsCollector()
        collector.record(_make_metric(result="pass"))
        collector.record(_make_metric(result="pass"))
        assert collector.pass_rate() == 1.0

    def test_avg_decision_latency(self) -> None:
        collector = AgentMetricsCollector()
        collector.record(_make_metric(claim_time=100.0, first_output_time=104.0))  # 4s
        collector.record(_make_metric(claim_time=200.0, first_output_time=206.0))  # 6s
        assert collector.avg_decision_latency() == 5.0

    def test_avg_decision_latency_skips_zero(self) -> None:
        collector = AgentMetricsCollector()
        collector.record(_make_metric(claim_time=0.0, first_output_time=0.0))
        collector.record(_make_metric(claim_time=100.0, first_output_time=102.0))  # 2s
        assert collector.avg_decision_latency() == 2.0

    def test_retry_breakdown(self) -> None:
        collector = AgentMetricsCollector()
        collector.record(_make_metric(result="retry", retry_category="timeout"))
        collector.record(_make_metric(result="retry", retry_category="timeout"))
        collector.record(_make_metric(result="retry", retry_category="crash"))
        collector.record(_make_metric(result="pass"))
        breakdown = collector.retry_breakdown()
        assert breakdown == {"timeout": 2, "crash": 1}

    def test_retry_breakdown_empty(self) -> None:
        collector = AgentMetricsCollector()
        collector.record(_make_metric(result="pass"))
        assert collector.retry_breakdown() == {}

    def test_compound_success_rate(self) -> None:
        collector = AgentMetricsCollector()
        # 2 pass, 0 fail => p_step = 1.0, steps = 3 => 1.0^3 = 1.0
        collector.record(_make_metric(result="pass", step_count=3))
        collector.record(_make_metric(result="pass", step_count=3))
        assert collector.compound_success_rate() == 1.0

    def test_compound_success_rate_with_failures(self) -> None:
        collector = AgentMetricsCollector()
        # 3 pass, 1 fail => p_step = 0.75, avg_steps = 4
        for _ in range(3):
            collector.record(_make_metric(result="pass", step_count=4))
        collector.record(_make_metric(result="fail", step_count=4))
        rate = collector.compound_success_rate()
        expected = 0.75**4
        assert abs(rate - expected) < 1e-9

    def test_compound_success_rate_explicit_steps(self) -> None:
        collector = AgentMetricsCollector()
        collector.record(_make_metric(result="pass"))
        collector.record(_make_metric(result="fail"))
        # p_step = 0.5, explicit avg_steps = 2 => 0.5^2 = 0.25
        assert collector.compound_success_rate(avg_steps=2.0) == 0.25

    def test_compound_success_rate_empty(self) -> None:
        collector = AgentMetricsCollector()
        assert collector.compound_success_rate() == 0.0

    def test_filter_by_model(self) -> None:
        collector = AgentMetricsCollector()
        collector.record(_make_metric(model="sonnet", result="pass"))
        collector.record(_make_metric(model="sonnet", result="fail"))
        collector.record(_make_metric(model="haiku", result="pass"))
        assert collector.pass_rate(model="sonnet") == 0.5
        assert collector.pass_rate(model="haiku") == 1.0

    def test_filter_by_role(self) -> None:
        collector = AgentMetricsCollector()
        collector.record(_make_metric(role="backend", result="pass"))
        collector.record(_make_metric(role="backend", result="pass"))
        collector.record(_make_metric(role="qa", result="fail"))
        assert collector.pass_rate(role="backend") == 1.0
        assert collector.pass_rate(role="qa") == 0.0

    def test_filter_by_model_and_role(self) -> None:
        collector = AgentMetricsCollector()
        collector.record(_make_metric(model="sonnet", role="backend", result="pass"))
        collector.record(_make_metric(model="sonnet", role="qa", result="fail"))
        collector.record(_make_metric(model="haiku", role="backend", result="fail"))
        assert collector.pass_rate(model="sonnet", role="backend") == 1.0
        assert collector.pass_rate(model="sonnet", role="qa") == 0.0

    def test_to_summary(self) -> None:
        collector = AgentMetricsCollector()
        collector.record(
            _make_metric(
                result="pass",
                claim_time=100.0,
                first_output_time=102.0,
                step_count=3,
            )
        )
        collector.record(
            _make_metric(
                result="fail",
                claim_time=200.0,
                first_output_time=204.0,
                step_count=3,
                retry_category="timeout",
            )
        )
        summary = collector.to_summary()
        assert summary["total_tasks"] == 2
        assert summary["pass_rate"] == 0.5
        assert summary["avg_decision_latency_s"] == 3.0
        assert summary["retry_breakdown"] == {"timeout": 1}
        assert isinstance(summary["compound_success_rate"], float)
