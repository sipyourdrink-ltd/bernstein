"""ML-predicted task duration — feature extraction, training, and inference.

Uses a local GradientBoostingRegressor trained on historical completions stored in
``.sdd/models/duration_training.jsonl``.  Falls back to a static scope/complexity
lookup table until at least 50 completions are available (cold start).

Prediction API::

    from bernstein.core.duration_predictor import get_predictor, DurationEstimate

    predictor = get_predictor(workdir / ".sdd" / "models")
    estimate: DurationEstimate = predictor.predict(task)
    # estimate.p50_seconds — median expected duration
    # estimate.p90_seconds — use for deadline slack calculation
    # estimate.confidence  — 0.3 = cold start, up to 0.95 at 1k+ samples
"""

from __future__ import annotations

import json
import logging
import math
import pickle
import time
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

if TYPE_CHECKING:
    from bernstein.core.models import Task

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MIN_TRAIN_SAMPLES = 50
RETRAIN_GROWTH_THRESHOLD = 0.20  # Retrain when dataset grows by ≥20%

# Cold-start lookup: (scope, complexity) → p50_seconds
_COLD_START_P50: dict[tuple[str, str], float] = {
    ("small", "low"): 120.0,
    ("small", "medium"): 300.0,
    ("small", "high"): 600.0,
    ("medium", "low"): 600.0,
    ("medium", "medium"): 1200.0,
    ("medium", "high"): 2400.0,
    ("large", "low"): 1800.0,
    ("large", "medium"): 3600.0,
    ("large", "high"): 7200.0,
}

# P90 multipliers by complexity (right-skewed distributions)
_P90_MULT: dict[str, float] = {"low": 1.5, "medium": 2.0, "high": 2.5}


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DurationEstimate:
    """Predicted task duration with uncertainty bounds.

    Attributes:
        p50_seconds: Median expected duration.
        p90_seconds: 90th-percentile duration — use this for deadline slack.
        confidence: Estimate quality 0.0-1.0. Values below 0.5 indicate cold
            start or insufficient training data.
        is_cold_start: True when no trained model is available and the
            static fallback table was used.
    """

    p50_seconds: float
    p90_seconds: float
    confidence: float
    is_cold_start: bool = False


@dataclass
class TrainingRecord:
    """A completed-task data point for duration model training.

    All fields are deliberately primitive so records serialise to plain JSON
    without custom encoders.
    """

    task_id: str
    role: str
    complexity: str  # "low" | "medium" | "high"
    scope: str  # "small" | "medium" | "large"
    task_type: str  # "standard" | "fix" | "research" | "upgrade_proposal"
    priority: int  # 1-3
    estimated_minutes: int
    description_length: int
    owned_files_count: int
    depends_on_count: int
    model: str  # "haiku" | "sonnet" | "opus" | "fast-path" | …
    actual_duration_seconds: float
    timestamp: float


# ---------------------------------------------------------------------------
# Feature extraction
# ---------------------------------------------------------------------------


class TaskFeatureExtractor:
    """Converts a :class:`~bernstein.core.models.Task` into a numeric feature vector.

    Feature layout (9 dimensions)::

        [complexity_idx, scope_idx, task_type_idx, priority,
         estimated_minutes, description_length, owned_files_count,
         depends_on_count, model_tier]

    All values are numeric — no one-hot encoding — so the tree-based model
    handles ordinality natively without dimensionality explosion.
    """

    _COMPLEXITY_MAP: ClassVar[dict[str, float]] = {"low": 0.0, "medium": 1.0, "high": 2.0}
    _SCOPE_MAP: ClassVar[dict[str, float]] = {"small": 0.0, "medium": 1.0, "large": 2.0}
    _TASK_TYPE_MAP: ClassVar[dict[str, float]] = {
        "standard": 0.0,
        "fix": 1.0,
        "research": 2.0,
        "upgrade_proposal": 3.0,
    }

    def extract(self, task: Task, model: str | None = None) -> list[float]:
        """Build feature vector from a live Task object.

        Args:
            task: Task to featurise.
            model: Effective model name (overrides ``task.model`` when set).

        Returns:
            9-element list of floats ready for scikit-learn inference.
        """
        complexity = task.complexity.value if hasattr(task.complexity, "value") else str(task.complexity)
        scope = task.scope.value if hasattr(task.scope, "value") else str(task.scope)
        task_type = task.task_type.value if hasattr(task.task_type, "value") else "standard"
        effective_model = model or task.model or "sonnet"

        return [
            self._COMPLEXITY_MAP.get(complexity, 1.0),
            self._SCOPE_MAP.get(scope, 1.0),
            self._TASK_TYPE_MAP.get(task_type, 0.0),
            float(task.priority),
            float(task.estimated_minutes),
            float(len(task.description)),
            float(len(task.owned_files)),
            float(len(task.depends_on)),
            self._model_tier(effective_model),
        ]

    def extract_from_record(self, rec: TrainingRecord) -> list[float]:
        """Build feature vector from a :class:`TrainingRecord`.

        Args:
            rec: Persisted training record.

        Returns:
            9-element list of floats.
        """
        return [
            self._COMPLEXITY_MAP.get(rec.complexity, 1.0),
            self._SCOPE_MAP.get(rec.scope, 1.0),
            self._TASK_TYPE_MAP.get(rec.task_type, 0.0),
            float(rec.priority),
            float(rec.estimated_minutes),
            float(rec.description_length),
            float(rec.owned_files_count),
            float(rec.depends_on_count),
            self._model_tier(rec.model),
        ]

    @staticmethod
    def _model_tier(model: str) -> float:
        """Map model name to a 0/1/2 capability tier.

        Args:
            model: Model identifier string.

        Returns:
            0.0 = haiku/fast-path, 1.0 = sonnet (default), 2.0 = opus.
        """
        lower = model.lower()
        if "haiku" in lower or "fast" in lower:
            return 0.0
        if "opus" in lower:
            return 2.0
        return 1.0


# ---------------------------------------------------------------------------
# Predictor
# ---------------------------------------------------------------------------


class DurationPredictor:
    """ML-based task duration predictor with cold-start fallback.

    Trains a GradientBoostingRegressor on log-transformed actual durations
    collected in ``.sdd/models/duration_training.jsonl``.  When fewer than
    :data:`MIN_TRAIN_SAMPLES` completions are available the predictor falls
    back to a static scope/complexity lookup table.

    Auto-retrains on completion events when the dataset has grown by
    :data:`RETRAIN_GROWTH_THRESHOLD` since the last training run.

    Args:
        models_dir: Directory for model artefacts and training data.
    """

    def __init__(self, models_dir: Path) -> None:
        self._models_dir = models_dir
        self._models_dir.mkdir(parents=True, exist_ok=True)
        self._training_path = models_dir / "duration_training.jsonl"
        self._model_path = models_dir / "duration_predictor.pkl"
        self._meta_path = models_dir / "duration_predictor_meta.json"
        self._extractor = TaskFeatureExtractor()
        self._model: Any = None
        self._trained_on_n: int = 0
        self._load_model()

    # -- model persistence ---------------------------------------------------

    def _load_model(self) -> None:
        """Load a previously trained model from disk if one exists."""
        if not self._model_path.exists():
            return
        try:
            with self._model_path.open("rb") as fh:
                self._model = pickle.load(fh)
            if self._meta_path.exists():
                meta = json.loads(self._meta_path.read_text())
                self._trained_on_n = int(meta.get("trained_on_n", 0))
            logger.debug("Duration predictor loaded (%d training samples)", self._trained_on_n)
        except Exception as exc:
            logger.warning("Failed to load duration predictor: %s", exc)
            self._model = None
            self._trained_on_n = 0

    def _save_model(self, n_samples: int) -> None:
        """Persist the trained model and metadata to disk.

        Args:
            n_samples: Number of training samples used.
        """
        try:
            with self._model_path.open("wb") as fh:
                pickle.dump(self._model, fh)
            self._meta_path.write_text(json.dumps({"trained_on_n": n_samples, "trained_at": time.time()}))
            self._trained_on_n = n_samples
        except OSError as exc:
            logger.warning("Failed to save duration predictor: %s", exc)

    # -- training data -------------------------------------------------------

    def record_completion(
        self,
        task: Task,
        actual_duration_seconds: float,
        model: str | None = None,
    ) -> None:
        """Append a completed-task record to the training dataset.

        Call this from the task completion path for every successfully
        completed task.  Triggers auto-retrain when the dataset has grown
        enough.

        Args:
            task: The completed task.
            actual_duration_seconds: Wall-clock execution time in seconds.
            model: Effective model used (overrides ``task.model``).
        """
        if actual_duration_seconds <= 0:
            return

        complexity = task.complexity.value if hasattr(task.complexity, "value") else str(task.complexity)
        scope = task.scope.value if hasattr(task.scope, "value") else str(task.scope)
        task_type = task.task_type.value if hasattr(task.task_type, "value") else "standard"

        record = {
            "task_id": task.id,
            "role": task.role,
            "complexity": complexity,
            "scope": scope,
            "task_type": task_type,
            "priority": task.priority,
            "estimated_minutes": task.estimated_minutes,
            "description_length": len(task.description),
            "owned_files_count": len(task.owned_files),
            "depends_on_count": len(task.depends_on),
            "model": model or task.model or "sonnet",
            "actual_duration_seconds": actual_duration_seconds,
            "timestamp": time.time(),
        }
        try:
            with self._training_path.open("a") as fh:
                fh.write(json.dumps(record) + "\n")
        except OSError as exc:
            logger.warning("Failed to write duration training record: %s", exc)
            return

        self._maybe_retrain()

    def _load_training_records(self) -> list[TrainingRecord]:
        """Read all training records from the JSONL file on disk.

        Returns:
            List of :class:`TrainingRecord` objects; corrupt lines are skipped.
        """
        if not self._training_path.exists():
            return []

        records: list[TrainingRecord] = []
        try:
            for raw_line in self._training_path.read_text().splitlines():
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    records.append(
                        TrainingRecord(
                            task_id=d.get("task_id", ""),
                            role=d.get("role", "backend"),
                            complexity=d.get("complexity", "medium"),
                            scope=d.get("scope", "medium"),
                            task_type=d.get("task_type", "standard"),
                            priority=int(d.get("priority", 2)),
                            estimated_minutes=int(d.get("estimated_minutes", 30)),
                            description_length=int(d.get("description_length", 0)),
                            owned_files_count=int(d.get("owned_files_count", 0)),
                            depends_on_count=int(d.get("depends_on_count", 0)),
                            model=d.get("model", "sonnet"),
                            actual_duration_seconds=float(d["actual_duration_seconds"]),
                            timestamp=float(d.get("timestamp", 0.0)),
                        )
                    )
                except (KeyError, ValueError, json.JSONDecodeError):
                    continue
        except OSError:
            pass
        return records

    # -- training ------------------------------------------------------------

    def _maybe_retrain(self) -> None:
        """Retrain the model if the dataset has grown by the threshold."""
        records = self._load_training_records()
        n = len(records)
        if n < MIN_TRAIN_SAMPLES:
            return
        if self._trained_on_n == 0 or n >= self._trained_on_n * (1 + RETRAIN_GROWTH_THRESHOLD):
            self._train(records)

    def train(self) -> int:
        """Force retrain on all available data.

        Call this on startup to ensure the latest model is used.

        Returns:
            Number of training samples used, or 0 if below the minimum.
        """
        records = self._load_training_records()
        if len(records) < MIN_TRAIN_SAMPLES:
            logger.debug(
                "Duration predictor: only %d samples (need %d), skipping training",
                len(records),
                MIN_TRAIN_SAMPLES,
            )
            return 0
        self._train(records)
        return len(records)

    def _train(self, records: list[TrainingRecord]) -> None:
        """Fit a GradientBoostingRegressor on log-transformed durations.

        Training on log(duration) reduces the effect of extreme outliers and
        produces more useful predictions for right-skewed completion times.

        Args:
            records: Training records to fit on.
        """
        try:
            from sklearn.ensemble import GradientBoostingRegressor  # type: ignore[import-untyped]
        except ImportError:
            logger.warning("scikit-learn not installed — duration predictor disabled")
            return

        X = [self._extractor.extract_from_record(r) for r in records]
        y = [math.log(max(r.actual_duration_seconds, 1.0)) for r in records]

        try:
            gbr = GradientBoostingRegressor(
                n_estimators=100,
                max_depth=4,
                learning_rate=0.1,
                subsample=0.8,
                random_state=42,
            )
            gbr.fit(X, y)
            self._model = gbr
            self._save_model(len(records))
            logger.info("Duration predictor retrained on %d samples", len(records))
        except Exception as exc:
            logger.warning("Duration predictor training failed: %s", exc)
            self._model = None

    # -- inference -----------------------------------------------------------

    def predict(self, task: Task, model: str | None = None) -> DurationEstimate:
        """Predict p50/p90 duration for a task.

        Args:
            task: Task to predict duration for.
            model: Effective model name (overrides ``task.model``).

        Returns:
            :class:`DurationEstimate` with p50, p90, confidence, and cold_start flag.
        """
        if self._model is None:
            return self._cold_start(task)

        try:
            features = [self._extractor.extract(task, model=model)]
            log_p50 = float(self._model.predict(features)[0])
            p50 = math.exp(log_p50)

            complexity = task.complexity.value if hasattr(task.complexity, "value") else str(task.complexity)
            p90_mult = _P90_MULT.get(complexity, 2.0)
            p90 = p50 * p90_mult

            # Confidence grows from 0.5 at MIN_TRAIN_SAMPLES to 0.95 at ~1500 samples
            confidence = min(0.95, 0.5 + (self._trained_on_n - MIN_TRAIN_SAMPLES) / 1000)

            return DurationEstimate(
                p50_seconds=round(p50, 1),
                p90_seconds=round(p90, 1),
                confidence=round(confidence, 3),
                is_cold_start=False,
            )
        except Exception as exc:
            logger.debug("Duration prediction failed, using cold start: %s", exc)
            return self._cold_start(task)

    def _cold_start(self, task: Task) -> DurationEstimate:
        """Return a static scope/complexity estimate when no model is available.

        Args:
            task: Task to estimate.

        Returns:
            Conservative :class:`DurationEstimate` with confidence=0.3.
        """
        scope = task.scope.value if hasattr(task.scope, "value") else str(task.scope)
        complexity = task.complexity.value if hasattr(task.complexity, "value") else str(task.complexity)

        p50 = _COLD_START_P50.get((scope, complexity), 600.0)
        p90 = p50 * _P90_MULT.get(complexity, 2.0)

        return DurationEstimate(
            p50_seconds=p50,
            p90_seconds=p90,
            confidence=0.3,
            is_cold_start=True,
        )

    # -- utility -------------------------------------------------------------

    @property
    def training_sample_count(self) -> int:
        """Return the number of records currently on disk (without loading all)."""
        if not self._training_path.exists():
            return 0
        try:
            return sum(1 for ln in self._training_path.read_text().splitlines() if ln.strip())
        except OSError:
            return 0

    @property
    def is_trained(self) -> bool:
        """True when a trained model is in memory."""
        return self._model is not None


# ---------------------------------------------------------------------------
# Global accessor
# ---------------------------------------------------------------------------

_default_predictor: DurationPredictor | None = None


def get_predictor(models_dir: Path | None = None) -> DurationPredictor:
    """Return the process-wide :class:`DurationPredictor` instance.

    The predictor is created lazily on first call.  Pass *models_dir* on
    the first call to customise the artifact directory; subsequent calls
    ignore the argument.

    Args:
        models_dir: Directory for model artefacts.  Defaults to
            ``Path.cwd() / ".sdd" / "models"``.

    Returns:
        Singleton :class:`DurationPredictor`.
    """
    global _default_predictor
    if _default_predictor is None:
        _default_predictor = DurationPredictor(models_dir or Path.cwd() / ".sdd" / "models")
    return _default_predictor


def reset_predictor() -> None:
    """Reset the global predictor — intended for testing only."""
    global _default_predictor
    _default_predictor = None
