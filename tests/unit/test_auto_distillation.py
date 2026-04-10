"""Tests for AutoDistiller — auto-distillation pipeline.

Covers:
- DistillationExample: serialisation, distillation key
- DistillationConfig: defaults and overrides
- AutoDistiller: example collection, batch preparation, model registration,
  routing, quality gating, persistence, and statistics
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from bernstein.core.models import Task, TaskType

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _task(
    task_id: str = "t1",
    role: str = "backend",
    task_type: TaskType = TaskType.STANDARD,
    title: str = "Implement feature X",
    description: str = "Add the X feature to the backend module.",
    result_summary: str = "Implemented feature X with tests.",
    model: str | None = None,
    owned_files: list[str] | None = None,
) -> Task:
    return Task(
        id=task_id,
        title=title,
        description=description,
        role=role,
        task_type=task_type,
        result_summary=result_summary,
        model=model,
        owned_files=owned_files or ["src/foo.py"],
    )


# ---------------------------------------------------------------------------
# DistillationExample
# ---------------------------------------------------------------------------


class TestDistillationExample:
    def test_distillation_key(self) -> None:
        from bernstein.core.auto_distillation import DistillationExample

        ex = DistillationExample(
            example_id="abc",
            task_id="t1",
            role="backend",
            task_type="standard",
            writer_model="sonnet",
            task_title="Fix bug",
            task_description="Fix the bug",
            result_summary="Fixed",
            owned_files=[],
            cost_usd=0.01,
            duration_seconds=30.0,
            quality_score=1.0,
            timestamp=1000.0,
        )
        assert ex.distillation_key() == "backend:standard"

    def test_round_trip_serialisation(self) -> None:
        from bernstein.core.auto_distillation import DistillationExample

        ex = DistillationExample(
            example_id="abc123",
            task_id="t42",
            role="frontend",
            task_type="upgrade",
            writer_model="opus",
            task_title="Upgrade React",
            task_description="Upgrade from v17 to v18",
            result_summary="Upgraded successfully",
            owned_files=["src/App.tsx"],
            cost_usd=0.05,
            duration_seconds=120.0,
            quality_score=0.95,
            timestamp=2000.0,
        )
        d = ex.to_dict()
        restored = DistillationExample.from_dict(d)
        assert restored.example_id == ex.example_id
        assert restored.role == ex.role
        assert restored.task_type == ex.task_type
        assert restored.distillation_key() == "frontend:upgrade"
        assert restored.cost_usd == pytest.approx(0.05)


# ---------------------------------------------------------------------------
# DistillationBatch
# ---------------------------------------------------------------------------


class TestDistillationBatch:
    def test_round_trip(self) -> None:
        from bernstein.core.auto_distillation import DistillationBatch, TrainingJobStatus

        batch = DistillationBatch(
            batch_id="b1",
            distillation_key="backend:standard",
            example_count=20,
            example_ids=["e1", "e2"],
            status=TrainingJobStatus.PREPARING,
            provider="openai",
            target_model="gpt-5.4-mini",
            created_at=1000.0,
        )
        d = batch.to_dict()
        assert d["status"] == "preparing"
        restored = DistillationBatch.from_dict(d)
        assert restored.batch_id == "b1"
        assert restored.status == TrainingJobStatus.PREPARING
        assert restored.example_count == 20


# ---------------------------------------------------------------------------
# DistilledModel
# ---------------------------------------------------------------------------


class TestDistilledModel:
    def test_success_rate_zero_routed(self) -> None:
        from bernstein.core.auto_distillation import DistilledModel

        m = DistilledModel(
            model_name="ft:gpt-5.4-mini:backend",
            distillation_key="backend:standard",
            base_model="gpt-5.4-mini",
            batch_id="b1",
            registered_at=1000.0,
        )
        assert m.success_rate == pytest.approx(0.0)

    def test_success_rate_with_data(self) -> None:
        from bernstein.core.auto_distillation import DistilledModel

        m = DistilledModel(
            model_name="ft:gpt-5.4-mini:backend",
            distillation_key="backend:standard",
            base_model="gpt-5.4-mini",
            batch_id="b1",
            registered_at=1000.0,
            tasks_routed=10,
            tasks_succeeded=8,
        )
        assert m.success_rate == pytest.approx(0.8)

    def test_round_trip(self) -> None:
        from bernstein.core.auto_distillation import DistilledModel

        m = DistilledModel(
            model_name="ft:model",
            distillation_key="qa:standard",
            base_model="gpt-5.4-mini",
            batch_id="b2",
            registered_at=999.0,
            tasks_routed=5,
            tasks_succeeded=4,
            avg_cost_usd=0.002,
            active=True,
        )
        d = m.to_dict()
        restored = DistilledModel.from_dict(d)
        assert restored.model_name == "ft:model"
        assert restored.success_rate == pytest.approx(0.8)
        assert restored.active is True


# ---------------------------------------------------------------------------
# DistillationConfig
# ---------------------------------------------------------------------------


class TestDistillationConfig:
    def test_defaults(self) -> None:
        from bernstein.core.auto_distillation import DistillationConfig

        cfg = DistillationConfig()
        assert cfg.enabled is False
        assert cfg.batch_threshold == 20
        assert cfg.quality_threshold == pytest.approx(0.8)

    def test_override(self) -> None:
        from bernstein.core.auto_distillation import DistillationConfig

        cfg = DistillationConfig(enabled=True, batch_threshold=10)
        assert cfg.enabled is True
        assert cfg.batch_threshold == 10


# ---------------------------------------------------------------------------
# AutoDistiller — collection
# ---------------------------------------------------------------------------


class TestAutoDistillerCollection:
    def test_disabled_returns_none(self) -> None:
        from bernstein.core.auto_distillation import AutoDistiller, DistillationConfig

        distiller = AutoDistiller(DistillationConfig(enabled=False))
        result = distiller.collect_example(
            _task(),
            writer_model="sonnet",
            cost_usd=0.01,
            duration_seconds=30.0,
            quality_score=1.0,
        )
        assert result is None

    def test_low_quality_skipped(self) -> None:
        from bernstein.core.auto_distillation import AutoDistiller, DistillationConfig

        distiller = AutoDistiller(DistillationConfig(enabled=True, quality_threshold=0.8))
        result = distiller.collect_example(
            _task(),
            writer_model="sonnet",
            cost_usd=0.01,
            duration_seconds=30.0,
            quality_score=0.5,
        )
        assert result is None

    def test_no_result_summary_skipped(self) -> None:
        from bernstein.core.auto_distillation import AutoDistiller, DistillationConfig

        task = _task(result_summary="")
        # Task.result_summary is "" which is falsy
        task.result_summary = None  # type: ignore[assignment]
        distiller = AutoDistiller(DistillationConfig(enabled=True))
        result = distiller.collect_example(
            task,
            writer_model="sonnet",
            cost_usd=0.01,
            duration_seconds=30.0,
            quality_score=1.0,
        )
        assert result is None

    def test_successful_collection(self) -> None:
        from bernstein.core.auto_distillation import AutoDistiller, DistillationConfig

        distiller = AutoDistiller(DistillationConfig(enabled=True))
        result = distiller.collect_example(
            _task(),
            writer_model="sonnet",
            cost_usd=0.05,
            duration_seconds=60.0,
            quality_score=1.0,
        )
        assert result is not None
        assert result.role == "backend"
        assert result.task_type == "standard"
        assert result.writer_model == "sonnet"
        assert result.cost_usd == pytest.approx(0.05)

    def test_collection_increments_counts(self) -> None:
        from bernstein.core.auto_distillation import AutoDistiller, DistillationConfig

        distiller = AutoDistiller(DistillationConfig(enabled=True))
        for i in range(5):
            distiller.collect_example(
                _task(task_id=f"t{i}"),
                writer_model="sonnet",
                cost_usd=0.01,
                duration_seconds=30.0,
                quality_score=1.0,
            )
        s = distiller.stats()
        assert s.total_examples == 5
        assert s.examples_per_key["backend:standard"] == 5


# ---------------------------------------------------------------------------
# AutoDistiller — batch management
# ---------------------------------------------------------------------------


class TestAutoDistillerBatch:
    def test_should_trigger_false_below_threshold(self) -> None:
        from bernstein.core.auto_distillation import AutoDistiller, DistillationConfig

        distiller = AutoDistiller(DistillationConfig(enabled=True, batch_threshold=10))
        for i in range(5):
            distiller.collect_example(
                _task(task_id=f"t{i}"),
                writer_model="sonnet",
                cost_usd=0.01,
                duration_seconds=30.0,
                quality_score=1.0,
            )
        assert distiller.should_trigger_batch("backend:standard") is False

    def test_should_trigger_true_at_threshold(self) -> None:
        from bernstein.core.auto_distillation import AutoDistiller, DistillationConfig

        distiller = AutoDistiller(DistillationConfig(enabled=True, batch_threshold=5))
        for i in range(5):
            distiller.collect_example(
                _task(task_id=f"t{i}"),
                writer_model="sonnet",
                cost_usd=0.01,
                duration_seconds=30.0,
                quality_score=1.0,
            )
        assert distiller.should_trigger_batch("backend:standard") is True

    def test_prepare_batch_without_dir_returns_none(self) -> None:
        """Without a distill_dir, examples aren't persisted so batch can't load them."""
        from bernstein.core.auto_distillation import AutoDistiller, DistillationConfig

        distiller = AutoDistiller(DistillationConfig(enabled=True, batch_threshold=3))
        for i in range(3):
            distiller.collect_example(
                _task(task_id=f"t{i}"),
                writer_model="sonnet",
                cost_usd=0.01,
                duration_seconds=30.0,
                quality_score=1.0,
            )
        # No distill_dir → _load_examples_for_key returns []
        batch = distiller.prepare_batch("backend:standard")
        assert batch is None

    def test_prepare_batch_with_dir(self, tmp_path: Path) -> None:
        from bernstein.core.auto_distillation import AutoDistiller, DistillationConfig

        distill_dir = tmp_path / "distillation"
        distiller = AutoDistiller(
            DistillationConfig(enabled=True, batch_threshold=3),
            distill_dir=distill_dir,
        )
        for i in range(3):
            distiller.collect_example(
                _task(task_id=f"t{i}"),
                writer_model="sonnet",
                cost_usd=0.01,
                duration_seconds=30.0,
                quality_score=1.0,
            )
        batch = distiller.prepare_batch("backend:standard")
        assert batch is not None
        assert batch.example_count == 3
        assert batch.distillation_key == "backend:standard"
        assert len(batch.example_ids) == 3

    def test_prepare_batch_resets_counter(self, tmp_path: Path) -> None:
        from bernstein.core.auto_distillation import AutoDistiller, DistillationConfig

        distill_dir = tmp_path / "distillation"
        distiller = AutoDistiller(
            DistillationConfig(enabled=True, batch_threshold=3),
            distill_dir=distill_dir,
        )
        for i in range(3):
            distiller.collect_example(
                _task(task_id=f"t{i}"),
                writer_model="sonnet",
                cost_usd=0.01,
                duration_seconds=30.0,
                quality_score=1.0,
            )
        distiller.prepare_batch("backend:standard")
        # Counter should be reset
        assert distiller.should_trigger_batch("backend:standard") is False
        s = distiller.stats()
        assert s.examples_per_key["backend:standard"] == 0

    def test_format_training_data(self, tmp_path: Path) -> None:
        from bernstein.core.auto_distillation import AutoDistiller, DistillationConfig

        distill_dir = tmp_path / "distillation"
        distiller = AutoDistiller(
            DistillationConfig(enabled=True, batch_threshold=2),
            distill_dir=distill_dir,
        )
        for i in range(2):
            distiller.collect_example(
                _task(task_id=f"t{i}"),
                writer_model="sonnet",
                cost_usd=0.01,
                duration_seconds=30.0,
                quality_score=1.0,
            )
        batch = distiller.prepare_batch("backend:standard")
        assert batch is not None

        records = distiller.format_training_data(batch)
        assert len(records) == 2
        for record in records:
            messages = record["messages"]
            assert len(messages) == 3
            assert messages[0]["role"] == "system"
            assert messages[1]["role"] == "user"
            assert messages[2]["role"] == "assistant"
            assert "backend" in messages[0]["content"]
            assert "Implement feature X" in messages[1]["content"]


# ---------------------------------------------------------------------------
# AutoDistiller — model lifecycle
# ---------------------------------------------------------------------------


class TestAutoDistillerModelLifecycle:
    def test_mark_submitted_and_completed(self, tmp_path: Path) -> None:
        from bernstein.core.auto_distillation import AutoDistiller, DistillationConfig, TrainingJobStatus

        distill_dir = tmp_path / "distillation"
        distiller = AutoDistiller(
            DistillationConfig(enabled=True, batch_threshold=2),
            distill_dir=distill_dir,
        )
        for i in range(2):
            distiller.collect_example(
                _task(task_id=f"t{i}"),
                writer_model="sonnet",
                cost_usd=0.01,
                duration_seconds=30.0,
                quality_score=1.0,
            )
        batch = distiller.prepare_batch("backend:standard")
        assert batch is not None

        distiller.mark_batch_submitted(batch.batch_id, "job-123")
        assert distiller._batches[batch.batch_id].status == TrainingJobStatus.SUBMITTED
        assert distiller._batches[batch.batch_id].training_job_id == "job-123"

        model = distiller.mark_batch_completed(batch.batch_id, "ft:gpt-5.4-mini:backend")
        assert model is not None
        assert model.model_name == "ft:gpt-5.4-mini:backend"
        assert model.active is True
        assert distiller._batches[batch.batch_id].status == TrainingJobStatus.COMPLETED

    def test_mark_batch_failed(self, tmp_path: Path) -> None:
        from bernstein.core.auto_distillation import AutoDistiller, DistillationConfig, TrainingJobStatus

        distill_dir = tmp_path / "distillation"
        distiller = AutoDistiller(
            DistillationConfig(enabled=True, batch_threshold=2),
            distill_dir=distill_dir,
        )
        for i in range(2):
            distiller.collect_example(
                _task(task_id=f"t{i}"),
                writer_model="sonnet",
                cost_usd=0.01,
                duration_seconds=30.0,
                quality_score=1.0,
            )
        batch = distiller.prepare_batch("backend:standard")
        assert batch is not None

        distiller.mark_batch_failed(batch.batch_id, "Out of quota")
        assert distiller._batches[batch.batch_id].status == TrainingJobStatus.FAILED
        assert distiller._batches[batch.batch_id].error == "Out of quota"


# ---------------------------------------------------------------------------
# AutoDistiller — routing
# ---------------------------------------------------------------------------


class TestAutoDistillerRouting:
    def test_get_distilled_model_none_when_disabled(self) -> None:
        from bernstein.core.auto_distillation import AutoDistiller, DistillationConfig

        distiller = AutoDistiller(DistillationConfig(enabled=False))
        assert distiller.get_distilled_model("backend", "standard") is None

    def test_get_distilled_model_none_when_no_model(self) -> None:
        from bernstein.core.auto_distillation import AutoDistiller, DistillationConfig

        distiller = AutoDistiller(DistillationConfig(enabled=True))
        assert distiller.get_distilled_model("backend", "standard") is None

    def test_get_distilled_model_returns_active_model(self, tmp_path: Path) -> None:
        from bernstein.core.auto_distillation import AutoDistiller, DistillationConfig

        distill_dir = tmp_path / "distillation"
        distiller = AutoDistiller(
            DistillationConfig(enabled=True, batch_threshold=2),
            distill_dir=distill_dir,
        )
        # Collect examples and create batch
        for i in range(2):
            distiller.collect_example(
                _task(task_id=f"t{i}"),
                writer_model="sonnet",
                cost_usd=0.01,
                duration_seconds=30.0,
                quality_score=1.0,
            )
        batch = distiller.prepare_batch("backend:standard")
        assert batch is not None
        distiller.mark_batch_submitted(batch.batch_id, "job-1")
        distiller.mark_batch_completed(batch.batch_id, "ft:gpt-5.4-mini:backend")

        result = distiller.get_distilled_model("backend", "standard")
        assert result == "ft:gpt-5.4-mini:backend"

    def test_get_distilled_model_deactivates_low_success(self, tmp_path: Path) -> None:
        from bernstein.core.auto_distillation import AutoDistiller, DistillationConfig

        distill_dir = tmp_path / "distillation"
        distiller = AutoDistiller(
            DistillationConfig(enabled=True, batch_threshold=2),
            distill_dir=distill_dir,
        )
        for i in range(2):
            distiller.collect_example(
                _task(task_id=f"t{i}"),
                writer_model="sonnet",
                cost_usd=0.01,
                duration_seconds=30.0,
                quality_score=1.0,
            )
        batch = distiller.prepare_batch("backend:standard")
        assert batch is not None
        distiller.mark_batch_submitted(batch.batch_id, "job-1")
        distiller.mark_batch_completed(batch.batch_id, "ft:model")

        # Simulate 10 tasks with only 5 successes (50% < 60% threshold)
        for i in range(10):
            distiller.record_distilled_outcome("backend", "standard", success=i < 5, cost_usd=0.001)

        # Should deactivate and return None
        result = distiller.get_distilled_model("backend", "standard")
        assert result is None

    def test_record_distilled_outcome_updates_stats(self, tmp_path: Path) -> None:
        from bernstein.core.auto_distillation import AutoDistiller, DistillationConfig

        distill_dir = tmp_path / "distillation"
        distiller = AutoDistiller(
            DistillationConfig(enabled=True, batch_threshold=2),
            distill_dir=distill_dir,
        )
        for i in range(2):
            distiller.collect_example(
                _task(task_id=f"t{i}"),
                writer_model="sonnet",
                cost_usd=0.01,
                duration_seconds=30.0,
                quality_score=1.0,
            )
        batch = distiller.prepare_batch("backend:standard")
        assert batch is not None
        distiller.mark_batch_submitted(batch.batch_id, "job-1")
        distiller.mark_batch_completed(batch.batch_id, "ft:model")

        distiller.record_distilled_outcome("backend", "standard", success=True, cost_usd=0.002)
        distiller.record_distilled_outcome("backend", "standard", success=True, cost_usd=0.003)
        distiller.record_distilled_outcome("backend", "standard", success=False, cost_usd=0.001)

        model = distiller._models["backend:standard"]
        assert model.tasks_routed == 3
        assert model.tasks_succeeded == 2
        assert model.success_rate == pytest.approx(2 / 3)

    def test_auto_route_disabled_returns_none(self, tmp_path: Path) -> None:
        from bernstein.core.auto_distillation import AutoDistiller, DistillationConfig

        distill_dir = tmp_path / "distillation"
        distiller = AutoDistiller(
            DistillationConfig(enabled=True, auto_route=False, batch_threshold=2),
            distill_dir=distill_dir,
        )
        for i in range(2):
            distiller.collect_example(
                _task(task_id=f"t{i}"),
                writer_model="sonnet",
                cost_usd=0.01,
                duration_seconds=30.0,
                quality_score=1.0,
            )
        batch = distiller.prepare_batch("backend:standard")
        assert batch is not None
        distiller.mark_batch_submitted(batch.batch_id, "job-1")
        distiller.mark_batch_completed(batch.batch_id, "ft:model")

        assert distiller.get_distilled_model("backend", "standard") is None


# ---------------------------------------------------------------------------
# AutoDistiller — persistence
# ---------------------------------------------------------------------------


class TestAutoDistillerPersistence:
    def test_examples_persisted_to_jsonl(self, tmp_path: Path) -> None:
        from bernstein.core.auto_distillation import AutoDistiller, DistillationConfig

        distill_dir = tmp_path / "distillation"
        distiller = AutoDistiller(
            DistillationConfig(enabled=True),
            distill_dir=distill_dir,
        )
        distiller.collect_example(
            _task(),
            writer_model="sonnet",
            cost_usd=0.01,
            duration_seconds=30.0,
            quality_score=1.0,
        )

        jsonl_path = distill_dir / "examples.jsonl"
        assert jsonl_path.exists()
        lines = jsonl_path.read_text().strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["role"] == "backend"
        assert record["task_type"] == "standard"

    def test_state_persisted_to_json(self, tmp_path: Path) -> None:
        from bernstein.core.auto_distillation import AutoDistiller, DistillationConfig

        distill_dir = tmp_path / "distillation"
        distiller = AutoDistiller(
            DistillationConfig(enabled=True),
            distill_dir=distill_dir,
        )
        distiller.collect_example(
            _task(),
            writer_model="sonnet",
            cost_usd=0.01,
            duration_seconds=30.0,
            quality_score=1.0,
        )

        state_path = distill_dir / "state.json"
        assert state_path.exists()
        state = json.loads(state_path.read_text())
        assert state["total_examples"] == 1
        assert state["examples_per_key"]["backend:standard"] == 1

    def test_state_reloaded_on_new_instance(self, tmp_path: Path) -> None:
        from bernstein.core.auto_distillation import AutoDistiller, DistillationConfig

        distill_dir = tmp_path / "distillation"
        cfg = DistillationConfig(enabled=True, batch_threshold=2)

        # First instance: collect examples and create batch + model
        d1 = AutoDistiller(cfg, distill_dir=distill_dir)
        for i in range(2):
            d1.collect_example(
                _task(task_id=f"t{i}"),
                writer_model="sonnet",
                cost_usd=0.01,
                duration_seconds=30.0,
                quality_score=1.0,
            )
        batch = d1.prepare_batch("backend:standard")
        assert batch is not None
        d1.mark_batch_submitted(batch.batch_id, "job-42")
        d1.mark_batch_completed(batch.batch_id, "ft:reloaded-model")

        # Second instance: should reload state
        d2 = AutoDistiller(cfg, distill_dir=distill_dir)
        model = d2.get_distilled_model("backend", "standard")
        assert model == "ft:reloaded-model"
        s = d2.stats()
        assert s.completed_batches == 1
        assert s.active_models == 1


# ---------------------------------------------------------------------------
# AutoDistiller — statistics
# ---------------------------------------------------------------------------


class TestAutoDistillerStats:
    def test_empty_stats(self) -> None:
        from bernstein.core.auto_distillation import AutoDistiller, DistillationConfig

        distiller = AutoDistiller(DistillationConfig(enabled=True))
        s = distiller.stats()
        assert s.total_examples == 0
        assert s.active_batches == 0
        assert s.active_models == 0

    def test_summary_dict(self, tmp_path: Path) -> None:
        from bernstein.core.auto_distillation import AutoDistiller, DistillationConfig

        distill_dir = tmp_path / "distillation"
        distiller = AutoDistiller(
            DistillationConfig(enabled=True, batch_threshold=2),
            distill_dir=distill_dir,
        )
        for i in range(2):
            distiller.collect_example(
                _task(task_id=f"t{i}"),
                writer_model="sonnet",
                cost_usd=0.01,
                duration_seconds=30.0,
                quality_score=1.0,
            )
        batch = distiller.prepare_batch("backend:standard")
        assert batch is not None
        distiller.mark_batch_submitted(batch.batch_id, "job-1")
        distiller.mark_batch_completed(batch.batch_id, "ft:model")
        distiller.record_distilled_outcome("backend", "standard", success=True, cost_usd=0.002)

        summary = distiller.summary()
        assert summary["enabled"] is True
        assert summary["total_examples"] == 2
        assert summary["completed_batches"] == 1
        assert summary["active_models"] == 1
        assert len(summary["models"]) == 1
        assert summary["models"][0]["model_name"] == "ft:model"

    def test_model_replacement_deactivates_old(self, tmp_path: Path) -> None:
        from bernstein.core.auto_distillation import AutoDistiller, DistillationConfig

        distill_dir = tmp_path / "distillation"
        distiller = AutoDistiller(
            DistillationConfig(enabled=True, batch_threshold=2),
            distill_dir=distill_dir,
        )

        # First batch
        for i in range(2):
            distiller.collect_example(
                _task(task_id=f"t{i}"),
                writer_model="sonnet",
                cost_usd=0.01,
                duration_seconds=30.0,
                quality_score=1.0,
            )
        b1 = distiller.prepare_batch("backend:standard")
        assert b1 is not None
        distiller.mark_batch_submitted(b1.batch_id, "job-1")
        distiller.mark_batch_completed(b1.batch_id, "ft:model-v1")

        # Second batch (collect more examples)
        for i in range(2):
            distiller.collect_example(
                _task(task_id=f"t{i + 10}"),
                writer_model="sonnet",
                cost_usd=0.01,
                duration_seconds=30.0,
                quality_score=1.0,
            )
        b2 = distiller.prepare_batch("backend:standard")
        assert b2 is not None
        distiller.mark_batch_submitted(b2.batch_id, "job-2")
        distiller.mark_batch_completed(b2.batch_id, "ft:model-v2")

        # Only the new model should be active
        result = distiller.get_distilled_model("backend", "standard")
        assert result == "ft:model-v2"
        s = distiller.stats()
        assert s.active_models == 1


# ---------------------------------------------------------------------------
# AutoDistiller — multiple keys
# ---------------------------------------------------------------------------


class TestAutoDistillerMultipleKeys:
    def test_separate_keys_tracked_independently(self, tmp_path: Path) -> None:
        from bernstein.core.auto_distillation import AutoDistiller, DistillationConfig

        distill_dir = tmp_path / "distillation"
        distiller = AutoDistiller(
            DistillationConfig(enabled=True, batch_threshold=3),
            distill_dir=distill_dir,
        )

        # Collect backend examples
        for i in range(3):
            distiller.collect_example(
                _task(task_id=f"be-{i}", role="backend"),
                writer_model="sonnet",
                cost_usd=0.01,
                duration_seconds=30.0,
                quality_score=1.0,
            )

        # Collect frontend examples (only 2, not enough)
        for i in range(2):
            distiller.collect_example(
                _task(task_id=f"fe-{i}", role="frontend"),
                writer_model="sonnet",
                cost_usd=0.01,
                duration_seconds=30.0,
                quality_score=1.0,
            )

        assert distiller.should_trigger_batch("backend:standard") is True
        assert distiller.should_trigger_batch("frontend:standard") is False

        s = distiller.stats()
        assert s.examples_per_key["backend:standard"] == 3
        assert s.examples_per_key["frontend:standard"] == 2
