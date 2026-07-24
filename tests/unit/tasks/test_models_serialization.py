"""Behavioral tests for task-model validation and serialization round-trips.

Covers the previously-untested logic paths in ``core/tasks/models.py``:
``Task.from_dict`` coercions, ``_normalize_attachments`` guards,
``ApprovalSpec`` validation, the cost-model and plan round-trips, and the
small predicate helpers (``ProgressSnapshot.is_same_progress``,
``NodeInfo.is_alive``, ``ClusterConfig.cluster_url_scheme``,
``OrchestratorConfig.__post_init__``).
"""

from __future__ import annotations

import time

import pytest

from bernstein.core.tasks.models import (
    AgentCostSummary,
    ApprovalSpec,
    ClusterConfig,
    CompletionSignal,
    ModelCostBreakdown,
    NodeInfo,
    OrchestratorConfig,
    PlanStatus,
    ProgressSnapshot,
    RunCostProjection,
    RunCostReport,
    Scope,
    Task,
    TaskCostEstimate,
    TaskPlan,
    TaskStatus,
    TaskType,
    _normalize_attachments,
)

# ---------------------------------------------------------------------------
# Task.from_dict
# ---------------------------------------------------------------------------


def test_task_from_dict_minimal_applies_defaults() -> None:
    task = Task.from_dict({"id": "x", "title": "T", "description": "D", "role": "backend"})
    assert task.status is TaskStatus.OPEN
    assert task.scope is Scope.MEDIUM
    assert task.priority == 2
    assert task.task_type is TaskType.STANDARD
    assert task.estimated_minutes == 30


def test_task_from_dict_invalid_task_type_falls_back_to_standard() -> None:
    task = Task.from_dict({"id": "x", "title": "T", "description": "D", "role": "backend", "task_type": "bogus"})
    assert task.task_type is TaskType.STANDARD


def test_task_from_dict_batch_eligible_none_preserved() -> None:
    task = Task.from_dict({"id": "x", "title": "T", "description": "D", "role": "backend", "batch_eligible": None})
    assert task.batch_eligible is None


def test_task_from_dict_batch_eligible_truthy_coerced_to_bool() -> None:
    task = Task.from_dict({"id": "x", "title": "T", "description": "D", "role": "backend", "batch_eligible": 1})
    assert task.batch_eligible is True


def test_task_from_dict_empty_tenant_id_defaults() -> None:
    task = Task.from_dict({"id": "x", "title": "T", "description": "D", "role": "backend", "tenant_id": ""})
    assert task.tenant_id == "default"


def test_task_from_dict_falsy_story_id_becomes_none() -> None:
    task = Task.from_dict({"id": "x", "title": "T", "description": "D", "role": "backend", "story_id": ""})
    assert task.story_id is None


def test_task_from_dict_truthy_story_id_kept_as_string() -> None:
    task = Task.from_dict({"id": "x", "title": "T", "description": "D", "role": "backend", "story_id": 5})
    assert task.story_id == "5"


def test_task_from_dict_skips_malformed_completion_signal() -> None:
    task = Task.from_dict(
        {
            "id": "x",
            "title": "T",
            "description": "D",
            "role": "backend",
            "completion_signals": [
                {"type": "path_exists", "value": "out.txt"},
                {"type": "path_exists"},  # missing value -> skipped
            ],
        }
    )
    assert len(task.completion_signals) == 1
    assert task.completion_signals[0].value == "out.txt"


def test_task_round_trips_figures_grounded_signal() -> None:
    """Issue #2888: the figures_grounded signal type survives to_dict/from_dict."""
    task = Task.from_dict(
        {
            "id": "x",
            "title": "T",
            "description": "D",
            "role": "backend",
            "completion_signals": [{"type": "figures_grounded", "value": "warn"}],
        }
    )
    assert task.completion_signals[0].type == "figures_grounded"
    assert task.completion_signals[0].value == "warn"
    restored = Task.from_dict(task.to_dict())
    assert restored.completion_signals[0].type == "figures_grounded"
    assert restored.completion_signals[0].value == "warn"


def test_task_from_dict_best_of_n_coercion() -> None:
    task = Task.from_dict({"id": "x", "title": "T", "description": "D", "role": "backend", "best_of_n": "3"})
    assert task.best_of_n == 3


def test_task_from_dict_best_of_n_none_preserved() -> None:
    task = Task.from_dict({"id": "x", "title": "T", "description": "D", "role": "backend", "best_of_n": None})
    assert task.best_of_n is None


def test_task_from_dict_status_round_trips_from_enum_value() -> None:
    task = Task.from_dict({"id": "x", "title": "T", "description": "D", "role": "backend", "status": "blocked"})
    assert task.status is TaskStatus.BLOCKED


def test_task_from_dict_parses_approval_spec() -> None:
    task = Task.from_dict(
        {
            "id": "x",
            "title": "T",
            "description": "D",
            "role": "backend",
            "approval_spec": {"prompt": "approve?", "timeout_seconds": 60, "default_action": "approve"},
        }
    )
    assert task.approval_spec is not None
    assert task.approval_spec.prompt == "approve?"
    assert task.approval_spec.default_action == "approve"


# ---------------------------------------------------------------------------
# _normalize_attachments
# ---------------------------------------------------------------------------


def test_normalize_attachments_none_returns_empty() -> None:
    assert _normalize_attachments(None) == []


def test_normalize_attachments_coerces_list_elements_to_str() -> None:
    assert _normalize_attachments(["a.png", 1]) == ["a.png", "1"]


def test_normalize_attachments_tuple_accepted() -> None:
    assert _normalize_attachments(("a.png", "b.png")) == ["a.png", "b.png"]


def test_normalize_attachments_rejects_bare_string() -> None:
    with pytest.raises(TypeError, match="list of paths"):
        _normalize_attachments("diagram.png")


def test_normalize_attachments_rejects_scalar() -> None:
    with pytest.raises(TypeError, match="list of paths"):
        _normalize_attachments(123)


# ---------------------------------------------------------------------------
# ApprovalSpec
# ---------------------------------------------------------------------------


def test_approval_spec_empty_prompt_raises() -> None:
    with pytest.raises(ValueError, match="non-empty"):
        ApprovalSpec(prompt="   ")


def test_approval_spec_non_positive_timeout_raises() -> None:
    with pytest.raises(ValueError, match="> 0"):
        ApprovalSpec(prompt="ok", timeout_seconds=0)


def test_approval_spec_defaults() -> None:
    spec = ApprovalSpec(prompt="proceed?")
    assert spec.timeout_seconds == 86_400
    assert spec.default_action == "reject"


def test_approval_spec_round_trip() -> None:
    spec = ApprovalSpec(prompt="proceed?", timeout_seconds=120, default_action="approve")
    assert ApprovalSpec.from_dict(spec.to_dict()) == spec


def test_approval_spec_from_dict_rejects_bad_action() -> None:
    with pytest.raises(ValueError, match="reject\\|approve\\|fail"):
        ApprovalSpec.from_dict({"prompt": "ok", "default_action": "maybe"})


# ---------------------------------------------------------------------------
# ProgressSnapshot.is_same_progress
# ---------------------------------------------------------------------------


def test_progress_snapshot_same_ignores_timestamp_and_file() -> None:
    a = ProgressSnapshot(timestamp=1.0, files_changed=3, tests_passing=10, errors=0, last_file="a.py")
    b = ProgressSnapshot(timestamp=2.0, files_changed=3, tests_passing=10, errors=0, last_file="b.py")
    assert a.is_same_progress(b) is True


def test_progress_snapshot_different_when_files_changed_differs() -> None:
    a = ProgressSnapshot(timestamp=1.0, files_changed=3)
    b = ProgressSnapshot(timestamp=1.0, files_changed=4)
    assert a.is_same_progress(b) is False


def test_progress_snapshot_different_when_errors_differ() -> None:
    a = ProgressSnapshot(timestamp=1.0, files_changed=3, errors=0)
    b = ProgressSnapshot(timestamp=1.0, files_changed=3, errors=1)
    assert a.is_same_progress(b) is False


# ---------------------------------------------------------------------------
# NodeInfo.is_alive
# ---------------------------------------------------------------------------


def test_node_is_alive_recent_heartbeat() -> None:
    node = NodeInfo(last_heartbeat=time.time())
    assert node.is_alive() is True


def test_node_is_alive_stale_beyond_timeout() -> None:
    node = NodeInfo(last_heartbeat=time.time() - 100)
    assert node.is_alive(timeout_s=60.0) is False


def test_node_is_alive_within_extended_timeout() -> None:
    node = NodeInfo(last_heartbeat=time.time() - 100)
    assert node.is_alive(timeout_s=200.0) is True


# ---------------------------------------------------------------------------
# ClusterConfig.cluster_url_scheme
# ---------------------------------------------------------------------------


def test_cluster_url_scheme_http_without_tls() -> None:
    assert ClusterConfig().cluster_url_scheme == "http"


# ---------------------------------------------------------------------------
# OrchestratorConfig.__post_init__
# ---------------------------------------------------------------------------


def test_orchestrator_config_dict_approval_parsed_into_workflow() -> None:
    cfg = OrchestratorConfig(approval={"enabled": False, "high_risk": "pr"})  # type: ignore[arg-type]
    assert cfg.approval == "workflow"
    assert cfg.approval_workflow.enabled is False
    assert cfg.approval_workflow.high_risk == "pr"


def test_orchestrator_config_string_approval_unchanged() -> None:
    cfg = OrchestratorConfig(approval="auto")
    assert cfg.approval == "auto"


# ---------------------------------------------------------------------------
# Cost-model and plan round-trips
# ---------------------------------------------------------------------------


def test_model_cost_breakdown_round_trip() -> None:
    mcb = ModelCostBreakdown(
        model="opus",
        total_cost_usd=1.5,
        total_tokens=1000,
        invocation_count=3,
        input_tokens=600,
        output_tokens=400,
        cache_read_tokens=50,
        cache_write_tokens=25,
    )
    assert ModelCostBreakdown.from_dict(mcb.to_dict()) == mcb


def test_task_plan_round_trip_preserves_estimates_and_status() -> None:
    plan = TaskPlan(
        id="p1",
        goal="ship it",
        task_estimates=[TaskCostEstimate(task_id="t1", title="Title", role="backend", risk_reasons=["complex"])],
        total_estimated_cost_usd=1.25,
        total_estimated_minutes=45,
        high_risk_tasks=["t1"],
        status=PlanStatus.APPROVED,
        decided_at=5.0,
        decision_reason="looks fine",
    )
    restored = TaskPlan.from_dict(plan.to_dict())
    assert restored.id == "p1"
    assert restored.status is PlanStatus.APPROVED
    assert len(restored.task_estimates) == 1
    assert restored.task_estimates[0].risk_reasons == ["complex"]
    assert restored.high_risk_tasks == ["t1"]
    assert restored.decision_reason == "looks fine"


def test_run_cost_report_round_trip_with_projection() -> None:
    report = RunCostReport(
        run_id="r1",
        total_spent_usd=2.0,
        budget_usd=10.0,
        per_agent=[AgentCostSummary(agent_id="a1", total_cost_usd=2.0, task_count=1, model_breakdown={"opus": 2.0})],
        per_model=[ModelCostBreakdown(model="opus", total_cost_usd=2.0, total_tokens=100, invocation_count=1)],
        projection=RunCostProjection(
            run_id="r1",
            tasks_done=1,
            tasks_remaining=1,
            current_cost_usd=2.0,
            projected_total_usd=4.0,
            avg_cost_per_task_usd=2.0,
            budget_usd=10.0,
            within_budget=True,
            confidence=0.5,
        ),
        timestamp=9.0,
        cache_savings_usd=0.5,
    )
    restored = RunCostReport.from_dict(report.to_dict())
    assert restored.run_id == "r1"
    assert restored.total_spent_usd == pytest.approx(2.0)
    assert restored.projection is not None
    assert restored.projection.projected_total_usd == pytest.approx(4.0)
    assert restored.per_agent[0].agent_id == "a1"
    assert restored.cache_savings_usd == pytest.approx(0.5)


def test_run_cost_report_round_trip_without_projection() -> None:
    report = RunCostReport(
        run_id="r2",
        total_spent_usd=0.0,
        budget_usd=0.0,
        per_agent=[],
        per_model=[],
        projection=None,
    )
    restored = RunCostReport.from_dict(report.to_dict())
    assert restored.projection is None
    assert restored.per_agent == []


# ---------------------------------------------------------------------------
# Task.artifact_spec round-trip through Task.to_dict / from_dict (#2608)
# ---------------------------------------------------------------------------


def test_task_defaults_to_code_diff_artifact_spec() -> None:
    from bernstein.core.tasks.artifacts import ArtifactKind

    task = Task.from_dict({"id": "x", "title": "T", "description": "D", "role": "backend"})
    assert task.artifact_spec.kind is ArtifactKind.CODE_DIFF
    assert task.artifact_spec.criteria == ()


def test_task_to_dict_from_dict_round_trips_artifact_spec() -> None:
    from bernstein.core.tasks.artifacts import ArtifactCriterion, ArtifactKind, ArtifactSpec

    spec = ArtifactSpec(
        kind=ArtifactKind.DATASET,
        criteria=(ArtifactCriterion(type="hash_stable", value="sha256:abc"),),
    )
    task = Task(id="t1", title="Ttl", description="Desc", role="retrieval", artifact_spec=spec)
    restored = Task.from_dict(task.to_dict())
    assert restored.artifact_spec == spec
    assert restored.artifact_spec.kind is ArtifactKind.DATASET


def test_task_to_dict_from_dict_round_trips_core_fields() -> None:
    task = Task(
        id="t2",
        title="Title",
        description="Body",
        role="backend",
        priority=1,
        estimated_minutes=45,
        depends_on=["a", "b"],
        owned_files=["src/x.py"],
        tags=["fast"],
    )
    restored = Task.from_dict(task.to_dict())
    assert restored.id == "t2"
    assert restored.title == "Title"
    assert restored.role == "backend"
    assert restored.priority == 1
    assert restored.estimated_minutes == 45
    assert restored.depends_on == ["a", "b"]
    assert restored.owned_files == ["src/x.py"]
    assert restored.tags == ["fast"]


def test_task_to_dict_is_json_serialisable() -> None:
    import json

    from bernstein.core.tasks.artifacts import ArtifactKind, ArtifactSpec

    task = Task(
        id="t3",
        title="T",
        description="D",
        role="backend",
        completion_signals=[CompletionSignal(type="path_exists", value="README.md")],
        artifact_spec=ArtifactSpec(kind=ArtifactKind.REPORT),
    )
    # Must not raise - every value in the dict is a plain JSON type.
    payload = json.dumps(task.to_dict())
    assert '"artifact_spec"' in payload
