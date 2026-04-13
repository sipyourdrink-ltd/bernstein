"""Tests for HOOK-001 — hook event taxonomy (hook_events.py)."""

from __future__ import annotations

import time

import pytest
from bernstein.core.hook_events import (
    AGENT_EVENTS,
    BLOCKING_EVENTS,
    EVENT_PAYLOAD_MAP,
    MERGE_EVENTS,
    TASK_EVENTS,
    AgentPayload,
    BlockingHookPayload,
    BudgetPayload,
    CircuitBreakerPayload,
    ClusterPayload,
    ConfigDriftPayload,
    HookEvent,
    HookPayload,
    MergePayload,
    OrchestratorPayload,
    PermissionPayload,
    PlanPayload,
    QualityGatePayload,
    SecretDetectedPayload,
    TaskPayload,
    payload_class_for,
)

# ---------------------------------------------------------------------------
# Enum completeness
# ---------------------------------------------------------------------------


class TestHookEventEnum:
    """HookEvent enum has the required 28+ members."""

    def test_minimum_event_count(self) -> None:
        assert len(HookEvent) >= 28

    def test_all_values_are_strings(self) -> None:
        for member in HookEvent:
            assert isinstance(member.value, str)

    def test_no_duplicate_values(self) -> None:
        values = [m.value for m in HookEvent]
        assert len(values) == len(set(values))

    @pytest.mark.parametrize(
        "event_name",
        [
            "task.created",
            "task.claimed",
            "task.completed",
            "task.failed",
            "task.retried",
            "agent.spawned",
            "agent.heartbeat",
            "agent.completed",
            "agent.killed",
            "agent.stalled",
            "merge.started",
            "merge.completed",
            "merge.conflict",
            "quality_gate.passed",
            "quality_gate.failed",
            "budget.threshold",
            "budget.exceeded",
            "config.drift",
            "orchestrator.tick",
            "orchestrator.startup",
            "orchestrator.shutdown",
            "plan.loaded",
            "plan.stage_completed",
            "permission.denied",
            "permission.escalated",
            "secret.detected",
            "circuit_breaker.tripped",
            "cluster.node_joined",
        ],
    )
    def test_required_event_exists(self, event_name: str) -> None:
        found = [m for m in HookEvent if m.value == event_name]
        assert len(found) == 1, f"Missing event: {event_name}"


# ---------------------------------------------------------------------------
# Convenience sets
# ---------------------------------------------------------------------------


class TestEventSets:
    """Convenience frozensets group events correctly."""

    def test_task_events_count(self) -> None:
        assert len(TASK_EVENTS) == 5

    def test_agent_events_count(self) -> None:
        assert len(AGENT_EVENTS) == 5

    def test_merge_events_count(self) -> None:
        assert len(MERGE_EVENTS) == 3

    def test_blocking_events_are_pre_actions(self) -> None:
        for event in BLOCKING_EVENTS:
            assert event.value.startswith("pre_")


# ---------------------------------------------------------------------------
# Payload mapping
# ---------------------------------------------------------------------------


class TestPayloadMapping:
    """EVENT_PAYLOAD_MAP covers every HookEvent member."""

    def test_all_events_have_payload_class(self) -> None:
        for event in HookEvent:
            assert event in EVENT_PAYLOAD_MAP, f"Missing mapping for {event.value}"

    def test_payload_class_for_known(self) -> None:
        assert payload_class_for(HookEvent.TASK_CREATED) is TaskPayload

    def test_payload_class_for_agent(self) -> None:
        assert payload_class_for(HookEvent.AGENT_SPAWNED) is AgentPayload

    def test_payload_class_for_merge(self) -> None:
        assert payload_class_for(HookEvent.MERGE_STARTED) is MergePayload


# ---------------------------------------------------------------------------
# Base HookPayload
# ---------------------------------------------------------------------------


class TestHookPayload:
    """HookPayload base class serialises correctly."""

    def test_to_dict_has_event_and_timestamp(self) -> None:
        before = time.time()
        p = HookPayload(event=HookEvent.ORCHESTRATOR_TICK)
        d = p.to_dict()
        after = time.time()
        assert d["event"] == "orchestrator.tick"
        assert before <= d["timestamp"] <= after

    def test_to_dict_includes_metadata(self) -> None:
        p = HookPayload(
            event=HookEvent.ORCHESTRATOR_TICK,
            metadata={"key": "value"},
        )
        d = p.to_dict()
        assert d["metadata"] == {"key": "value"}

    def test_to_dict_omits_empty_metadata(self) -> None:
        p = HookPayload(event=HookEvent.ORCHESTRATOR_TICK)
        d = p.to_dict()
        assert "metadata" not in d


# ---------------------------------------------------------------------------
# Domain payloads
# ---------------------------------------------------------------------------


class TestTaskPayload:
    """TaskPayload serialises task-specific fields."""

    def test_to_dict_includes_task_fields(self) -> None:
        p = TaskPayload(
            event=HookEvent.TASK_CREATED,
            task_id="t1",
            role="backend",
            title="Fix bug",
        )
        d = p.to_dict()
        assert d["task_id"] == "t1"
        assert d["role"] == "backend"
        assert d["title"] == "Fix bug"

    def test_error_only_when_set(self) -> None:
        p = TaskPayload(event=HookEvent.TASK_FAILED, task_id="t2", error="timeout")
        d = p.to_dict()
        assert d["error"] == "timeout"

    def test_retry_count_only_when_nonzero(self) -> None:
        p = TaskPayload(event=HookEvent.TASK_RETRIED, task_id="t3", retry_count=2)
        d = p.to_dict()
        assert d["retry_count"] == 2


class TestAgentPayload:
    """AgentPayload serialises agent-specific fields."""

    def test_to_dict_has_session_and_role(self) -> None:
        p = AgentPayload(
            event=HookEvent.AGENT_SPAWNED,
            session_id="sess-1",
            role="qa",
            model="sonnet",
        )
        d = p.to_dict()
        assert d["session_id"] == "sess-1"
        assert d["model"] == "sonnet"

    def test_reason_included_when_set(self) -> None:
        p = AgentPayload(
            event=HookEvent.AGENT_KILLED,
            session_id="sess-2",
            role="backend",
            reason="budget_exceeded",
        )
        d = p.to_dict()
        assert d["reason"] == "budget_exceeded"


class TestMergePayload:
    """MergePayload serialises merge-specific fields."""

    def test_to_dict_basic(self) -> None:
        p = MergePayload(
            event=HookEvent.MERGE_STARTED,
            branch="feature/x",
            target="main",
        )
        d = p.to_dict()
        assert d["branch"] == "feature/x"
        assert d["target"] == "main"

    def test_conflict_files_included_when_present(self) -> None:
        p = MergePayload(
            event=HookEvent.MERGE_CONFLICT,
            branch="feature/y",
            target="main",
            conflict_files=["a.py", "b.py"],
        )
        d = p.to_dict()
        assert d["conflict_files"] == ["a.py", "b.py"]


class TestQualityGatePayload:
    """QualityGatePayload serialises gate-specific fields."""

    def test_to_dict_has_gate_name(self) -> None:
        p = QualityGatePayload(
            event=HookEvent.QUALITY_GATE_PASSED,
            gate_name="lint",
            task_id="t5",
        )
        d = p.to_dict()
        assert d["gate_name"] == "lint"


class TestBudgetPayload:
    """BudgetPayload serialises spend fields."""

    def test_to_dict_has_amounts(self) -> None:
        p = BudgetPayload(
            event=HookEvent.BUDGET_THRESHOLD,
            current_spend_usd=8.50,
            budget_usd=10.0,
            percent=85.0,
        )
        d = p.to_dict()
        assert d["current_spend_usd"] == pytest.approx(8.50)
        assert d["percent"] == pytest.approx(85.0)


class TestConfigDriftPayload:
    """ConfigDriftPayload serialises config key drift."""

    def test_to_dict_basic(self) -> None:
        p = ConfigDriftPayload(
            event=HookEvent.CONFIG_DRIFT,
            key="model",
            expected="sonnet",
            actual="opus",
        )
        d = p.to_dict()
        assert d["key"] == "model"
        assert d["expected"] == "sonnet"
        assert d["actual"] == "opus"


class TestOrchestratorPayload:
    """OrchestratorPayload serialises tick/agent counts."""

    def test_to_dict_has_tick(self) -> None:
        p = OrchestratorPayload(
            event=HookEvent.ORCHESTRATOR_TICK,
            tick_number=42,
            active_agents=3,
            open_tasks=5,
        )
        d = p.to_dict()
        assert d["tick_number"] == 42
        assert d["active_agents"] == 3


class TestPlanPayload:
    """PlanPayload serialises plan fields."""

    def test_to_dict_basic(self) -> None:
        p = PlanPayload(
            event=HookEvent.PLAN_LOADED,
            plan_path="/tmp/plan.yaml",
            total_stages=4,
        )
        d = p.to_dict()
        assert d["plan_path"] == "/tmp/plan.yaml"
        assert d["total_stages"] == 4


class TestPermissionPayload:
    """PermissionPayload serialises permission fields."""

    def test_to_dict_basic(self) -> None:
        p = PermissionPayload(
            event=HookEvent.PERMISSION_DENIED,
            task_id="t10",
            tool="Bash",
            reason="not allowed",
        )
        d = p.to_dict()
        assert d["tool"] == "Bash"


class TestSecretDetectedPayload:
    """SecretDetectedPayload serialises secret detection fields."""

    def test_to_dict_basic(self) -> None:
        p = SecretDetectedPayload(
            event=HookEvent.SECRET_DETECTED,
            file_path="config.py",
            secret_type="api_key",
            line_number=42,
        )
        d = p.to_dict()
        assert d["secret_type"] == "api_key"
        assert d["line_number"] == 42


class TestCircuitBreakerPayload:
    """CircuitBreakerPayload serialises breaker fields."""

    def test_to_dict_basic(self) -> None:
        p = CircuitBreakerPayload(
            event=HookEvent.CIRCUIT_BREAKER_TRIPPED,
            breaker_name="spawn",
            failure_count=5,
            cooldown_s=30.0,
        )
        d = p.to_dict()
        assert d["breaker_name"] == "spawn"
        assert d["failure_count"] == 5


class TestClusterPayload:
    """ClusterPayload serialises cluster node info."""

    def test_to_dict_basic(self) -> None:
        p = ClusterPayload(
            event=HookEvent.CLUSTER_NODE_JOINED,
            node_id="node-2",
            node_address="10.0.0.2:8052",
        )
        d = p.to_dict()
        assert d["node_id"] == "node-2"


class TestBlockingHookPayload:
    """BlockingHookPayload serialises blocking action fields."""

    def test_to_dict_basic(self) -> None:
        p = BlockingHookPayload(
            event=HookEvent.PRE_MERGE,
            action="merge",
            context={"branch": "feature/x"},
        )
        d = p.to_dict()
        assert d["action"] == "merge"
        assert d["context"]["branch"] == "feature/x"

    def test_context_omitted_when_empty(self) -> None:
        p = BlockingHookPayload(event=HookEvent.PRE_SPAWN, action="spawn")
        d = p.to_dict()
        assert "context" not in d
