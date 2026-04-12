"""Predictive cost model using online linear regression.

Learns from actual token consumption outcomes to predict future task costs.
Unlike the heuristic-based ``cost_forecast.py`` and ``cost_estimation.py``,
this module trains an online linear regression model via stochastic gradient
descent (SGD) on observed (features, tokens) pairs.

When fewer than ``min_observations`` samples are available, the model falls
back to the same scope/complexity heuristic tables used elsewhere.  Once
enough data is collected the learned weights replace the heuristic entirely.

Model state is persisted to ``.sdd/metrics/predictive_model.json``.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Heuristic fallback tables (same values as cost_estimation / cost_forecast)
# ---------------------------------------------------------------------------

_SCOPE_TOKENS_K: dict[str, float] = {
    "small": 10.0,
    "medium": 50.0,
    "large": 150.0,
}

_COMPLEXITY_MULTIPLIER: dict[str, float] = {
    "low": 0.7,
    "medium": 1.0,
    "high": 2.0,
}

# Blended cost per 1k tokens — used to convert token estimates into USD.
_BLENDED_COST_PER_1K: dict[str, float] = {
    "haiku": 0.003,
    "sonnet": 0.009,
    "opus": 0.015,
    "gpt-5.4": 0.00875,
    "gpt-5.4-mini": 0.002625,
    "o3": 0.005,
    "o4-mini": 0.00275,
    "gemini-3": 0.009,
    "gemini-3.1-pro": 0.00175,
    "gemini-3-flash": 0.000575,
    "qwen3-coder": 0.00056,
    "qwen-max": 0.001,
    "qwen-plus": 0.0005,
    "qwen-turbo": 0.0002,
}

_DEFAULT_COST_PER_1K: float = 0.005

# ---------------------------------------------------------------------------
# Ordinal encoding maps
# ---------------------------------------------------------------------------

_SCOPE_ORDINAL: dict[str, float] = {"small": 1.0, "medium": 2.0, "large": 3.0}
_COMPLEXITY_ORDINAL: dict[str, float] = {"low": 1.0, "medium": 2.0, "high": 3.0}

# Feature vector length: scope + complexity + file_count + code_complexity + role_hash + model_hash
_N_FEATURES: int = 6


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TaskFeatures:
    """Feature set describing a task for cost prediction.

    Attributes:
        role: Task role (backend, frontend, qa, etc.).
        scope: Task scope — small, medium, or large.
        complexity: Task complexity — low, medium, or high.
        model: Model name used for the task.
        file_count: Number of files in scope (0 if unknown).
        code_complexity: Normalised complexity score between 0.0 and 1.0.
    """

    role: str
    scope: str
    complexity: str
    model: str
    file_count: int = 0
    code_complexity: float = 0.5

    def to_feature_vector(self) -> list[float]:
        """Encode task features as a numeric vector for regression.

        Returns:
            List of floats: [scope_ord, complexity_ord, file_count,
            code_complexity, role_hash, model_hash].
        """
        return [
            _SCOPE_ORDINAL.get(self.scope, 2.0),
            _COMPLEXITY_ORDINAL.get(self.complexity, 2.0),
            float(self.file_count),
            self.code_complexity,
            hash(self.role) % 100 / 100.0,
            hash(self.model) % 100 / 100.0,
        ]

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return {
            "role": self.role,
            "scope": self.scope,
            "complexity": self.complexity,
            "model": self.model,
            "file_count": self.file_count,
            "code_complexity": self.code_complexity,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TaskFeatures:
        """Deserialise from a dict."""
        return cls(
            role=str(d["role"]),
            scope=str(d["scope"]),
            complexity=str(d["complexity"]),
            model=str(d["model"]),
            file_count=int(d.get("file_count", 0)),
            code_complexity=float(d.get("code_complexity", 0.5)),
        )


@dataclass(frozen=True)
class CostPrediction:
    """Result of a cost prediction.

    Attributes:
        estimated_tokens: Predicted total token count.
        estimated_cost_usd: Predicted cost in USD.
        confidence: Confidence level between 0.0 and 1.0.
        prediction_interval_low: Lower bound of the cost prediction interval.
        prediction_interval_high: Upper bound of the cost prediction interval.
        sample_count: Number of observations used to train the model.
        source: Either ``"predictive_model"`` or ``"heuristic_fallback"``.
    """

    estimated_tokens: int
    estimated_cost_usd: float
    confidence: float
    prediction_interval_low: float
    prediction_interval_high: float
    sample_count: int
    source: str

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return {
            "estimated_tokens": self.estimated_tokens,
            "estimated_cost_usd": round(self.estimated_cost_usd, 6),
            "confidence": round(self.confidence, 3),
            "prediction_interval_low": round(self.prediction_interval_low, 6),
            "prediction_interval_high": round(self.prediction_interval_high, 6),
            "sample_count": self.sample_count,
            "source": self.source,
        }


@dataclass
class TrainingObservation:
    """A recorded (features, outcome) pair used for model training.

    Attributes:
        features: The task feature set.
        actual_tokens: Total tokens consumed (input + output).
        actual_cost_usd: Actual cost incurred.
        timestamp: Unix timestamp when the observation was recorded.
    """

    features: TaskFeatures
    actual_tokens: int
    actual_cost_usd: float
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return {
            "features": self.features.to_dict(),
            "actual_tokens": self.actual_tokens,
            "actual_cost_usd": self.actual_cost_usd,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TrainingObservation:
        """Deserialise from a dict."""
        return cls(
            features=TaskFeatures.from_dict(d["features"]),
            actual_tokens=int(d["actual_tokens"]),
            actual_cost_usd=float(d["actual_cost_usd"]),
            timestamp=float(d.get("timestamp", 0.0)),
        )


# ---------------------------------------------------------------------------
# Predictive cost model
# ---------------------------------------------------------------------------


def _dot(a: list[float], b: list[float]) -> float:
    """Dot product of two equal-length float vectors."""
    return sum(x * y for x, y in zip(a, b, strict=True))


def _model_cost_per_1k(model: str) -> float:
    """Blended cost per 1k tokens for a model name."""
    model_lower = model.lower()
    for key, cost in _BLENDED_COST_PER_1K.items():
        if key in model_lower:
            return cost
    return _DEFAULT_COST_PER_1K


class PredictiveCostModel:
    """Online linear regression model for task cost prediction.

    Learns a mapping from :class:`TaskFeatures` to total token consumption
    via stochastic gradient descent.  When fewer than ``min_observations``
    samples have been recorded, predictions fall back to the static
    scope/complexity heuristic.

    Thread-safe: all mutations acquire ``_lock``.

    Args:
        min_observations: Minimum training samples before the learned
            weights are used instead of the heuristic fallback.
    """

    def __init__(self, min_observations: int = 10) -> None:
        self._min_observations: int = min_observations
        self._weights: list[float] = [0.0] * _N_FEATURES
        self._bias: float = 0.0
        self._observations: list[TrainingObservation] = []
        self._n_features: int = _N_FEATURES
        self._learning_rate: float = 0.001
        self._lock: threading.Lock = threading.Lock()

        # Running residual tracking for confidence/intervals.
        self._residual_sum_sq: float = 0.0
        self._residual_count: int = 0

    # ---- public API -------------------------------------------------------

    def record_observation(
        self,
        features: TaskFeatures,
        actual_tokens: int,
        actual_cost_usd: float,
    ) -> None:
        """Record an observed outcome and update model weights.

        Args:
            features: The task features for this observation.
            actual_tokens: Total tokens consumed (input + output).
            actual_cost_usd: Actual cost in USD.
        """
        obs = TrainingObservation(
            features=features,
            actual_tokens=actual_tokens,
            actual_cost_usd=actual_cost_usd,
        )
        with self._lock:
            self._observations.append(obs)
            fv = features.to_feature_vector()
            self._sgd_update(fv, float(actual_tokens))

    def predict(self, features: TaskFeatures) -> CostPrediction:
        """Predict token consumption and cost for a task.

        Uses the learned regression weights when enough observations
        exist; otherwise returns a heuristic-based estimate.

        Args:
            features: Task features to predict for.

        Returns:
            :class:`CostPrediction` with estimate and confidence interval.
        """
        with self._lock:
            if len(self._observations) < self._min_observations:
                return self._heuristic_fallback(features)

            fv = features.to_feature_vector()
            predicted_tokens = _dot(self._weights, fv) + self._bias
            predicted_tokens = max(predicted_tokens, 0.0)

            cost_per_1k = _model_cost_per_1k(features.model)
            estimated_cost = (predicted_tokens / 1000.0) * cost_per_1k

            confidence = self._compute_confidence()
            interval_low, interval_high = self._compute_prediction_interval(
                estimated_cost,
            )

            return CostPrediction(
                estimated_tokens=int(predicted_tokens),
                estimated_cost_usd=estimated_cost,
                confidence=confidence,
                prediction_interval_low=interval_low,
                prediction_interval_high=interval_high,
                sample_count=len(self._observations),
                source="predictive_model",
            )

    def save(self, path: Path) -> None:
        """Persist model state to a JSON file.

        Args:
            path: File path to write (e.g.
                ``.sdd/metrics/predictive_model.json``).
        """
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            data: dict[str, Any] = {
                "weights": self._weights,
                "bias": self._bias,
                "n_features": self._n_features,
                "learning_rate": self._learning_rate,
                "min_observations": self._min_observations,
                "residual_sum_sq": self._residual_sum_sq,
                "residual_count": self._residual_count,
                "observations": [o.to_dict() for o in self._observations],
            }
        path.write_text(json.dumps(data, indent=2))
        logger.debug("Predictive cost model saved to %s", path)

    @classmethod
    def load(cls, path: Path) -> PredictiveCostModel:
        """Load a previously persisted model from a JSON file.

        Args:
            path: Path to the JSON file written by :meth:`save`.

        Returns:
            Restored :class:`PredictiveCostModel`.

        Raises:
            FileNotFoundError: If the file does not exist.
            json.JSONDecodeError: If the file is not valid JSON.
        """
        data = json.loads(path.read_text())
        model = cls(min_observations=int(data.get("min_observations", 10)))
        model._weights = [float(w) for w in data["weights"]]
        model._bias = float(data["bias"])
        model._n_features = int(data["n_features"])
        model._learning_rate = float(data.get("learning_rate", 0.001))
        model._residual_sum_sq = float(data.get("residual_sum_sq", 0.0))
        model._residual_count = int(data.get("residual_count", 0))
        model._observations = [TrainingObservation.from_dict(o) for o in data.get("observations", [])]
        return model

    def summary(self) -> dict[str, Any]:
        """Return diagnostic statistics about the model.

        Returns:
            Dict with observation_count, avg_error, confidence, weights,
            bias, and whether the model is active or falling back.
        """
        with self._lock:
            n = len(self._observations)
            avg_error = 0.0
            if self._residual_count > 0:
                mse = self._residual_sum_sq / self._residual_count
                avg_error = math.sqrt(mse)

            return {
                "observation_count": n,
                "min_observations": self._min_observations,
                "active": n >= self._min_observations,
                "avg_error_tokens": round(avg_error, 1),
                "confidence": round(self._compute_confidence(), 3),
                "weights": list(self._weights),
                "bias": round(self._bias, 4),
                "residual_count": self._residual_count,
            }

    # ---- internal ---------------------------------------------------------

    def _sgd_update(self, feature_vector: list[float], target: float) -> None:
        """Perform a single step of stochastic gradient descent.

        Updates weights and bias to minimise squared error between the
        predicted and actual token count.  Gradients are clipped to
        prevent numerical explosion.

        Must be called while holding ``_lock``.

        Args:
            feature_vector: Numeric feature vector from ``to_feature_vector()``.
            target: Actual total token count observed.
        """
        predicted = _dot(self._weights, feature_vector) + self._bias
        error = predicted - target

        # Track residuals for confidence intervals
        self._residual_sum_sq += error * error
        self._residual_count += 1

        grad_clip = 1e6
        clipped_error = max(-grad_clip, min(error, grad_clip))

        for i in range(self._n_features):
            grad = clipped_error * feature_vector[i]
            grad = max(-grad_clip, min(grad, grad_clip))
            self._weights[i] -= self._learning_rate * grad

        self._bias -= self._learning_rate * clipped_error

    def _heuristic_fallback(self, features: TaskFeatures) -> CostPrediction:
        """Estimate cost using static scope/complexity lookup tables.

        Used when the model has insufficient training data.

        Args:
            features: Task features to estimate for.

        Returns:
            :class:`CostPrediction` with source ``"heuristic_fallback"``.
        """
        base_k = _SCOPE_TOKENS_K.get(features.scope, 50.0)
        mult = _COMPLEXITY_MULTIPLIER.get(features.complexity, 1.0)
        estimated_tokens_k = base_k * mult
        estimated_tokens = int(estimated_tokens_k * 1000)

        cost_per_1k = _model_cost_per_1k(features.model)
        estimated_cost = (estimated_tokens / 1000.0) * cost_per_1k

        n = len(self._observations)
        confidence = min(n / (self._min_observations * 2.0), 0.4)

        return CostPrediction(
            estimated_tokens=estimated_tokens,
            estimated_cost_usd=estimated_cost,
            confidence=confidence,
            prediction_interval_low=estimated_cost * 0.5,
            prediction_interval_high=estimated_cost * 2.0,
            sample_count=n,
            source="heuristic_fallback",
        )

    def _compute_confidence(self) -> float:
        """Compute model confidence from observation count and fit quality.

        Confidence rises with more observations and falls when residuals
        are large relative to the mean target.

        Must be called while holding ``_lock``.

        Returns:
            Confidence score between 0.0 and 1.0.
        """
        n = len(self._observations)
        if n == 0:
            return 0.0

        # Base confidence from sample size: asymptotically approaches 1.0
        count_factor = 1.0 - 1.0 / (1.0 + n / 20.0)

        if self._residual_count == 0:
            return round(min(count_factor, 0.95), 3)

        rmse = math.sqrt(self._residual_sum_sq / self._residual_count)

        # Compute mean target for normalisation
        mean_target = sum(o.actual_tokens for o in self._observations) / n
        if mean_target > 0:
            # Normalised RMSE: lower is better
            nrmse = rmse / mean_target
            fit_factor = max(0.0, 1.0 - nrmse)
        else:
            fit_factor = 0.5

        combined = 0.6 * count_factor + 0.4 * fit_factor
        return round(min(combined, 0.95), 3)

    def _compute_prediction_interval(
        self,
        predicted_cost: float,
    ) -> tuple[float, float]:
        """Compute prediction interval using residual standard deviation.

        With more observations the interval narrows; with fewer it is
        wider, reflecting greater uncertainty.

        Must be called while holding ``_lock``.

        Args:
            predicted_cost: The point estimate in USD.

        Returns:
            Tuple of (lower_bound, upper_bound) in USD.
        """
        if self._residual_count < 2:
            return (predicted_cost * 0.5, predicted_cost * 2.0)

        rmse_tokens = math.sqrt(
            self._residual_sum_sq / self._residual_count,
        )

        # Convert token residual to USD using a rough average cost
        n = len(self._observations)
        if n > 0:
            avg_cost_per_token = sum(o.actual_cost_usd / max(o.actual_tokens, 1) for o in self._observations) / n
        else:
            avg_cost_per_token = _DEFAULT_COST_PER_1K / 1000.0

        rmse_usd = rmse_tokens * avg_cost_per_token

        # 1.96 * std for ~95% interval
        margin = 1.96 * rmse_usd
        low = max(predicted_cost - margin, 0.0)
        high = predicted_cost + margin

        return (low, high)
