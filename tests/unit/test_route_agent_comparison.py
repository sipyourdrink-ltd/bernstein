"""Tests for the agent comparison view API (WEB-018)."""

from __future__ import annotations

from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from bernstein.core.routes.agent_comparison import (
    AgentMetrics,
    compute_agent_metrics,
    router,
)

# ---------------------------------------------------------------------------
# AgentMetrics property tests
# ---------------------------------------------------------------------------


class TestAgentMetrics:
    def test_success_rate_with_tasks(self) -> None:
        m = AgentMetrics(adapter="claude", model="opus", total_tasks=10, succeeded=7, failed=3)
        assert m.success_rate == pytest.approx(0.7)

    def test_success_rate_zero_tasks(self) -> None:
        m = AgentMetrics(adapter="claude", model="opus", total_tasks=0)
        assert m.success_rate == 0.0

    def test_cost_per_task_with_tasks(self) -> None:
        m = AgentMetrics(adapter="claude", model="opus", total_tasks=4, total_cost_usd=2.0)
        assert m.cost_per_task == pytest.approx(0.5)

    def test_cost_per_task_zero_tasks(self) -> None:
        m = AgentMetrics(adapter="claude", model="opus", total_tasks=0, total_cost_usd=0.0)
        assert m.cost_per_task == 0.0

    def test_model_dump_includes_computed_fields(self) -> None:
        m = AgentMetrics(adapter="codex", model="gpt-5.4-mini", total_tasks=2, succeeded=1, total_cost_usd=1.0)
        d = m.model_dump()
        assert "success_rate" in d
        assert "cost_per_task" in d
        assert d["success_rate"] == pytest.approx(0.5)
        assert d["cost_per_task"] == pytest.approx(0.5)

    def test_default_quality_gate_pass_rate(self) -> None:
        m = AgentMetrics(adapter="claude", model="sonnet")
        assert m.quality_gate_pass_rate == 1.0


# ---------------------------------------------------------------------------
# compute_agent_metrics tests
# ---------------------------------------------------------------------------


def _session(
    provider: str = "claude",
    model: str = "opus",
    status: str = "done",
    duration_s: float = 60.0,
    cost_usd: float = 0.10,
    quality_gate_passed: bool = True,
) -> dict[str, Any]:
    return {
        "provider": provider,
        "model": model,
        "status": status,
        "duration_s": duration_s,
        "cost_usd": cost_usd,
        "quality_gate_passed": quality_gate_passed,
    }


class TestComputeAgentMetrics:
    def test_empty_sessions(self) -> None:
        result = compute_agent_metrics([])
        assert result == []

    def test_single_group(self) -> None:
        sessions = [
            _session(status="done", duration_s=30.0, cost_usd=0.05),
            _session(status="done", duration_s=60.0, cost_usd=0.15),
            _session(status="failed", duration_s=90.0, cost_usd=0.10),
        ]
        result = compute_agent_metrics(sessions)
        assert len(result) == 1

        m = result[0]
        assert m.adapter == "claude"
        assert m.model == "opus"
        assert m.total_tasks == 3
        assert m.succeeded == 2
        assert m.failed == 1
        assert m.avg_completion_secs == pytest.approx(60.0)
        assert m.total_cost_usd == pytest.approx(0.30, abs=1e-4)
        assert m.success_rate == pytest.approx(2 / 3)
        assert m.cost_per_task == pytest.approx(0.10, abs=1e-4)

    def test_multiple_groups_sorted(self) -> None:
        sessions = [
            _session(provider="codex", model="gpt-5.4-mini"),
            _session(provider="claude", model="sonnet"),
            _session(provider="claude", model="opus"),
        ]
        result = compute_agent_metrics(sessions)
        assert len(result) == 3
        # Should be sorted by (adapter, model)
        keys = [(m.adapter, m.model) for m in result]
        assert keys == [("claude", "opus"), ("claude", "sonnet"), ("codex", "gpt-5.4-mini")]

    def test_quality_gate_pass_rate(self) -> None:
        sessions = [
            _session(quality_gate_passed=True),
            _session(quality_gate_passed=True),
            _session(quality_gate_passed=False),
        ]
        result = compute_agent_metrics(sessions)
        assert len(result) == 1
        assert result[0].quality_gate_pass_rate == pytest.approx(2 / 3, abs=1e-3)

    def test_missing_fields_use_defaults(self) -> None:
        sessions = [{"status": "done"}, {"status": "failed"}]
        result = compute_agent_metrics(sessions)
        assert len(result) == 1
        m = result[0]
        assert m.adapter == "unknown"
        assert m.model == "unknown"
        assert m.total_tasks == 2
        assert m.succeeded == 1
        assert m.failed == 1
        assert m.avg_completion_secs == 0.0
        assert m.total_cost_usd == 0.0

    def test_zero_duration_excluded_from_avg(self) -> None:
        sessions = [
            _session(duration_s=0.0),
            _session(duration_s=120.0),
        ]
        result = compute_agent_metrics(sessions)
        # duration_s=0.0 is falsy, so excluded from average
        assert result[0].avg_completion_secs == pytest.approx(120.0)

    def test_no_quality_gate_field_defaults_to_1(self) -> None:
        sessions = [{"provider": "claude", "model": "opus", "status": "done"}]
        result = compute_agent_metrics(sessions)
        assert result[0].quality_gate_pass_rate == 1.0


# ---------------------------------------------------------------------------
# Route integration tests
# ---------------------------------------------------------------------------


@pytest.fixture()
def comparison_app() -> FastAPI:
    """Create a minimal FastAPI app with the agent comparison router."""
    app = FastAPI()
    app.include_router(router)

    # Minimal store mock with empty agents dict
    class _MockStore:
        agents: dict[str, Any] = {}
        _tasks: dict[str, Any] = {}

    app.state.store = _MockStore()  # type: ignore[attr-defined]
    return app


class TestAgentComparisonRoute:
    def test_get_comparison_empty(self, comparison_app: FastAPI) -> None:
        client = TestClient(comparison_app)
        resp = client.get("/agents/comparison")
        assert resp.status_code == 200
        assert resp.json() == []

    def test_get_comparison_with_agents(self, comparison_app: FastAPI) -> None:
        """Verify that the route populates metrics from store agents."""
        from dataclasses import dataclass, field
        from typing import Literal

        @dataclass
        class _ModelConfig:
            model: str = "sonnet"
            effort: str = "high"

        @dataclass
        class _Agent:
            id: str = "agent-1"
            role: str = "backend"
            provider: str | None = "claude"
            model_config: _ModelConfig = field(default_factory=_ModelConfig)
            status: Literal["starting", "working", "idle", "dead"] = "dead"
            exit_code: int | None = 0
            spawn_ts: float = 1000.0
            task_ids: list[str] = field(default_factory=list)

        store = comparison_app.state.store
        store.agents = {
            "agent-1": _Agent(id="agent-1", provider="claude", exit_code=0),
            "agent-2": _Agent(
                id="agent-2", provider="codex", model_config=_ModelConfig(model="gpt-5.4-mini"), exit_code=1
            ),
        }

        client = TestClient(comparison_app)
        resp = client.get("/agents/comparison")
        assert resp.status_code == 200

        data = resp.json()
        assert len(data) == 2

        # Sorted by (adapter, model)
        assert data[0]["adapter"] == "claude"
        assert data[0]["model"] == "sonnet"
        assert data[1]["adapter"] == "codex"
        assert data[1]["model"] == "gpt-5.4-mini"

        # exit_code=0 -> done, exit_code=1 -> failed
        assert data[0]["succeeded"] == 1
        assert data[1]["failed"] == 1
