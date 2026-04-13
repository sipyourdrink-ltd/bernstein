"""Tests for ML-predicted task duration — feature extraction, training, inference."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from bernstein.core.duration_predictor import (
    MIN_TRAIN_SAMPLES,
    DurationEstimate,
    DurationPredictor,
    TaskFeatureExtractor,
    TrainingRecord,
    get_predictor,
    reset_predictor,
)
from bernstein.core.models import Complexity, Scope, Task, TaskType

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(
    *,
    role: str = "backend",
    complexity: Complexity = Complexity.MEDIUM,
    scope: Scope = Scope.MEDIUM,
    task_type: TaskType = TaskType.STANDARD,
    priority: int = 2,
    estimated_minutes: int = 30,
    description: str = "Some task description",
    owned_files: list[str] | None = None,
    depends_on: list[str] | None = None,
    model: str | None = None,
) -> Task:
    return Task(
        id="test-task-id",
        title="Test Task",
        description=description,
        role=role,
        priority=priority,
        scope=scope,
        complexity=complexity,
        task_type=task_type,
        estimated_minutes=estimated_minutes,
        owned_files=owned_files or [],
        depends_on=depends_on or [],
        model=model,
    )


def _make_training_jsonl(path: Path, n: int, duration: float = 120.0) -> None:
    """Write n training records to a JSONL file."""
    with path.open("w") as fh:
        for i in range(n):
            fh.write(
                json.dumps(
                    {
                        "task_id": f"t{i}",
                        "role": "backend",
                        "complexity": "medium",
                        "scope": "medium",
                        "task_type": "standard",
                        "priority": 2,
                        "estimated_minutes": 30,
                        "description_length": 100,
                        "owned_files_count": 2,
                        "depends_on_count": 0,
                        "model": "sonnet",
                        "actual_duration_seconds": duration + (i % 10) * 10,
                        "timestamp": time.time(),
                    }
                )
                + "\n"
            )


# ---------------------------------------------------------------------------
# TaskFeatureExtractor
# ---------------------------------------------------------------------------


class TestTaskFeatureExtractor:
    def setup_method(self) -> None:
        self.extractor = TaskFeatureExtractor()

    def test_extract_returns_9_features(self) -> None:
        task = _make_task()
        features = self.extractor.extract(task)
        assert len(features) == 9

    def test_complexity_mapping(self) -> None:
        low = self.extractor.extract(_make_task(complexity=Complexity.LOW))
        med = self.extractor.extract(_make_task(complexity=Complexity.MEDIUM))
        high = self.extractor.extract(_make_task(complexity=Complexity.HIGH))
        assert low[0] < med[0] < high[0]

    def test_scope_mapping(self) -> None:
        small = self.extractor.extract(_make_task(scope=Scope.SMALL))
        med = self.extractor.extract(_make_task(scope=Scope.MEDIUM))
        large = self.extractor.extract(_make_task(scope=Scope.LARGE))
        assert small[1] < med[1] < large[1]

    def test_model_tier_haiku(self) -> None:
        features = self.extractor.extract(_make_task(model="haiku"))
        assert features[8] == pytest.approx(0.0)

    def test_model_tier_sonnet(self) -> None:
        features = self.extractor.extract(_make_task(model="sonnet"))
        assert features[8] == pytest.approx(1.0)

    def test_model_tier_opus(self) -> None:
        features = self.extractor.extract(_make_task(model="opus"))
        assert features[8] == pytest.approx(2.0)

    def test_model_tier_fast_path(self) -> None:
        features = self.extractor.extract(_make_task(model="fast-path"))
        assert features[8] == pytest.approx(0.0)

    def test_description_length_in_features(self) -> None:
        short = self.extractor.extract(_make_task(description="hi"))
        long = self.extractor.extract(_make_task(description="x" * 500))
        assert long[5] > short[5]

    def test_owned_files_count_in_features(self) -> None:
        few = self.extractor.extract(_make_task(owned_files=[]))
        many = self.extractor.extract(_make_task(owned_files=["a.py", "b.py", "c.py"]))
        assert many[6] > few[6]

    def test_extract_from_record(self) -> None:
        rec = TrainingRecord(
            task_id="t1",
            role="backend",
            complexity="high",
            scope="large",
            task_type="fix",
            priority=1,
            estimated_minutes=60,
            description_length=200,
            owned_files_count=5,
            depends_on_count=3,
            model="opus",
            actual_duration_seconds=3600.0,
            timestamp=time.time(),
        )
        features = self.extractor.extract_from_record(rec)
        assert len(features) == 9
        assert features[0] == pytest.approx(2.0)  # high complexity
        assert features[1] == pytest.approx(2.0)  # large scope
        assert features[8] == pytest.approx(2.0)  # opus tier


# ---------------------------------------------------------------------------
# DurationPredictor — cold start
# ---------------------------------------------------------------------------


class TestDurationPredictorColdStart:
    def setup_method(self) -> None:
        reset_predictor()

    def teardown_method(self) -> None:
        reset_predictor()

    def test_cold_start_returns_estimate(self, tmp_path: Path) -> None:
        predictor = DurationPredictor(tmp_path / "models")
        task = _make_task(scope=Scope.MEDIUM, complexity=Complexity.MEDIUM)
        est = predictor.predict(task)
        assert isinstance(est, DurationEstimate)
        assert est.is_cold_start is True
        assert est.confidence == pytest.approx(0.3)
        assert est.p50_seconds > 0
        assert est.p90_seconds > est.p50_seconds

    def test_cold_start_large_high_longer_than_small_low(self, tmp_path: Path) -> None:
        predictor = DurationPredictor(tmp_path / "models")
        small_low = predictor.predict(_make_task(scope=Scope.SMALL, complexity=Complexity.LOW))
        large_high = predictor.predict(_make_task(scope=Scope.LARGE, complexity=Complexity.HIGH))
        assert large_high.p50_seconds > small_low.p50_seconds

    def test_cold_start_p90_greater_than_p50(self, tmp_path: Path) -> None:
        predictor = DurationPredictor(tmp_path / "models")
        est = predictor.predict(_make_task())
        assert est.p90_seconds >= est.p50_seconds


# ---------------------------------------------------------------------------
# DurationPredictor — training data recording
# ---------------------------------------------------------------------------


class TestDurationPredictorRecording:
    def setup_method(self) -> None:
        reset_predictor()

    def teardown_method(self) -> None:
        reset_predictor()

    def test_record_completion_writes_jsonl(self, tmp_path: Path) -> None:
        predictor = DurationPredictor(tmp_path / "models")
        task = _make_task()
        predictor.record_completion(task, actual_duration_seconds=300.0, model="sonnet")

        training_path = tmp_path / "models" / "duration_training.jsonl"
        assert training_path.exists()
        lines = training_path.read_text().strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["actual_duration_seconds"] == pytest.approx(300.0)
        assert record["role"] == "backend"
        assert record["complexity"] == "medium"
        assert record["scope"] == "medium"

    def test_record_ignores_zero_duration(self, tmp_path: Path) -> None:
        predictor = DurationPredictor(tmp_path / "models")
        task = _make_task()
        predictor.record_completion(task, actual_duration_seconds=0.0)
        training_path = tmp_path / "models" / "duration_training.jsonl"
        assert not training_path.exists()

    def test_record_ignores_negative_duration(self, tmp_path: Path) -> None:
        predictor = DurationPredictor(tmp_path / "models")
        task = _make_task()
        predictor.record_completion(task, actual_duration_seconds=-5.0)
        training_path = tmp_path / "models" / "duration_training.jsonl"
        assert not training_path.exists()

    def test_multiple_records_append(self, tmp_path: Path) -> None:
        predictor = DurationPredictor(tmp_path / "models")
        task = _make_task()
        for duration in [100.0, 200.0, 300.0]:
            predictor.record_completion(task, actual_duration_seconds=duration)
        training_path = tmp_path / "models" / "duration_training.jsonl"
        lines = training_path.read_text().strip().splitlines()
        assert len(lines) == 3

    def test_training_sample_count(self, tmp_path: Path) -> None:
        predictor = DurationPredictor(tmp_path / "models")
        task = _make_task()
        assert predictor.training_sample_count == 0
        predictor.record_completion(task, actual_duration_seconds=100.0)
        assert predictor.training_sample_count == 1
        predictor.record_completion(task, actual_duration_seconds=200.0)
        assert predictor.training_sample_count == 2


# ---------------------------------------------------------------------------
# DurationPredictor — training
# ---------------------------------------------------------------------------


class TestDurationPredictorTraining:
    def setup_method(self) -> None:
        reset_predictor()

    def teardown_method(self) -> None:
        reset_predictor()

    def test_train_returns_zero_below_min_samples(self, tmp_path: Path) -> None:
        models_dir = tmp_path / "models"
        models_dir.mkdir()
        _make_training_jsonl(models_dir / "duration_training.jsonl", n=10)
        predictor = DurationPredictor(models_dir)
        result = predictor.train()
        assert result == 0
        assert not predictor.is_trained

    @pytest.mark.skipif(
        not __import__("importlib").util.find_spec("sklearn"),
        reason="scikit-learn not installed",
    )
    def test_train_succeeds_with_enough_samples(self, tmp_path: Path) -> None:
        models_dir = tmp_path / "models"
        models_dir.mkdir()
        _make_training_jsonl(models_dir / "duration_training.jsonl", n=MIN_TRAIN_SAMPLES)
        predictor = DurationPredictor(models_dir)
        result = predictor.train()
        assert result == MIN_TRAIN_SAMPLES
        assert predictor.is_trained

    @pytest.mark.skipif(
        not __import__("importlib").util.find_spec("sklearn"),
        reason="scikit-learn not installed",
    )
    def test_trained_model_produces_non_cold_start_estimate(self, tmp_path: Path) -> None:
        models_dir = tmp_path / "models"
        models_dir.mkdir()
        _make_training_jsonl(models_dir / "duration_training.jsonl", n=MIN_TRAIN_SAMPLES)
        predictor = DurationPredictor(models_dir)
        predictor.train()
        task = _make_task()
        est = predictor.predict(task)
        assert est.is_cold_start is False
        assert est.confidence > 0.3

    @pytest.mark.skipif(
        not __import__("importlib").util.find_spec("sklearn"),
        reason="scikit-learn not installed",
    )
    def test_model_persists_to_disk(self, tmp_path: Path) -> None:
        models_dir = tmp_path / "models"
        models_dir.mkdir()
        _make_training_jsonl(models_dir / "duration_training.jsonl", n=MIN_TRAIN_SAMPLES)
        predictor = DurationPredictor(models_dir)
        predictor.train()
        assert (models_dir / "duration_predictor.pkl").exists()
        assert (models_dir / "duration_predictor_meta.json").exists()

    @pytest.mark.skipif(
        not __import__("importlib").util.find_spec("sklearn"),
        reason="scikit-learn not installed",
    )
    def test_model_loads_from_disk(self, tmp_path: Path) -> None:
        models_dir = tmp_path / "models"
        models_dir.mkdir()
        _make_training_jsonl(models_dir / "duration_training.jsonl", n=MIN_TRAIN_SAMPLES)
        # Train and save
        p1 = DurationPredictor(models_dir)
        p1.train()
        # Load fresh instance — should reload from disk
        reset_predictor()
        p2 = DurationPredictor(models_dir)
        assert p2.is_trained
        task = _make_task()
        est = p2.predict(task)
        assert est.is_cold_start is False

    @pytest.mark.skipif(
        not __import__("importlib").util.find_spec("sklearn"),
        reason="scikit-learn not installed",
    )
    def test_auto_retrain_triggers_on_growth(self, tmp_path: Path) -> None:
        models_dir = tmp_path / "models"
        models_dir.mkdir()
        training_path = models_dir / "duration_training.jsonl"
        _make_training_jsonl(training_path, n=MIN_TRAIN_SAMPLES)
        predictor = DurationPredictor(models_dir)
        predictor.train()
        initial_n = predictor._trained_on_n

        # Add enough records to trigger retraining (>20% growth)
        required_new = int(initial_n * 0.21) + 1
        task = _make_task()
        for _ in range(required_new):
            predictor.record_completion(task, actual_duration_seconds=200.0)

        # Retrain is triggered inside record_completion via _maybe_retrain
        assert predictor._trained_on_n > initial_n


# ---------------------------------------------------------------------------
# DurationPredictor — scikit-learn unavailable
# ---------------------------------------------------------------------------


class TestDurationPredictorNoSklearn:
    def setup_method(self) -> None:
        reset_predictor()

    def teardown_method(self) -> None:
        reset_predictor()

    def test_gracefully_falls_back_when_sklearn_missing(self, tmp_path: Path) -> None:
        models_dir = tmp_path / "models"
        models_dir.mkdir()
        _make_training_jsonl(models_dir / "duration_training.jsonl", n=MIN_TRAIN_SAMPLES)

        predictor = DurationPredictor(models_dir)
        # Simulate sklearn import failure
        with patch.dict("sys.modules", {"sklearn": None, "sklearn.ensemble": None}):
            result = predictor.train()
        # Should return the sample count but not crash
        assert result == MIN_TRAIN_SAMPLES or result == 0

    def test_cold_start_estimate_all_scope_complexity_combos(self, tmp_path: Path) -> None:
        predictor = DurationPredictor(tmp_path / "models")
        for scope in Scope:
            for complexity in Complexity:
                task = _make_task(scope=scope, complexity=complexity)
                est = predictor.predict(task)
                assert est.p50_seconds > 0
                assert est.p90_seconds >= est.p50_seconds
                assert est.is_cold_start is True


# ---------------------------------------------------------------------------
# Global accessor
# ---------------------------------------------------------------------------


class TestGetPredictor:
    def setup_method(self) -> None:
        reset_predictor()

    def teardown_method(self) -> None:
        reset_predictor()

    def test_get_predictor_returns_same_instance(self, tmp_path: Path) -> None:
        p1 = get_predictor(tmp_path / "models")
        p2 = get_predictor(tmp_path / "models2")  # second call ignores arg
        assert p1 is p2

    def test_reset_predictor_clears_singleton(self, tmp_path: Path) -> None:
        p1 = get_predictor(tmp_path / "models")
        reset_predictor()
        p2 = get_predictor(tmp_path / "models2")
        assert p1 is not p2
