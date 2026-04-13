"""Tests for predictive cost model (online linear regression)."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from bernstein.core.predictive_cost_model import (
    CostPrediction,
    PredictiveCostModel,
    TaskFeatures,
    TrainingObservation,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_features(
    role: str = "backend",
    scope: str = "medium",
    complexity: str = "medium",
    model: str = "sonnet",
    file_count: int = 5,
    code_complexity: float = 0.5,
) -> TaskFeatures:
    return TaskFeatures(
        role=role,
        scope=scope,
        complexity=complexity,
        model=model,
        file_count=file_count,
        code_complexity=code_complexity,
    )


def _train_model(
    n: int = 15,
    *,
    min_observations: int = 10,
    base_tokens: int = 50_000,
    base_cost: float = 0.45,
) -> PredictiveCostModel:
    """Create a model pre-trained with *n* observations."""
    model = PredictiveCostModel(min_observations=min_observations)
    for i in range(n):
        features = _make_features(file_count=5 + i)
        model.record_observation(
            features,
            actual_tokens=base_tokens + i * 1000,
            actual_cost_usd=base_cost + i * 0.01,
        )
    return model


# ---------------------------------------------------------------------------
# TaskFeatures
# ---------------------------------------------------------------------------


class TestTaskFeatures:
    def test_to_feature_vector_length(self) -> None:
        fv = _make_features().to_feature_vector()
        assert len(fv) == 6

    def test_feature_vector_deterministic(self) -> None:
        """Same inputs always produce the same vector."""
        f = _make_features(role="qa", model="opus")
        assert f.to_feature_vector() == f.to_feature_vector()

    def test_scope_encoding(self) -> None:
        assert _make_features(scope="small").to_feature_vector()[0] == pytest.approx(1.0)
        assert _make_features(scope="medium").to_feature_vector()[0] == pytest.approx(2.0)
        assert _make_features(scope="large").to_feature_vector()[0] == pytest.approx(3.0)

    def test_complexity_encoding(self) -> None:
        assert _make_features(complexity="low").to_feature_vector()[1] == pytest.approx(1.0)
        assert _make_features(complexity="medium").to_feature_vector()[1] == pytest.approx(2.0)
        assert _make_features(complexity="high").to_feature_vector()[1] == pytest.approx(3.0)

    def test_unknown_scope_defaults_to_medium(self) -> None:
        fv = _make_features(scope="custom").to_feature_vector()
        assert fv[0] == pytest.approx(2.0)

    def test_unknown_complexity_defaults_to_medium(self) -> None:
        fv = _make_features(complexity="custom").to_feature_vector()
        assert fv[1] == pytest.approx(2.0)

    def test_file_count_passthrough(self) -> None:
        fv = _make_features(file_count=42).to_feature_vector()
        assert fv[2] == pytest.approx(42.0)

    def test_code_complexity_passthrough(self) -> None:
        fv = _make_features(code_complexity=0.8).to_feature_vector()
        assert fv[3] == pytest.approx(0.8)

    def test_role_hash_bounded(self) -> None:
        fv = _make_features(role="backend").to_feature_vector()
        assert 0.0 <= fv[4] < 1.0

    def test_model_hash_bounded(self) -> None:
        fv = _make_features(model="opus").to_feature_vector()
        assert 0.0 <= fv[5] < 1.0

    def test_to_dict_roundtrip(self) -> None:
        f = _make_features(role="frontend", scope="large", complexity="high")
        restored = TaskFeatures.from_dict(f.to_dict())
        assert restored == f

    def test_from_dict_defaults(self) -> None:
        d = {"role": "qa", "scope": "small", "complexity": "low", "model": "haiku"}
        f = TaskFeatures.from_dict(d)
        assert f.file_count == 0
        assert f.code_complexity == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# CostPrediction
# ---------------------------------------------------------------------------


class TestCostPrediction:
    def test_to_dict_keys(self) -> None:
        pred = CostPrediction(
            estimated_tokens=50_000,
            estimated_cost_usd=0.45,
            confidence=0.7,
            prediction_interval_low=0.3,
            prediction_interval_high=0.6,
            sample_count=15,
            source="predictive_model",
        )
        d = pred.to_dict()
        expected_keys = {
            "estimated_tokens",
            "estimated_cost_usd",
            "confidence",
            "prediction_interval_low",
            "prediction_interval_high",
            "sample_count",
            "source",
        }
        assert set(d.keys()) == expected_keys


# ---------------------------------------------------------------------------
# TrainingObservation
# ---------------------------------------------------------------------------


class TestTrainingObservation:
    def test_to_dict_roundtrip(self) -> None:
        obs = TrainingObservation(
            features=_make_features(),
            actual_tokens=30_000,
            actual_cost_usd=0.27,
            timestamp=1234567890.0,
        )
        restored = TrainingObservation.from_dict(obs.to_dict())
        assert restored.features == obs.features
        assert restored.actual_tokens == obs.actual_tokens
        assert restored.actual_cost_usd == obs.actual_cost_usd
        assert restored.timestamp == obs.timestamp


# ---------------------------------------------------------------------------
# PredictiveCostModel — heuristic fallback
# ---------------------------------------------------------------------------


class TestHeuristicFallback:
    def test_fallback_when_insufficient_data(self) -> None:
        model = PredictiveCostModel(min_observations=10)
        pred = model.predict(_make_features())
        assert pred.source == "heuristic_fallback"
        assert pred.sample_count == 0

    def test_fallback_with_some_data(self) -> None:
        """Under min_observations still triggers fallback."""
        model = PredictiveCostModel(min_observations=10)
        for _ in range(5):
            model.record_observation(_make_features(), 10_000, 0.09)
        pred = model.predict(_make_features())
        assert pred.source == "heuristic_fallback"
        assert pred.sample_count == 5

    def test_small_low_cheaper_than_large_high(self) -> None:
        model = PredictiveCostModel(min_observations=10)
        small = model.predict(_make_features(scope="small", complexity="low"))
        large = model.predict(_make_features(scope="large", complexity="high"))
        assert small.estimated_cost_usd < large.estimated_cost_usd

    def test_fallback_interval_doubles(self) -> None:
        """Heuristic fallback uses 0.5x-2.0x interval."""
        model = PredictiveCostModel(min_observations=10)
        pred = model.predict(_make_features())
        assert pred.prediction_interval_low < pred.estimated_cost_usd
        assert pred.prediction_interval_high > pred.estimated_cost_usd
        assert abs(pred.prediction_interval_low - pred.estimated_cost_usd * 0.5) < 1e-6
        assert abs(pred.prediction_interval_high - pred.estimated_cost_usd * 2.0) < 1e-6

    def test_fallback_confidence_zero_with_no_data(self) -> None:
        model = PredictiveCostModel(min_observations=10)
        pred = model.predict(_make_features())
        assert pred.confidence == pytest.approx(0.0)

    def test_fallback_positive_tokens_and_cost(self) -> None:
        model = PredictiveCostModel(min_observations=10)
        pred = model.predict(_make_features())
        assert pred.estimated_tokens > 0
        assert pred.estimated_cost_usd > 0.0


# ---------------------------------------------------------------------------
# PredictiveCostModel — trained predictions
# ---------------------------------------------------------------------------


class TestTrainedPredictions:
    def test_switches_to_predictive_after_enough_data(self) -> None:
        model = _train_model(n=15)
        pred = model.predict(_make_features())
        assert pred.source == "predictive_model"
        assert pred.sample_count == 15

    def test_predictions_nonnegative(self) -> None:
        model = _train_model(n=20)
        pred = model.predict(_make_features())
        assert pred.estimated_tokens >= 0
        assert pred.estimated_cost_usd >= 0.0

    def test_prediction_improves_with_consistent_data(self) -> None:
        """Model trained on consistent data should converge near the target."""
        target_tokens = 40_000
        model = PredictiveCostModel(min_observations=5)
        features = _make_features()

        for _ in range(200):
            model.record_observation(features, target_tokens, 0.36)

        pred = model.predict(features)
        assert pred.source == "predictive_model"
        error_pct = abs(pred.estimated_tokens - target_tokens) / target_tokens
        assert error_pct < 0.3, f"Error {error_pct:.1%} too large after 200 observations"

    def test_larger_scope_predicts_more_tokens(self) -> None:
        """Model trained with scale-consistent data should predict more
        tokens for larger scope."""
        model = PredictiveCostModel(min_observations=5)
        for _ in range(50):
            model.record_observation(
                _make_features(scope="small"),
                10_000,
                0.09,
            )
            model.record_observation(
                _make_features(scope="large"),
                100_000,
                0.90,
            )

        small_pred = model.predict(_make_features(scope="small"))
        large_pred = model.predict(_make_features(scope="large"))
        assert large_pred.estimated_tokens > small_pred.estimated_tokens


# ---------------------------------------------------------------------------
# Prediction intervals
# ---------------------------------------------------------------------------


class TestPredictionIntervals:
    def test_interval_contains_estimate(self) -> None:
        model = _train_model(n=20)
        pred = model.predict(_make_features())
        assert pred.prediction_interval_low <= pred.estimated_cost_usd
        assert pred.prediction_interval_high >= pred.estimated_cost_usd

    def test_interval_narrows_with_consistent_data(self) -> None:
        """Interval should be narrower after many consistent observations
        vs. after just crossing the threshold."""
        target = 50_000
        features = _make_features()

        few_model = PredictiveCostModel(min_observations=5)
        for _ in range(10):
            few_model.record_observation(features, target, 0.45)
        few_pred = few_model.predict(features)
        few_width = few_pred.prediction_interval_high - few_pred.prediction_interval_low

        many_model = PredictiveCostModel(min_observations=5)
        for _ in range(200):
            many_model.record_observation(features, target, 0.45)
        many_pred = many_model.predict(features)
        many_width = many_pred.prediction_interval_high - many_pred.prediction_interval_low

        assert many_width <= few_width

    def test_interval_low_nonnegative(self) -> None:
        model = _train_model(n=15)
        pred = model.predict(_make_features())
        assert pred.prediction_interval_low >= 0.0


# ---------------------------------------------------------------------------
# Confidence
# ---------------------------------------------------------------------------


class TestConfidence:
    def test_confidence_increases_with_observations(self) -> None:
        features = _make_features()
        model = PredictiveCostModel(min_observations=5)

        for _ in range(6):
            model.record_observation(features, 50_000, 0.45)
        early_pred = model.predict(features)

        for _ in range(50):
            model.record_observation(features, 50_000, 0.45)
        late_pred = model.predict(features)

        assert late_pred.confidence > early_pred.confidence

    def test_confidence_capped_at_095(self) -> None:
        model = PredictiveCostModel(min_observations=5)
        features = _make_features()
        for _ in range(500):
            model.record_observation(features, 50_000, 0.45)
        pred = model.predict(features)
        assert pred.confidence <= 0.95


# ---------------------------------------------------------------------------
# Save / load round-trip
# ---------------------------------------------------------------------------


class TestSaveLoad:
    def test_roundtrip_preserves_state(self, tmp_path: Path) -> None:
        model = _train_model(n=15)
        save_path = tmp_path / "model.json"
        model.save(save_path)

        loaded = PredictiveCostModel.load(save_path)

        # pyright: ignore[reportPrivateUsage] — intentional for round-trip verification
        assert loaded._weights == model._weights  # pyright: ignore[reportPrivateUsage]
        assert loaded._bias == model._bias  # pyright: ignore[reportPrivateUsage]
        assert loaded._min_observations == model._min_observations  # pyright: ignore[reportPrivateUsage]
        assert loaded._learning_rate == model._learning_rate  # pyright: ignore[reportPrivateUsage]
        assert loaded._residual_sum_sq == model._residual_sum_sq  # pyright: ignore[reportPrivateUsage]
        assert loaded._residual_count == model._residual_count  # pyright: ignore[reportPrivateUsage]
        assert len(loaded._observations) == len(model._observations)  # pyright: ignore[reportPrivateUsage]

    def test_loaded_model_predictions_match(self, tmp_path: Path) -> None:
        model = _train_model(n=20)
        features = _make_features()
        before_pred = model.predict(features)

        save_path = tmp_path / "model.json"
        model.save(save_path)
        loaded = PredictiveCostModel.load(save_path)
        after_pred = loaded.predict(features)

        assert before_pred.estimated_tokens == after_pred.estimated_tokens
        assert abs(before_pred.estimated_cost_usd - after_pred.estimated_cost_usd) < 1e-9

    def test_save_creates_parent_dirs(self, tmp_path: Path) -> None:
        model = PredictiveCostModel()
        save_path = tmp_path / "deep" / "nested" / "model.json"
        model.save(save_path)
        assert save_path.exists()


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


class TestEdgeCases:
    def test_zero_tokens_observation(self) -> None:
        """Recording zero tokens does not crash."""
        model = PredictiveCostModel(min_observations=5)
        model.record_observation(_make_features(), actual_tokens=0, actual_cost_usd=0.0)
        pred = model.predict(_make_features())
        assert pred.source == "heuristic_fallback"

    def test_unknown_role(self) -> None:
        """Unknown roles produce valid (hash-based) features."""
        model = PredictiveCostModel(min_observations=5)
        features = _make_features(role="interdimensional-cable-engineer")
        pred = model.predict(features)
        assert pred.estimated_tokens > 0

    def test_unknown_model(self) -> None:
        """Unknown model names use default pricing fallback."""
        model = PredictiveCostModel(min_observations=5)
        features = _make_features(model="unknownmodel-7b")
        pred = model.predict(features)
        assert pred.estimated_cost_usd > 0.0

    def test_very_large_tokens(self) -> None:
        """Large token values do not cause overflow."""
        model = PredictiveCostModel(min_observations=5)
        for _ in range(10):
            model.record_observation(
                _make_features(),
                actual_tokens=10_000_000,
                actual_cost_usd=90.0,
            )
        pred = model.predict(_make_features())
        assert pred.estimated_tokens >= 0

    def test_summary_before_any_data(self) -> None:
        model = PredictiveCostModel()
        s = model.summary()
        assert s["observation_count"] == 0
        assert s["active"] is False

    def test_summary_after_training(self) -> None:
        model = _train_model(n=15)
        s = model.summary()
        assert s["observation_count"] == 15
        assert s["active"] is True
        assert "weights" in s
        assert "bias" in s


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


class TestThreadSafety:
    def test_concurrent_record_and_predict(self) -> None:
        """Concurrent record + predict calls do not raise."""
        model = PredictiveCostModel(min_observations=5)
        errors: list[Exception] = []

        def recorder() -> None:
            try:
                for i in range(50):
                    model.record_observation(
                        _make_features(file_count=i),
                        actual_tokens=10_000 + i * 100,
                        actual_cost_usd=0.09 + i * 0.001,
                    )
            except Exception as exc:
                errors.append(exc)

        def predictor() -> None:
            try:
                for _ in range(50):
                    pred = model.predict(_make_features())
                    assert pred.estimated_tokens >= 0
            except Exception as exc:
                errors.append(exc)

        threads: list[threading.Thread] = []
        for _ in range(3):
            threads.append(threading.Thread(target=recorder))
            threads.append(threading.Thread(target=predictor))

        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert errors == [], f"Thread errors: {errors}"

    def test_concurrent_observation_count(self) -> None:
        """All concurrent observations are recorded."""
        model = PredictiveCostModel(min_observations=5)
        n_threads = 4
        n_per_thread = 25

        def worker() -> None:
            for i in range(n_per_thread):
                model.record_observation(
                    _make_features(file_count=i),
                    actual_tokens=10_000,
                    actual_cost_usd=0.09,
                )

        threads = [threading.Thread(target=worker) for _ in range(n_threads)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)

        assert model.summary()["observation_count"] == n_threads * n_per_thread
