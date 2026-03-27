"""Tests for the pilot-to-production graduation framework."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.graduation import (
    GraduationEvaluator,
    GraduationRecord,
    GraduationStage,
    GraduationStore,
    StageMetrics,
    StagePolicy,
    _default_policies,
    stage_to_orchestrator_overrides,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_record(
    session_id: str = "test-session",
    stage: GraduationStage = GraduationStage.SANDBOX,
) -> GraduationRecord:
    record = GraduationRecord(session_id=session_id, current_stage=stage)
    record.stage_metrics[stage.value] = StageMetrics(stage=stage)
    return record


def _fill_metrics(
    record: GraduationRecord,
    *,
    completed: int = 0,
    failed: int = 0,
    consecutive_failures: int = 0,
) -> None:
    m = record.current_metrics()
    m.tasks_completed = completed
    m.tasks_failed = failed
    m.consecutive_failures = consecutive_failures


# ---------------------------------------------------------------------------
# GraduationStage ordering
# ---------------------------------------------------------------------------


class TestGraduationStageOrdering:
    def test_all_four_stages_defined(self) -> None:
        stages = {s.value for s in GraduationStage}
        assert stages == {"sandbox", "shadow", "assisted", "autonomous"}

    def test_next_stage_sandbox(self) -> None:
        assert GraduationEvaluator.next_stage(GraduationStage.SANDBOX) == GraduationStage.SHADOW

    def test_next_stage_shadow(self) -> None:
        assert GraduationEvaluator.next_stage(GraduationStage.SHADOW) == GraduationStage.ASSISTED

    def test_next_stage_assisted(self) -> None:
        assert GraduationEvaluator.next_stage(GraduationStage.ASSISTED) == GraduationStage.AUTONOMOUS

    def test_next_stage_autonomous_raises(self) -> None:
        with pytest.raises(ValueError, match="no stage after"):
            GraduationEvaluator.next_stage(GraduationStage.AUTONOMOUS)


# ---------------------------------------------------------------------------
# StageMetrics computed properties
# ---------------------------------------------------------------------------


class TestStageMetrics:
    def test_tasks_total(self) -> None:
        m = StageMetrics(stage=GraduationStage.SANDBOX, tasks_completed=3, tasks_failed=1)
        assert m.tasks_total == 4

    def test_success_rate_no_tasks(self) -> None:
        m = StageMetrics(stage=GraduationStage.SANDBOX)
        assert m.success_rate == 0.0

    def test_success_rate_all_pass(self) -> None:
        m = StageMetrics(stage=GraduationStage.SANDBOX, tasks_completed=5, tasks_failed=0)
        assert m.success_rate == 1.0

    def test_success_rate_mixed(self) -> None:
        m = StageMetrics(stage=GraduationStage.SANDBOX, tasks_completed=8, tasks_failed=2)
        assert m.success_rate == pytest.approx(0.80)

    def test_avg_duration_no_tasks(self) -> None:
        m = StageMetrics(stage=GraduationStage.SANDBOX)
        assert m.avg_duration_s == 0.0

    def test_avg_duration_computed(self) -> None:
        m = StageMetrics(
            stage=GraduationStage.SANDBOX,
            tasks_completed=2,
            tasks_failed=0,
            total_duration_s=120.0,
        )
        assert m.avg_duration_s == 60.0

    def test_roundtrip_serialisation(self) -> None:
        m = StageMetrics(
            stage=GraduationStage.SHADOW,
            tasks_completed=7,
            tasks_failed=1,
            consecutive_failures=0,
            total_cost_usd=0.42,
            total_duration_s=600.0,
        )
        restored = StageMetrics.from_dict(m.to_dict())
        assert restored.tasks_completed == 7
        assert restored.tasks_failed == 1
        assert restored.stage == GraduationStage.SHADOW
        assert restored.total_cost_usd == pytest.approx(0.42)


# ---------------------------------------------------------------------------
# GraduationRecord serialisation
# ---------------------------------------------------------------------------


class TestGraduationRecord:
    def test_roundtrip(self) -> None:
        record = _make_record("my-run")
        _fill_metrics(record, completed=3, failed=0)
        restored = GraduationRecord.from_dict(record.to_dict())
        assert restored.session_id == "my-run"
        assert restored.current_stage == GraduationStage.SANDBOX
        assert restored.stage_metrics["sandbox"].tasks_completed == 3

    def test_current_metrics_auto_creates(self) -> None:
        record = GraduationRecord(session_id="r1", current_stage=GraduationStage.SHADOW)
        m = record.current_metrics()
        assert isinstance(m, StageMetrics)
        assert m.stage == GraduationStage.SHADOW


# ---------------------------------------------------------------------------
# GraduationEvaluator.can_graduate
# ---------------------------------------------------------------------------


class TestGraduationEvaluatorCanGraduate:
    def setup_method(self) -> None:
        self.ev = GraduationEvaluator()

    def test_terminal_stage_cannot_graduate(self) -> None:
        record = _make_record(stage=GraduationStage.AUTONOMOUS)
        ok, msg = self.ev.can_graduate(record)
        assert not ok
        assert "terminal" in msg

    def test_not_enough_tasks(self) -> None:
        record = _make_record()
        _fill_metrics(record, completed=1, failed=0)
        ok, msg = self.ev.can_graduate(record)
        assert not ok
        assert "need" in msg

    def test_success_rate_too_low(self) -> None:
        record = _make_record()
        # 4 completed, 6 failed → 40% success rate (below 80%)
        _fill_metrics(record, completed=4, failed=6)
        ok, msg = self.ev.can_graduate(record)
        assert not ok
        assert "success rate" in msg

    def test_too_many_consecutive_failures(self) -> None:
        record = _make_record()
        # Meet completed + success rate but fail consecutive check
        _fill_metrics(record, completed=5, failed=1, consecutive_failures=3)
        ok, msg = self.ev.can_graduate(record)
        assert not ok
        assert "consecutive" in msg

    def test_ready_to_graduate(self) -> None:
        record = _make_record()
        _fill_metrics(record, completed=5, failed=0, consecutive_failures=0)
        ok, msg = self.ev.can_graduate(record)
        assert ok
        assert "shadow" in msg

    def test_custom_policy(self) -> None:
        policies = {
            GraduationStage.SANDBOX.value: StagePolicy(
                stage=GraduationStage.SANDBOX,
                min_tasks_completed=1,
                min_success_rate=0.5,
                max_consecutive_failures=10,
            )
        }
        ev = GraduationEvaluator(policies=policies)
        record = _make_record()
        _fill_metrics(record, completed=1, failed=1)
        ok, _ = ev.can_graduate(record)
        assert ok


# ---------------------------------------------------------------------------
# GraduationEvaluator.promote
# ---------------------------------------------------------------------------


class TestGraduationEvaluatorPromote:
    def test_promote_sandbox_to_shadow(self) -> None:
        ev = GraduationEvaluator()
        record = _make_record()
        ev.promote(record, reason="test", promoted_by="ci")
        assert record.current_stage == GraduationStage.SHADOW
        assert len(record.promotion_log) == 1
        entry = record.promotion_log[0]
        assert entry["from_stage"] == "sandbox"
        assert entry["to_stage"] == "shadow"
        assert entry["reason"] == "test"
        assert entry["promoted_by"] == "ci"

    def test_promote_creates_new_stage_metrics(self) -> None:
        ev = GraduationEvaluator()
        record = _make_record()
        assert "shadow" not in record.stage_metrics
        ev.promote(record)
        assert "shadow" in record.stage_metrics

    def test_promote_terminal_raises(self) -> None:
        ev = GraduationEvaluator()
        record = _make_record(stage=GraduationStage.AUTONOMOUS)
        with pytest.raises(ValueError):
            ev.promote(record)

    def test_full_promotion_chain(self) -> None:
        ev = GraduationEvaluator()
        record = _make_record()
        ev.promote(record)  # → shadow
        ev.promote(record)  # → assisted
        ev.promote(record)  # → autonomous
        assert record.current_stage == GraduationStage.AUTONOMOUS
        assert len(record.promotion_log) == 3


# ---------------------------------------------------------------------------
# GraduationStore
# ---------------------------------------------------------------------------


class TestGraduationStore:
    def test_get_or_create_new(self, tmp_path: Path) -> None:
        store = GraduationStore(tmp_path)
        record = store.get_or_create("run-1")
        assert record.session_id == "run-1"
        assert record.current_stage == GraduationStage.SANDBOX

    def test_get_or_create_returns_existing(self, tmp_path: Path) -> None:
        store = GraduationStore(tmp_path)
        r1 = store.get_or_create("run-1")
        r1.current_stage = GraduationStage.SHADOW
        store.save(r1)
        r2 = store.get_or_create("run-1")
        assert r2.current_stage == GraduationStage.SHADOW

    def test_list_all_empty(self, tmp_path: Path) -> None:
        store = GraduationStore(tmp_path)
        assert store.list_all() == []

    def test_list_all_returns_all(self, tmp_path: Path) -> None:
        store = GraduationStore(tmp_path)
        store.get_or_create("run-a")
        store.get_or_create("run-b")
        records = store.list_all()
        assert len(records) == 2
        ids = {r.session_id for r in records}
        assert ids == {"run-a", "run-b"}

    def test_record_task_event_success(self, tmp_path: Path) -> None:
        store = GraduationStore(tmp_path)
        record = store.record_task_event("run-1", success=True, task_id="t1", duration_s=10.0)
        assert record.current_metrics().tasks_completed == 1
        assert record.current_metrics().consecutive_failures == 0

    def test_record_task_event_failure(self, tmp_path: Path) -> None:
        store = GraduationStore(tmp_path)
        record = store.record_task_event("run-1", success=False, task_id="t1")
        assert record.current_metrics().tasks_failed == 1
        assert record.current_metrics().consecutive_failures == 1

    def test_record_task_event_resets_consecutive_on_success(self, tmp_path: Path) -> None:
        store = GraduationStore(tmp_path)
        store.record_task_event("run-1", success=False, task_id="t1")
        store.record_task_event("run-1", success=False, task_id="t2")
        record = store.record_task_event("run-1", success=True, task_id="t3")
        assert record.current_metrics().consecutive_failures == 0
        assert record.current_metrics().tasks_completed == 1

    def test_metrics_appended_to_jsonl(self, tmp_path: Path) -> None:
        store = GraduationStore(tmp_path)
        store.record_task_event("run-1", success=True, task_id="t1", cost_usd=0.10)
        store.record_task_event("run-1", success=False, task_id="t2")
        metrics_file = tmp_path / "metrics" / "graduation.jsonl"
        assert metrics_file.exists()
        lines = [json.loads(ln) for ln in metrics_file.read_text().splitlines() if ln.strip()]
        assert len(lines) == 2
        assert lines[0]["task_id"] == "t1"
        assert lines[0]["success"] is True
        assert lines[1]["success"] is False

    def test_record_promotion_writes_to_jsonl(self, tmp_path: Path) -> None:
        store = GraduationStore(tmp_path)
        record = store.get_or_create("run-1")
        ev = GraduationEvaluator()
        ev.promote(record, reason="manual", promoted_by="alice")
        store.save(record)
        store.record_promotion(record)
        metrics_file = tmp_path / "metrics" / "graduation.jsonl"
        lines = [json.loads(ln) for ln in metrics_file.read_text().splitlines() if ln.strip()]
        promotion = next((l for l in lines if l.get("type") == "promotion"), None)
        assert promotion is not None
        assert promotion["from_stage"] == "sandbox"
        assert promotion["to_stage"] == "shadow"


# ---------------------------------------------------------------------------
# stage_to_orchestrator_overrides
# ---------------------------------------------------------------------------


class TestStageToOrchestratorOverrides:
    def test_sandbox_is_dry_run(self) -> None:
        overrides = stage_to_orchestrator_overrides(GraduationStage.SANDBOX)
        assert overrides["dry_run"] is True
        assert overrides["merge_strategy"] == "none"

    def test_shadow_no_commit(self) -> None:
        overrides = stage_to_orchestrator_overrides(GraduationStage.SHADOW)
        assert overrides["dry_run"] is False
        assert overrides["merge_strategy"] == "none"

    def test_assisted_review(self) -> None:
        overrides = stage_to_orchestrator_overrides(GraduationStage.ASSISTED)
        assert overrides["approval"] == "review"
        assert overrides["merge_strategy"] == "pr"

    def test_autonomous_auto(self) -> None:
        overrides = stage_to_orchestrator_overrides(GraduationStage.AUTONOMOUS)
        assert overrides["approval"] == "auto"
        assert overrides["merge_strategy"] == "pr"


# ---------------------------------------------------------------------------
# Default policies
# ---------------------------------------------------------------------------


class TestDefaultPolicies:
    def test_all_three_inbound_stages_have_policies(self) -> None:
        policies = _default_policies()
        assert GraduationStage.SANDBOX.value in policies
        assert GraduationStage.SHADOW.value in policies
        assert GraduationStage.ASSISTED.value in policies

    def test_autonomous_has_no_policy(self) -> None:
        # Terminal stage — no outbound policy needed.
        policies = _default_policies()
        assert GraduationStage.AUTONOMOUS.value not in policies

    def test_assisted_requires_more_tasks_than_shadow(self) -> None:
        policies = _default_policies()
        assert (
            policies[GraduationStage.ASSISTED.value].min_tasks_completed
            > policies[GraduationStage.SHADOW.value].min_tasks_completed
        )

    def test_success_rate_escalates_through_stages(self) -> None:
        p = _default_policies()
        assert (
            p[GraduationStage.SANDBOX.value].min_success_rate
            <= p[GraduationStage.SHADOW.value].min_success_rate
            <= p[GraduationStage.ASSISTED.value].min_success_rate
        )
