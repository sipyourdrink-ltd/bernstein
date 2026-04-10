"""Auto-distillation pipeline — fine-tune smaller models from successful task completions.

After N successful completions of a task type, this module collects the
(prompt, completion) pairs, groups them by role and task type, and prepares
fine-tuning batches.  Once a batch reaches the configured threshold, it is
submitted as a training job.  Completed distilled models are registered so
the router can direct similar future tasks to the cheaper model at 10-100x
lower cost.

Persistence layout under ``.sdd/distillation/``::

    examples.jsonl          — append-only log of training examples
    state.json              — mutable tracking state (batches, models, counts)

Integration:
    Called from ``process_completed_tasks()`` in ``task_completion.py`` after
    janitor verification passes.  The router queries ``get_distilled_model()``
    before falling back to the bandit or cascade router.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.metric_collector import TaskMetrics
    from bernstein.core.models import Task

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DEFAULT_BATCH_THRESHOLD = 20  # examples needed before triggering fine-tune
_DEFAULT_MAX_DESCRIPTION_CHARS = 4_000
_DEFAULT_MAX_RESULT_CHARS = 2_000
_HIGH_QUALITY_THRESHOLD = 0.8  # minimum confidence/quality to include example
_COST_REDUCTION_TARGET = 10.0  # 10x cheaper target for distilled model

# Supported fine-tuning providers
_SUPPORTED_PROVIDERS = frozenset({"openai", "anthropic", "local"})


class TrainingJobStatus(Enum):
    """Status of a fine-tuning job."""

    PREPARING = "preparing"
    SUBMITTED = "submitted"
    TRAINING = "training"
    COMPLETED = "completed"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DistillationConfig:
    """Configuration for the auto-distillation pipeline.

    Attributes:
        enabled: Master on/off switch.
        batch_threshold: Number of successful examples before triggering
            a fine-tuning batch for a given (role, task_type) key.
        max_description_chars: Truncate task description at this length.
        max_result_chars: Truncate result summary at this length.
        target_model: Base model to fine-tune (e.g. ``"gpt-5.4-mini"``).
        provider: Fine-tuning provider (``"openai"``, ``"anthropic"``, ``"local"``).
        quality_threshold: Minimum quality score to include an example
            (0.0 to 1.0; janitor-passed tasks get 1.0).
        auto_route: When True, automatically route matching tasks to
            distilled models once available.
        max_batches_per_key: Maximum concurrent training jobs per
            (role, task_type) key.
    """

    enabled: bool = False
    batch_threshold: int = _DEFAULT_BATCH_THRESHOLD
    max_description_chars: int = _DEFAULT_MAX_DESCRIPTION_CHARS
    max_result_chars: int = _DEFAULT_MAX_RESULT_CHARS
    target_model: str = "gpt-5.4-mini"
    provider: str = "openai"
    quality_threshold: float = _HIGH_QUALITY_THRESHOLD
    auto_route: bool = True
    max_batches_per_key: int = 3


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass
class DistillationExample:
    """A single training example extracted from a successful task completion.

    Attributes:
        example_id: Unique identifier (SHA-256 of task_id + timestamp).
        task_id: Source task ID.
        role: Agent role (e.g. ``"backend"``, ``"frontend"``).
        task_type: Task type string (e.g. ``"standard"``, ``"upgrade"``).
        writer_model: Model that produced the successful completion.
        task_title: Task title (input context).
        task_description: Task description (input prompt).
        result_summary: Agent's result summary (output completion).
        owned_files: Files the agent was scoped to.
        cost_usd: Cost of the original completion.
        duration_seconds: Wall-clock time for the original completion.
        quality_score: Quality score (1.0 = janitor passed, 0.0 = failed).
        timestamp: When this example was collected.
    """

    example_id: str
    task_id: str
    role: str
    task_type: str
    writer_model: str
    task_title: str
    task_description: str
    result_summary: str
    owned_files: list[str]
    cost_usd: float
    duration_seconds: float
    quality_score: float
    timestamp: float

    def distillation_key(self) -> str:
        """Return the grouping key for this example: ``role:task_type``."""
        return f"{self.role}:{self.task_type}"

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> DistillationExample:
        """Deserialise from a dict.

        Args:
            raw: Dict loaded from JSONL.

        Returns:
            Populated ``DistillationExample``.
        """
        return cls(
            example_id=str(raw["example_id"]),
            task_id=str(raw["task_id"]),
            role=str(raw["role"]),
            task_type=str(raw.get("task_type", "standard")),
            writer_model=str(raw.get("writer_model", "unknown")),
            task_title=str(raw.get("task_title", "")),
            task_description=str(raw.get("task_description", "")),
            result_summary=str(raw.get("result_summary", "")),
            owned_files=list(raw.get("owned_files", [])),
            cost_usd=float(raw.get("cost_usd", 0.0)),
            duration_seconds=float(raw.get("duration_seconds", 0.0)),
            quality_score=float(raw.get("quality_score", 0.0)),
            timestamp=float(raw.get("timestamp", 0.0)),
        )


@dataclass
class DistillationBatch:
    """A batch of examples prepared for fine-tuning.

    Attributes:
        batch_id: Unique identifier for this batch.
        distillation_key: The ``role:task_type`` key this batch covers.
        example_count: Number of examples in this batch.
        example_ids: IDs of examples included.
        status: Current status of the training job.
        provider: Fine-tuning provider used.
        target_model: Base model being fine-tuned.
        training_job_id: Provider's job ID (set after submission).
        created_at: When this batch was created.
        submitted_at: When the job was submitted (or None).
        completed_at: When training completed (or None).
        finetuned_model: Provider model name of the result (or None).
        error: Error message if the job failed.
    """

    batch_id: str
    distillation_key: str
    example_count: int
    example_ids: list[str]
    status: TrainingJobStatus
    provider: str
    target_model: str
    training_job_id: str | None = None
    created_at: float = 0.0
    submitted_at: float | None = None
    completed_at: float | None = None
    finetuned_model: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        d = asdict(self)
        d["status"] = self.status.value
        return d

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> DistillationBatch:
        """Deserialise from a dict.

        Args:
            raw: Dict loaded from state JSON.

        Returns:
            Populated ``DistillationBatch``.
        """
        return cls(
            batch_id=str(raw["batch_id"]),
            distillation_key=str(raw["distillation_key"]),
            example_count=int(raw.get("example_count", 0)),
            example_ids=list(raw.get("example_ids", [])),
            status=TrainingJobStatus(raw.get("status", "preparing")),
            provider=str(raw.get("provider", "openai")),
            target_model=str(raw.get("target_model", "")),
            training_job_id=raw.get("training_job_id"),
            created_at=float(raw.get("created_at", 0.0)),
            submitted_at=raw.get("submitted_at"),
            completed_at=raw.get("completed_at"),
            finetuned_model=raw.get("finetuned_model"),
            error=raw.get("error"),
        )


@dataclass
class DistilledModel:
    """A registered distilled model available for routing.

    Attributes:
        model_name: Provider model name (e.g. ``"ft:gpt-5.4-mini:...:backend"``).
        distillation_key: The ``role:task_type`` key this model serves.
        base_model: The base model it was fine-tuned from.
        batch_id: The training batch that produced it.
        registered_at: When this model was registered for routing.
        tasks_routed: Number of tasks routed to this model so far.
        tasks_succeeded: Number of successful completions on this model.
        avg_cost_usd: Rolling average cost per task.
        active: Whether this model is currently in the routing pool.
    """

    model_name: str
    distillation_key: str
    base_model: str
    batch_id: str
    registered_at: float
    tasks_routed: int = 0
    tasks_succeeded: int = 0
    avg_cost_usd: float = 0.0
    active: bool = True

    @property
    def success_rate(self) -> float:
        """Fraction of routed tasks that succeeded."""
        if self.tasks_routed == 0:
            return 0.0
        return self.tasks_succeeded / self.tasks_routed

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return asdict(self)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> DistilledModel:
        """Deserialise from a dict."""
        return cls(
            model_name=str(raw["model_name"]),
            distillation_key=str(raw["distillation_key"]),
            base_model=str(raw.get("base_model", "")),
            batch_id=str(raw.get("batch_id", "")),
            registered_at=float(raw.get("registered_at", 0.0)),
            tasks_routed=int(raw.get("tasks_routed", 0)),
            tasks_succeeded=int(raw.get("tasks_succeeded", 0)),
            avg_cost_usd=float(raw.get("avg_cost_usd", 0.0)),
            active=bool(raw.get("active", True)),
        )


@dataclass
class DistillationStats:
    """Summary statistics for the distillation pipeline.

    Attributes:
        total_examples: Total examples collected across all keys.
        examples_per_key: Count of examples per distillation key.
        active_batches: Number of batches currently training.
        completed_batches: Number of batches that finished training.
        active_models: Number of distilled models in the routing pool.
        total_tasks_routed: Total tasks routed to distilled models.
        estimated_savings_usd: Estimated cost savings from distillation.
    """

    total_examples: int = 0
    examples_per_key: dict[str, int] = field(default_factory=dict[str, int])
    active_batches: int = 0
    completed_batches: int = 0
    active_models: int = 0
    total_tasks_routed: int = 0
    estimated_savings_usd: float = 0.0


# ---------------------------------------------------------------------------
# AutoDistiller
# ---------------------------------------------------------------------------


class AutoDistiller:
    """Manages the auto-distillation pipeline lifecycle.

    Collects training examples from successful task completions, prepares
    fine-tuning batches when thresholds are met, tracks training jobs, and
    registers completed models for routing.

    State is persisted to ``distill_dir/state.json`` and examples are
    appended to ``distill_dir/examples.jsonl``.

    Args:
        config: Distillation configuration.
        distill_dir: Directory for persistence (``.sdd/distillation``).
    """

    EXAMPLES_FILE = "examples.jsonl"
    STATE_FILE = "state.json"

    def __init__(self, config: DistillationConfig, distill_dir: Path | None = None) -> None:
        self._config = config
        self._distill_dir = distill_dir
        self._examples_per_key: dict[str, int] = {}
        self._batches: dict[str, DistillationBatch] = {}
        self._models: dict[str, DistilledModel] = {}  # keyed by distillation_key
        self._total_examples: int = 0
        self._loaded: bool = False

    # ------------------------------------------------------------------
    # Public properties
    # ------------------------------------------------------------------

    @property
    def config(self) -> DistillationConfig:
        """Current configuration."""
        return self._config

    # ------------------------------------------------------------------
    # Example collection
    # ------------------------------------------------------------------

    def collect_example(
        self,
        task: Task,
        *,
        writer_model: str,
        cost_usd: float,
        duration_seconds: float,
        quality_score: float,
        task_metrics: TaskMetrics | None = None,
    ) -> DistillationExample | None:
        """Collect a training example from a successful task completion.

        Only collects if the quality score meets the configured threshold
        and the pipeline is enabled.

        Args:
            task: The completed task.
            writer_model: Model that produced the completion.
            cost_usd: Cost of the original completion.
            duration_seconds: Wall-clock time for the completion.
            quality_score: Quality score (1.0 = janitor passed).
            task_metrics: Optional metrics for additional context.

        Returns:
            The collected ``DistillationExample``, or None if skipped.
        """
        if not self._config.enabled:
            return None

        self._ensure_loaded()

        if quality_score < self._config.quality_threshold:
            logger.debug(
                "auto_distillation: skipping task %s — quality %.2f < threshold %.2f",
                task.id,
                quality_score,
                self._config.quality_threshold,
            )
            return None

        if not task.result_summary:
            logger.debug("auto_distillation: skipping task %s — no result_summary", task.id)
            return None

        now = time.time()
        example_id = hashlib.sha256(f"{task.id}:{now}".encode()).hexdigest()[:16]

        example = DistillationExample(
            example_id=example_id,
            task_id=task.id,
            role=task.role,
            task_type=task.task_type.value,
            writer_model=writer_model,
            task_title=task.title,
            task_description=task.description[: self._config.max_description_chars],
            result_summary=task.result_summary[: self._config.max_result_chars],
            owned_files=task.owned_files,
            cost_usd=cost_usd,
            duration_seconds=duration_seconds,
            quality_score=quality_score,
            timestamp=now,
        )

        key = example.distillation_key()
        self._examples_per_key[key] = self._examples_per_key.get(key, 0) + 1
        self._total_examples += 1

        self._append_example(example)
        self._save_state()

        logger.info(
            "auto_distillation: collected example %s for key %r (total=%d, key_count=%d/%d)",
            example.example_id,
            key,
            self._total_examples,
            self._examples_per_key[key],
            self._config.batch_threshold,
        )

        return example

    def should_trigger_batch(self, key: str) -> bool:
        """Check if a distillation key has enough examples for a batch.

        Args:
            key: Distillation key (``role:task_type``).

        Returns:
            True if the example count meets the batch threshold and no
            active batch exists for this key.
        """
        self._ensure_loaded()
        count = self._examples_per_key.get(key, 0)
        if count < self._config.batch_threshold:
            return False

        # Check if we already have too many active batches for this key
        active_for_key = sum(
            1
            for b in self._batches.values()
            if b.distillation_key == key
            and b.status in (TrainingJobStatus.PREPARING, TrainingJobStatus.SUBMITTED, TrainingJobStatus.TRAINING)
        )
        return active_for_key < self._config.max_batches_per_key

    # ------------------------------------------------------------------
    # Batch management
    # ------------------------------------------------------------------

    def prepare_batch(self, key: str) -> DistillationBatch | None:
        """Prepare a fine-tuning batch for a distillation key.

        Reads the most recent ``batch_threshold`` examples for this key
        from the JSONL file and creates a batch record.

        Args:
            key: Distillation key (``role:task_type``).

        Returns:
            The prepared ``DistillationBatch``, or None if not enough
            examples or the pipeline is disabled.
        """
        if not self._config.enabled:
            return None

        self._ensure_loaded()

        if not self.should_trigger_batch(key):
            return None

        examples = self._load_examples_for_key(key, limit=self._config.batch_threshold)
        if len(examples) < self._config.batch_threshold:
            return None

        now = time.time()
        batch_id = hashlib.sha256(f"{key}:{now}".encode()).hexdigest()[:12]

        batch = DistillationBatch(
            batch_id=batch_id,
            distillation_key=key,
            example_count=len(examples),
            example_ids=[e.example_id for e in examples],
            status=TrainingJobStatus.PREPARING,
            provider=self._config.provider,
            target_model=self._config.target_model,
            created_at=now,
        )

        self._batches[batch_id] = batch
        # Reset counter so we collect fresh examples for the next batch
        self._examples_per_key[key] = 0
        self._save_state()

        logger.info(
            "auto_distillation: prepared batch %s for key %r (%d examples)",
            batch_id,
            key,
            len(examples),
        )
        return batch

    def format_training_data(self, batch: DistillationBatch) -> list[dict[str, Any]]:
        """Format batch examples into provider-specific training data.

        Produces OpenAI chat-completion fine-tuning format by default.
        Each example becomes a (system, user, assistant) message triple.

        Args:
            batch: The batch to format.

        Returns:
            List of training records in provider format.
        """
        examples = self._load_examples_by_ids(batch.example_ids)
        records: list[dict[str, Any]] = []

        for ex in examples:
            role_prompt = f"You are a {ex.role} agent working on a software project."
            user_prompt = (
                f"Task: {ex.task_title}\n\n"
                f"Description:\n{ex.task_description}\n\n"
                f"Files in scope: {', '.join(ex.owned_files) if ex.owned_files else 'any'}"
            )
            records.append(
                {
                    "messages": [
                        {"role": "system", "content": role_prompt},
                        {"role": "user", "content": user_prompt},
                        {"role": "assistant", "content": ex.result_summary},
                    ]
                }
            )

        return records

    def mark_batch_submitted(
        self,
        batch_id: str,
        training_job_id: str,
    ) -> None:
        """Mark a batch as submitted to the training provider.

        Args:
            batch_id: The batch to update.
            training_job_id: Provider's job ID.
        """
        self._ensure_loaded()
        batch = self._batches.get(batch_id)
        if batch is None:
            logger.warning("auto_distillation: unknown batch %s", batch_id)
            return
        batch.status = TrainingJobStatus.SUBMITTED
        batch.training_job_id = training_job_id
        batch.submitted_at = time.time()
        self._save_state()
        logger.info(
            "auto_distillation: batch %s submitted as job %s",
            batch_id,
            training_job_id,
        )

    def mark_batch_completed(
        self,
        batch_id: str,
        finetuned_model: str,
    ) -> DistilledModel | None:
        """Mark a batch as completed and register the distilled model.

        Args:
            batch_id: The batch that completed training.
            finetuned_model: Provider model name of the result.

        Returns:
            The registered ``DistilledModel``, or None on error.
        """
        self._ensure_loaded()
        batch = self._batches.get(batch_id)
        if batch is None:
            logger.warning("auto_distillation: unknown batch %s", batch_id)
            return None

        batch.status = TrainingJobStatus.COMPLETED
        batch.completed_at = time.time()
        batch.finetuned_model = finetuned_model

        model = self._register_model(
            model_name=finetuned_model,
            distillation_key=batch.distillation_key,
            base_model=batch.target_model,
            batch_id=batch_id,
        )
        self._save_state()
        return model

    def mark_batch_failed(self, batch_id: str, error: str) -> None:
        """Mark a batch as failed.

        Args:
            batch_id: The failed batch.
            error: Error message.
        """
        self._ensure_loaded()
        batch = self._batches.get(batch_id)
        if batch is None:
            return
        batch.status = TrainingJobStatus.FAILED
        batch.error = error
        batch.completed_at = time.time()
        self._save_state()
        logger.warning("auto_distillation: batch %s failed: %s", batch_id, error)

    # ------------------------------------------------------------------
    # Model routing
    # ------------------------------------------------------------------

    def get_distilled_model(self, role: str, task_type: str) -> str | None:
        """Look up a distilled model for routing.

        Returns the active distilled model for the given role and task
        type, if one exists and auto-routing is enabled.

        Args:
            role: Agent role.
            task_type: Task type string.

        Returns:
            Model name string, or None if no distilled model is available.
        """
        if not self._config.enabled or not self._config.auto_route:
            return None

        self._ensure_loaded()
        key = f"{role}:{task_type}"
        model = self._models.get(key)
        if model is None or not model.active:
            return None

        # Deactivate if success rate drops too low (after enough samples)
        if model.tasks_routed >= 10 and model.success_rate < 0.6:
            logger.warning(
                "auto_distillation: deactivating model %s — success rate %.1f%% < 60%%",
                model.model_name,
                model.success_rate * 100,
            )
            model.active = False
            self._save_state()
            return None

        return model.model_name

    def record_distilled_outcome(
        self,
        role: str,
        task_type: str,
        success: bool,
        cost_usd: float,
    ) -> None:
        """Record the outcome of a task routed to a distilled model.

        Updates the model's running statistics for quality monitoring.

        Args:
            role: Agent role.
            task_type: Task type string.
            success: Whether the task passed janitor verification.
            cost_usd: Cost incurred for this task.
        """
        self._ensure_loaded()
        key = f"{role}:{task_type}"
        model = self._models.get(key)
        if model is None:
            return

        model.tasks_routed += 1
        if success:
            model.tasks_succeeded += 1

        # Rolling average cost
        n = model.tasks_routed
        model.avg_cost_usd = model.avg_cost_usd * ((n - 1) / n) + cost_usd / n
        self._save_state()

    # ------------------------------------------------------------------
    # Statistics & reporting
    # ------------------------------------------------------------------

    def stats(self) -> DistillationStats:
        """Return summary statistics for the distillation pipeline.

        Returns:
            ``DistillationStats`` with counts and savings estimates.
        """
        self._ensure_loaded()
        active_batches = sum(
            1
            for b in self._batches.values()
            if b.status in (TrainingJobStatus.PREPARING, TrainingJobStatus.SUBMITTED, TrainingJobStatus.TRAINING)
        )
        completed_batches = sum(1 for b in self._batches.values() if b.status == TrainingJobStatus.COMPLETED)

        active_models = sum(1 for m in self._models.values() if m.active)
        total_routed = sum(m.tasks_routed for m in self._models.values())
        total_savings = sum(
            m.avg_cost_usd * m.tasks_routed * (_COST_REDUCTION_TARGET - 1) / _COST_REDUCTION_TARGET
            for m in self._models.values()
            if m.tasks_routed > 0
        )

        return DistillationStats(
            total_examples=self._total_examples,
            examples_per_key=dict(self._examples_per_key),
            active_batches=active_batches,
            completed_batches=completed_batches,
            active_models=active_models,
            total_tasks_routed=total_routed,
            estimated_savings_usd=round(total_savings, 4),
        )

    def summary(self) -> dict[str, Any]:
        """Return a dict suitable for dashboard display.

        Returns:
            Dict with pipeline status, counts, and model info.
        """
        self._ensure_loaded()
        s = self.stats()
        models_info: list[dict[str, Any]] = []
        for m in self._models.values():
            models_info.append(
                {
                    "model_name": m.model_name,
                    "key": m.distillation_key,
                    "active": m.active,
                    "tasks_routed": m.tasks_routed,
                    "success_rate": round(m.success_rate, 3),
                    "avg_cost_usd": round(m.avg_cost_usd, 6),
                }
            )
        return {
            "enabled": self._config.enabled,
            "total_examples": s.total_examples,
            "examples_per_key": s.examples_per_key,
            "batch_threshold": self._config.batch_threshold,
            "active_batches": s.active_batches,
            "completed_batches": s.completed_batches,
            "active_models": s.active_models,
            "total_tasks_routed": s.total_tasks_routed,
            "estimated_savings_usd": s.estimated_savings_usd,
            "models": models_info,
        }

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    def save(self) -> None:
        """Persist state to disk.  No-op if no ``distill_dir`` was set."""
        self._save_state()

    def _save_state(self) -> None:
        """Write state.json with batches, models, and example counts."""
        if self._distill_dir is None:
            return
        try:
            self._distill_dir.mkdir(parents=True, exist_ok=True)
            state: dict[str, Any] = {
                "total_examples": self._total_examples,
                "examples_per_key": self._examples_per_key,
                "batches": {bid: b.to_dict() for bid, b in self._batches.items()},
                "models": {key: m.to_dict() for key, m in self._models.items()},
                "saved_at": time.time(),
            }
            state_path = self._distill_dir / self.STATE_FILE
            state_path.write_text(json.dumps(state, indent=2))
        except OSError as exc:
            logger.warning("auto_distillation: could not save state: %s", exc)

    def _append_example(self, example: DistillationExample) -> None:
        """Append a single example to the JSONL file."""
        if self._distill_dir is None:
            return
        try:
            self._distill_dir.mkdir(parents=True, exist_ok=True)
            path = self._distill_dir / self.EXAMPLES_FILE
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(example.to_dict()) + "\n")
        except OSError as exc:
            logger.warning("auto_distillation: could not append example: %s", exc)

    def _ensure_loaded(self) -> None:
        """Lazy-load state from disk on first access."""
        if self._loaded:
            return
        self._loaded = True

        if self._distill_dir is None:
            return

        state_path = self._distill_dir / self.STATE_FILE
        if not state_path.exists():
            return

        try:
            state = json.loads(state_path.read_text())
            self._total_examples = int(state.get("total_examples", 0))
            self._examples_per_key = {str(k): int(v) for k, v in state.get("examples_per_key", {}).items()}
            for bid, bdata in state.get("batches", {}).items():
                self._batches[str(bid)] = DistillationBatch.from_dict(bdata)
            for key, mdata in state.get("models", {}).items():
                self._models[str(key)] = DistilledModel.from_dict(mdata)
        except Exception as exc:
            logger.warning("auto_distillation: could not load state from %s: %s", state_path, exc)

    def _load_examples_for_key(
        self,
        key: str,
        limit: int,
    ) -> list[DistillationExample]:
        """Load the most recent examples for a distillation key from JSONL.

        Args:
            key: Distillation key (``role:task_type``).
            limit: Maximum number of examples to return.

        Returns:
            List of examples, most recent first, up to ``limit``.
        """
        if self._distill_dir is None:
            return []

        path = self._distill_dir / self.EXAMPLES_FILE
        if not path.exists():
            return []

        matching: list[DistillationExample] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                raw = json.loads(line)
                ex = DistillationExample.from_dict(raw)
                if ex.distillation_key() == key:
                    matching.append(ex)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("auto_distillation: error reading examples: %s", exc)

        # Return the most recent examples up to limit
        matching.sort(key=lambda e: e.timestamp, reverse=True)
        return matching[:limit]

    def _load_examples_by_ids(
        self,
        example_ids: list[str],
    ) -> list[DistillationExample]:
        """Load specific examples by their IDs from JSONL.

        Args:
            example_ids: List of example IDs to load.

        Returns:
            List of matching examples.
        """
        if self._distill_dir is None:
            return []

        path = self._distill_dir / self.EXAMPLES_FILE
        if not path.exists():
            return []

        id_set = set(example_ids)
        results: list[DistillationExample] = []
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                raw = json.loads(line)
                if raw.get("example_id") in id_set:
                    results.append(DistillationExample.from_dict(raw))
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("auto_distillation: error reading examples by ID: %s", exc)

        return results

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _register_model(
        self,
        model_name: str,
        distillation_key: str,
        base_model: str,
        batch_id: str,
    ) -> DistilledModel:
        """Register a fine-tuned model in the routing table.

        If a model already exists for this key, the new one replaces it
        (newer fine-tune supersedes older).

        Args:
            model_name: Provider model name.
            distillation_key: The ``role:task_type`` key.
            base_model: Base model it was fine-tuned from.
            batch_id: Training batch ID.

        Returns:
            The registered ``DistilledModel``.
        """
        model = DistilledModel(
            model_name=model_name,
            distillation_key=distillation_key,
            base_model=base_model,
            batch_id=batch_id,
            registered_at=time.time(),
        )
        # Deactivate previous model for this key (if any)
        old = self._models.get(distillation_key)
        if old is not None:
            logger.info(
                "auto_distillation: replacing model %s with %s for key %r",
                old.model_name,
                model_name,
                distillation_key,
            )
            old.active = False

        self._models[distillation_key] = model
        logger.info(
            "auto_distillation: registered distilled model %s for key %r",
            model_name,
            distillation_key,
        )
        return model
