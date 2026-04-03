"""Intelligent cost optimization engine.

Provides:
- EpsilonGreedyBandit: learns which model gives best ROI per task type
- ModelCascade: provides cascade config (cheapest viable → escalate on failure)
- Cost projection utilities for ``bernstein cost``

The bandit tracks per-(role, model) arms.  After MIN_OBSERVATIONS it converges
on the cheapest model whose success_rate >= QUALITY_THRESHOLD.  Until then it
explores with probability EPSILON.

Cascade order (cheapest → most expensive):
    haiku  →  sonnet  →  opus

State is persisted to ``.sdd/metrics/bandit_state.json`` so it survives
orchestrator restarts.
"""

from __future__ import annotations

import json
import logging
import random
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, TypedDict

from bernstein.core.models import Complexity, Scope, Task, TaskStatus

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

EPSILON: float = 0.1  # 10% explore, 90% exploit
MIN_OBSERVATIONS: int = 5  # arms trusted only after this many samples
QUALITY_THRESHOLD: float = 0.80  # minimum success_rate to consider an arm


class ModelUsdPer1MTokens(TypedDict, total=False):
    """USD per 1 million tokens (list prices, approximate)."""

    input: float
    output: float
    cache_read: float | None
    cache_write: float | None


# Per-model input/output pricing per 1M tokens (USD). Keys match substring checks in ``_model_cost``.
# Updated 2026-03-28 from official API pricing pages.
MODEL_COSTS_PER_1M_TOKENS: dict[str, ModelUsdPer1MTokens] = {
    "haiku": {"input": 1.0, "output": 5.0, "cache_read": 0.1, "cache_write": 1.25},
    "sonnet": {"input": 3.0, "output": 15.0, "cache_read": 0.3, "cache_write": 3.75},
    "opus": {"input": 5.0, "output": 25.0, "cache_read": 0.5, "cache_write": 6.25},
    "gpt-5.4": {"input": 2.5, "output": 15.0},
    "gpt-5.4-mini": {"input": 0.75, "output": 4.5},
    "o3": {"input": 2.0, "output": 8.0},
    "o4-mini": {"input": 1.1, "output": 4.4},
    "gemini-3": {"input": 3.0, "output": 15.0, "cache_read": 0.1},
    "gemini-2.5-pro": {"input": 1.25, "output": 10.0, "cache_read": 0.05},
    "gemini-2.5-flash": {"input": 0.3, "output": 2.5, "cache_read": 0.01},
    "gemini-3-flash": {"input": 0.5, "output": 3.0, "cache_read": 0.02},
    "qwen3-coder": {"input": 0.22, "output": 0.9},
    # Blended-only entries in ``_MODEL_COST_USD_PER_1K`` — approximate 40/60 input/output split of total $/1M.
    "qwen-max": {"input": 0.8, "output": 1.2},
    "qwen-plus": {"input": 0.4, "output": 0.6},
    "qwen-turbo": {"input": 0.16, "output": 0.24},
}

# Approximate cost per 1k tokens (input+output blended average), USD.
# Updated 2026-03-28 from official API pricing pages.
# Used only as a tiebreaker when multiple arms meet the quality threshold.
_MODEL_COST_USD_PER_1K: dict[str, float] = {
    # Claude (Anthropic) — per 1M tokens: Opus $5/$25, Sonnet $3/$15, Haiku $1/$5
    "haiku": 0.003,  # ($1 + $5) / 2 / 1000
    "sonnet": 0.009,  # ($3 + $15) / 2 / 1000
    "opus": 0.015,  # ($5 + $25) / 2 / 1000
    # OpenAI — per 1M tokens: GPT-5.4 $2.50/$15, o3 $2/$8, o4-mini $1.10/$4.40
    "gpt-5.4": 0.00875,  # ($2.50 + $15) / 2 / 1000
    "gpt-5.4-mini": 0.002625,  # ($0.75 + $4.50) / 2 / 1000
    "o3": 0.005,  # ($2 + $8) / 2 / 1000
    "o4-mini": 0.00275,  # ($1.10 + $4.40) / 2 / 1000
    # Gemini (Google) — per 1M tokens: 3-pro ~$3/$15, 3-flash $0.50/$3, 2.5-pro $1.25/$10
    "gemini-3": 0.009,  # ($3 + $15) / 2 / 1000
    "gemini-2.5-pro": 0.005625,  # ($1.25 + $10) / 2 / 1000
    "gemini-2.5-flash": 0.0014,  # ($0.30 + $2.50) / 2 / 1000
    "gemini-3-flash": 0.00175,  # ($0.50 + $3) / 2 / 1000
    # Qwen — open-weight, very cheap via API
    "qwen3-coder": 0.00056,  # ($0.22 + $0.90) / 2 / 1000
    "qwen-max": 0.001,
    "qwen-plus": 0.0005,
    "qwen-turbo": 0.0002,
}

# Cascade order — sonnet first (haiku removed: on Max plan sonnet is
# unlimited and produces much better results)
CASCADE: list[str] = ["sonnet", "opus"]


def _model_cost(model: str) -> float:
    """Rough cost per 1k tokens for a model name."""
    model_lower = model.lower()
    for key, cost in _MODEL_COST_USD_PER_1K.items():
        if key in model_lower:
            return cost
    return 0.005  # safe unknown default


# ---------------------------------------------------------------------------
# Bandit state
# ---------------------------------------------------------------------------


@dataclass
class BanditArm:
    """Single (role, model) arm tracked by the bandit."""

    role: str
    model: str
    observations: int = 0
    successes: int = 0
    total_cost_usd: float = 0.0
    total_latency_s: float = 0.0

    @property
    def success_rate(self) -> float:
        if self.observations == 0:
            return 1.0  # optimistic initialisation
        return self.successes / self.observations

    @property
    def avg_cost_usd(self) -> float:
        if self.observations == 0:
            return _model_cost(self.model) * 100  # rough estimate
        return self.total_cost_usd / self.observations

    @property
    def avg_latency_s(self) -> float:
        if self.observations == 0:
            return 0.0
        return self.total_latency_s / self.observations

    def record(self, success: bool, cost_usd: float = 0.0, latency_s: float = 0.0) -> None:
        self.observations += 1
        if success:
            self.successes += 1
        self.total_cost_usd += cost_usd
        self.total_latency_s += latency_s

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "model": self.model,
            "observations": self.observations,
            "successes": self.successes,
            "total_cost_usd": self.total_cost_usd,
            "total_latency_s": self.total_latency_s,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> BanditArm:
        return cls(
            role=d["role"],
            model=d["model"],
            observations=d.get("observations", 0),
            successes=d.get("successes", 0),
            total_cost_usd=d.get("total_cost_usd", 0.0),
            total_latency_s=d.get("total_latency_s", 0.0),
        )


class EpsilonGreedyBandit:
    """Epsilon-greedy multi-armed bandit for model selection per task role.

    Explores with probability ``epsilon`` and exploits (picks cheapest arm
    meeting the quality threshold) the rest of the time.  Arms with fewer
    than ``min_observations`` are always considered for exploration.

    Usage::

        bandit = EpsilonGreedyBandit.load(workdir / ".sdd" / "metrics")
        model = bandit.select(role="backend")
        ...
        bandit.record(role="backend", model=model, success=True, cost_usd=0.05)
        bandit.save(workdir / ".sdd" / "metrics")
    """

    STATE_FILE = "bandit_state.json"

    def __init__(
        self,
        epsilon: float = EPSILON,
        min_observations: int = MIN_OBSERVATIONS,
        quality_threshold: float = QUALITY_THRESHOLD,
    ) -> None:
        self.epsilon = epsilon
        self.min_observations = min_observations
        self.quality_threshold = quality_threshold
        # key: (role, model) → BanditArm
        self._arms: dict[tuple[str, str], BanditArm] = {}

    # ------------------------------------------------------------------
    # Persistence
    # ------------------------------------------------------------------

    @classmethod
    def load(cls, metrics_dir: Path) -> EpsilonGreedyBandit:
        """Load bandit state from disk, returning a fresh instance on error."""
        bandit = cls()
        state_path = metrics_dir / cls.STATE_FILE
        if not state_path.exists():
            return bandit
        try:
            data = json.loads(state_path.read_text())
            for arm_dict in data.get("arms", []):
                arm = BanditArm.from_dict(arm_dict)
                bandit._arms[(arm.role, arm.model)] = arm
        except Exception as exc:
            logger.warning("Could not load bandit state from %s: %s", state_path, exc)
        return bandit

    def save(self, metrics_dir: Path) -> None:
        """Persist bandit state to disk."""
        state_path = metrics_dir / self.STATE_FILE
        try:
            metrics_dir.mkdir(parents=True, exist_ok=True)
            data = {"arms": [arm.to_dict() for arm in self._arms.values()]}
            state_path.write_text(json.dumps(data, indent=2))
        except Exception as exc:
            logger.warning("Could not save bandit state to %s: %s", state_path, exc)

    # ------------------------------------------------------------------
    # Core API
    # ------------------------------------------------------------------

    def select(self, role: str, candidate_models: list[str] | None = None) -> str:
        """Select a model for a given role using epsilon-greedy strategy.

        Args:
            role: Task role (e.g. "backend", "qa").
            candidate_models: If provided, restrict selection to these models.
                              Defaults to the full CASCADE list.

        Returns:
            Model name string (e.g. "haiku", "sonnet", "opus").
        """
        models = candidate_models if candidate_models else list(CASCADE)

        # Exploration: random choice with probability epsilon
        if random.random() < self.epsilon:
            chosen = random.choice(models)
            logger.debug("Bandit[%s]: explore → %s", role, chosen)
            return chosen

        # Exploitation: pick cheapest arm that meets quality threshold
        # If arm hasn't been sufficiently observed, treat it as a candidate too
        qualifying: list[tuple[str, float]] = []  # (model, avg_cost)
        for model in models:
            arm = self._arms.get((role, model))
            if arm is None or arm.observations < self.min_observations:
                # Unknown/under-observed → include as candidate with nominal cost
                qualifying.append((model, _model_cost(model)))
            elif arm.success_rate >= self.quality_threshold:
                qualifying.append((model, arm.avg_cost_usd))

        if not qualifying:
            # All arms are under-performing — fall back to cheapest model to
            # keep trying (cascade will escalate on actual failures)
            fallback = min(models, key=_model_cost)
            logger.debug("Bandit[%s]: all arms under-threshold, fallback → %s", role, fallback)
            return fallback

        # Among qualifying arms, pick the cheapest
        chosen = min(qualifying, key=lambda t: t[1])[0]
        logger.debug("Bandit[%s]: exploit → %s (cost=%.5f)", role, chosen, dict(qualifying)[chosen])
        return chosen

    def record(
        self,
        role: str,
        model: str,
        success: bool,
        cost_usd: float = 0.0,
        latency_s: float = 0.0,
    ) -> None:
        """Record an observation for a (role, model) arm.

        Args:
            role: Task role.
            model: Model used.
            success: Whether the task succeeded (including janitor pass).
            cost_usd: Actual USD cost incurred.
            latency_s: Task duration in seconds.
        """
        key = (role, model)
        if key not in self._arms:
            self._arms[key] = BanditArm(role=role, model=model)
        self._arms[key].record(success=success, cost_usd=cost_usd, latency_s=latency_s)
        logger.debug(
            "Bandit[%s/%s]: recorded success=%s, cost=%.5f — arm now: obs=%d, success_rate=%.2f",
            role,
            model,
            success,
            cost_usd,
            self._arms[key].observations,
            self._arms[key].success_rate,
        )

    def summary(self) -> list[dict[str, Any]]:
        """Return a summary of all arm statistics, sorted by role then cost."""
        rows: list[dict[str, Any]] = []
        for arm in sorted(self._arms.values(), key=lambda a: (a.role, _model_cost(a.model))):
            rows.append(
                {
                    "role": arm.role,
                    "model": arm.model,
                    "observations": arm.observations,
                    "success_rate": round(arm.success_rate, 3),
                    "avg_cost_usd": round(arm.avg_cost_usd, 6),
                    "avg_latency_s": round(arm.avg_latency_s, 1),
                    "trusted": arm.observations >= self.min_observations,
                    "meets_quality": arm.success_rate >= self.quality_threshold,
                }
            )
        return rows

    def get_arm(self, role: str, model: str) -> BanditArm | None:
        """Return the recorded arm state for a role/model pair, if available."""
        return self._arms.get((role, model))


# ---------------------------------------------------------------------------
# Model cascade
# ---------------------------------------------------------------------------


def get_cascade_model(task: Task, retry_count: int = 0) -> str:
    """Return the appropriate cascade model for a task given its retry count.

    Cascade: haiku (0) → sonnet (1) → opus (2+).
    High-complexity or large-scope tasks skip haiku.
    Manager/architect/security roles skip straight to sonnet or opus.

    Args:
        task: The task to route.
        retry_count: Number of previous failures for this task.

    Returns:
        Model name string.
    """
    # High-stakes roles always start at sonnet or opus
    if (
        task.role in ("manager", "architect", "security")
        or task.complexity == Complexity.HIGH
        or task.scope == Scope.LARGE
        or task.priority == 1
    ):
        cascade = ["sonnet", "opus"]
    else:
        cascade = list(CASCADE)  # ["haiku", "sonnet", "opus"]

    idx = min(retry_count, len(cascade) - 1)
    return cascade[idx]


# ---------------------------------------------------------------------------
# Cost projection utilities
# ---------------------------------------------------------------------------


def _days_in_window(records: list[dict[str, Any]], days: int) -> list[dict[str, Any]]:  # type: ignore[reportUnusedFunction]
    """Filter records to those within the last N days."""
    cutoff = time.time() - days * 86400
    return [r for r in records if r.get("timestamp", 0) >= cutoff]


def compute_savings_vs_opus(records: list[dict[str, Any]]) -> float:
    """Estimate savings vs a hypothetical all-Opus baseline.

    For each task that completed with a model cheaper than Opus, estimate
    what Opus would have cost and compute the delta.

    Args:
        records: Task metric records from tasks.jsonl.

    Returns:
        Total estimated savings in USD.
    """
    opus_cost_per_1k = _MODEL_COST_USD_PER_1K["opus"]
    savings = 0.0
    for rec in records:
        actual_cost = float(rec.get("cost_usd", 0.0) or 0.0)
        tokens_in = int(rec.get("tokens_prompt", 0) or 0)
        tokens_out = int(rec.get("tokens_completion", 0) or 0)
        total_tokens = tokens_in + tokens_out
        model = (rec.get("model") or "").lower()
        if total_tokens > 0 and "opus" not in model and model != "fast-path":
            opus_estimated = (total_tokens / 1000) * opus_cost_per_1k
            savings += max(opus_estimated - actual_cost, 0.0)
    return savings


def compute_savings_vs_manual(records: list[dict[str, Any]], hourly_rate: float = 100.0) -> dict[str, float]:
    """Estimate savings vs manual coding.

    Calculates: estimated_manual_hours * hourly_rate - api_cost.

    Args:
        records: Task metric records from tasks.jsonl.
        hourly_rate: Hourly rate for manual coding in USD.

    Returns:
        Dict with manual_hours, manual_cost_usd, api_cost_usd, and savings_usd.
    """
    manual_hours = 0.0
    api_cost = 0.0
    for rec in records:
        api_cost += float(rec.get("cost_usd", 0.0) or 0.0)
        # Check if explicitly recorded, otherwise estimate based on scope
        recorded_hours = float(rec.get("estimated_manual_hours", 0.0) or 0.0)
        if recorded_hours <= 0:
            scope = str(rec.get("scope", "medium")).lower()
            if scope == "small":
                recorded_hours = 0.5  # 30 mins
            elif scope == "large":
                recorded_hours = 4.0  # 4 hours
            else:
                recorded_hours = 1.5  # 1.5 hours
        manual_hours += recorded_hours

    manual_cost = manual_hours * hourly_rate
    savings = max(0.0, manual_cost - api_cost)
    return {
        "manual_hours": round(manual_hours, 1),
        "manual_cost_usd": round(manual_cost, 2),
        "api_cost_usd": round(api_cost, 4),
        "savings_usd": round(savings, 2),
    }


def compute_daily_cost(records: list[dict[str, Any]], days: int = 7) -> list[dict[str, Any]]:
    """Compute per-day cost totals for the last N days.

    Args:
        records: Task metric records.
        days: Number of days to include.

    Returns:
        List of dicts with ``date`` (YYYY-MM-DD) and ``cost_usd``, sorted ascending.
    """
    cutoff = time.time() - days * 86400
    daily: dict[str, float] = {}
    for rec in records:
        ts = rec.get("timestamp", 0.0)
        if ts < cutoff:
            continue
        cost = float(rec.get("cost_usd", 0.0) or 0.0)
        date_str = time.strftime("%Y-%m-%d", time.localtime(ts))
        daily[date_str] = daily.get(date_str, 0.0) + cost
    return [{"date": d, "cost_usd": round(c, 6)} for d, c in sorted(daily.items())]


def project_monthly_cost(records: list[dict[str, Any]], window_days: int = 7) -> float:
    """Project monthly cost based on recent daily spend.

    Args:
        records: All task metric records.
        window_days: Number of recent days to base projection on.

    Returns:
        Projected 30-day cost in USD.
    """
    daily = compute_daily_cost(records, days=window_days)
    if not daily:
        return 0.0
    avg_daily = sum(d["cost_usd"] for d in daily) / len(daily)
    return avg_daily * 30


def estimate_run_cost(task_count: int, model: str = "sonnet") -> tuple[float, float]:
    """Estimate cost range for a planned run before spending anything.

    Uses average token consumption per task (roughly 50k-150k tokens) and
    the model's per-1k-token pricing to produce a low-high range.

    Args:
        task_count: Number of tasks to be spawned.
        model: Default model name (e.g. "sonnet", "opus", "haiku").

    Returns:
        Tuple of (low_estimate_usd, high_estimate_usd).
    """
    cost_per_1k = _model_cost(model)
    # Conservative range: 50k tokens (small task) to 150k tokens (large task)
    low_tokens_per_task = 50
    high_tokens_per_task = 150
    low = task_count * low_tokens_per_task * cost_per_1k
    high = task_count * high_tokens_per_task * cost_per_1k
    return (round(low, 2), round(high, 2))


@dataclass(frozen=True)
class PlannedRoleForecast:
    """Estimated remaining spend for one task role."""

    role: str
    task_count: int
    estimated_cost_usd: float


@dataclass(frozen=True)
class PlannedBacklogForecast:
    """Forecasted spend for active backlog tasks."""

    task_count: int
    current_spend_usd: float
    estimated_remaining_cost_usd: float
    projected_total_cost_usd: float
    avg_estimated_cost_per_task_usd: float
    budget_usd: float
    within_budget: bool
    confidence_level: str
    per_role: list[PlannedRoleForecast]

    def to_dict(self) -> dict[str, Any]:
        """Serialize the forecast into a JSON-safe mapping."""
        return {
            "task_count": self.task_count,
            "current_spend_usd": round(self.current_spend_usd, 4),
            "estimated_remaining_cost_usd": round(self.estimated_remaining_cost_usd, 4),
            "projected_total_cost_usd": round(self.projected_total_cost_usd, 4),
            "avg_estimated_cost_per_task_usd": round(self.avg_estimated_cost_per_task_usd, 4),
            "budget_usd": round(self.budget_usd, 4),
            "within_budget": self.within_budget,
            "confidence_level": self.confidence_level,
            "per_role": [
                {
                    "role": item.role,
                    "task_count": item.task_count,
                    "estimated_cost_usd": round(item.estimated_cost_usd, 4),
                }
                for item in self.per_role
            ],
        }


_FORECASTABLE_STATUSES: frozenset[TaskStatus] = frozenset(
    {
        TaskStatus.PLANNED,
        TaskStatus.OPEN,
        TaskStatus.CLAIMED,
        TaskStatus.IN_PROGRESS,
        TaskStatus.BLOCKED,
        TaskStatus.WAITING_FOR_SUBTASKS,
        TaskStatus.PENDING_APPROVAL,
        TaskStatus.ORPHANED,
    }
)


def forecast_planned_backlog(
    tasks: list[Task],
    *,
    metrics_dir: Path | None = None,
    current_spend_usd: float = 0.0,
    budget_usd: float = 0.0,
) -> PlannedBacklogForecast:
    """Estimate remaining and projected spend for non-terminal backlog tasks."""
    planned_tasks = [task for task in tasks if task.status in _FORECASTABLE_STATUSES]
    role_rollup: dict[str, dict[str, float]] = {}
    estimated_remaining_cost = 0.0

    for task in planned_tasks:
        estimated_cost = predict_task_cost(task, metrics_dir=metrics_dir)
        estimated_remaining_cost += estimated_cost
        role_data = role_rollup.setdefault(task.role, {"task_count": 0.0, "estimated_cost_usd": 0.0})
        role_data["task_count"] += 1
        role_data["estimated_cost_usd"] += estimated_cost

    projected_total = current_spend_usd + estimated_remaining_cost
    avg_cost = estimated_remaining_cost / len(planned_tasks) if planned_tasks else 0.0

    has_history = bool(metrics_dir and metrics_dir.exists() and any(metrics_dir.glob("api_usage_*.jsonl")))
    if len(planned_tasks) >= 10 and has_history:
        confidence_level = "high"
    elif len(planned_tasks) >= 3:
        confidence_level = "medium" if has_history else "low"
    else:
        confidence_level = "low"

    per_role = [
        PlannedRoleForecast(
            role=role,
            task_count=int(values["task_count"]),
            estimated_cost_usd=values["estimated_cost_usd"],
        )
        for role, values in sorted(role_rollup.items())
    ]

    return PlannedBacklogForecast(
        task_count=len(planned_tasks),
        current_spend_usd=current_spend_usd,
        estimated_remaining_cost_usd=estimated_remaining_cost,
        projected_total_cost_usd=projected_total,
        avg_estimated_cost_per_task_usd=avg_cost,
        budget_usd=budget_usd,
        within_budget=True if budget_usd <= 0 else projected_total <= budget_usd,
        confidence_level=confidence_level,
        per_role=per_role,
    )


def predict_task_cost(task: Task, metrics_dir: Path | None = None) -> float:
    """Predict the USD cost of a task before execution.

    Uses task scope and complexity to estimate token usage, then applies
    model-specific pricing.  If metrics_dir is provided, uses historical
    averages for the task's role to refine the prediction.

    Args:
        task: The task to estimate.
        metrics_dir: Optional path to .sdd/metrics for historical data.

    Returns:
        Estimated cost in USD.
    """
    model = task.model or get_cascade_model(task)
    cost_per_1k = _model_cost(model)

    # Base token estimates by scope (in 1k tokens)
    # small: 10k, medium: 50k, large: 150k
    scope_map = {Scope.SMALL: 10, Scope.MEDIUM: 50, Scope.LARGE: 150}
    base_tokens = scope_map.get(task.scope, 50)

    # Complexity multiplier
    # low: 0.8x, medium: 1.0x, high: 2.0x
    complexity_map = {Complexity.LOW: 0.8, Complexity.MEDIUM: 1.0, Complexity.HIGH: 2.0}
    multiplier = complexity_map.get(task.complexity, 1.0)

    estimated_tokens = base_tokens * multiplier

    # Refine with historical data if available
    if metrics_dir and metrics_dir.exists():
        bandit = EpsilonGreedyBandit.load(metrics_dir)
        arm = bandit.get_arm(task.role, model)
        if arm and arm.observations >= MIN_OBSERVATIONS:
            # Use weighted average of heuristic and historical data
            # (Heuristic weight decreases as observations increase)
            weight = 1.0 / (1.0 + arm.observations / 10.0)
            hist_tokens = (arm.avg_cost_usd / cost_per_1k) if cost_per_1k > 0 else base_tokens
            estimated_tokens = (weight * estimated_tokens) + ((1 - weight) * hist_tokens)

    return round(estimated_tokens * cost_per_1k, 4)


# ---------------------------------------------------------------------------
# Per-model cache read/write pricing tiers (T569)
# ---------------------------------------------------------------------------



@dataclass
class CachePricingTier:
    """Pricing tier for cache read/write operations."""

    model: str
    provider: str
    cache_read_usd_per_1m: float  # USD per 1 million cache read tokens
    cache_write_usd_per_1m: float  # USD per 1 million cache write tokens
    standard_read_usd_per_1m: float  # USD per 1 million standard read tokens
    standard_write_usd_per_1m: float  # USD per 1 million standard write tokens
    savings_percentage: float = 0.0  # Percentage savings vs standard pricing
    metadata: dict[str, Any] = field(default_factory=lambda: {})


class CachePricingRegistry:
    """Registry for per-model cache read/write pricing tiers."""

    def __init__(self):
        self.tiers: dict[str, CachePricingTier] = {}
        self._load_default_tiers()

    def _load_default_tiers(self) -> None:
        """Load default cache pricing tiers for common models."""
        # Anthropic models
        self.register_tier(
            CachePricingTier(
                model="claude-3-5-sonnet",
                provider="anthropic",
                cache_read_usd_per_1m=0.30,
                cache_write_usd_per_1m=0.30,
                standard_read_usd_per_1m=3.00,
                standard_write_usd_per_1m=15.00,
                savings_percentage=0.90,  # 90% savings for cached reads
            )
        )

        self.register_tier(
            CachePricingTier(
                model="claude-3-5-haiku",
                provider="anthropic",
                cache_read_usd_per_1m=0.10,
                cache_write_usd_per_1m=0.10,
                standard_read_usd_per_1m=0.80,
                standard_write_usd_per_1m=3.00,
                savings_percentage=0.875,  # 87.5% savings
            )
        )

        # OpenAI models
        self.register_tier(
            CachePricingTier(
                model="gpt-4o",
                provider="openai",
                cache_read_usd_per_1m=0.25,
                cache_write_usd_per_1m=0.25,
                standard_read_usd_per_1m=2.50,
                standard_write_usd_per_1m=10.00,
                savings_percentage=0.90,  # 90% savings
            )
        )

        # Google models
        self.register_tier(
            CachePricingTier(
                model="gemini-1.5-pro",
                provider="google",
                cache_read_usd_per_1m=0.20,
                cache_write_usd_per_1m=0.20,
                standard_read_usd_per_1m=1.25,
                standard_write_usd_per_1m=5.00,
                savings_percentage=0.84,  # 84% savings
            )
        )

    def register_tier(self, tier: CachePricingTier) -> None:
        """Register a cache pricing tier."""
        key = f"{tier.provider}:{tier.model}"
        self.tiers[key] = tier
        logger.info(f"Registered cache pricing tier: {key}")

    def get_tier(self, provider: str, model: str) -> CachePricingTier | None:
        """Get cache pricing tier for a provider/model."""
        key = f"{provider}:{model}"
        return self.tiers.get(key)

    def calculate_cache_savings(
        self,
        provider: str,
        model: str,
        tokens: int,
        operation: str = "read",  # "read" or "write"
    ) -> float:
        """Calculate cache savings for a given operation."""
        tier = self.get_tier(provider, model)
        if not tier:
            return 0.0

        if operation == "read":
            standard_cost = (tokens / 1_000_000) * tier.standard_read_usd_per_1m
            cache_cost = (tokens / 1_000_000) * tier.cache_read_usd_per_1m
        else:  # write
            standard_cost = (tokens / 1_000_000) * tier.standard_write_usd_per_1m
            cache_cost = (tokens / 1_000_000) * tier.cache_write_usd_per_1m

        return max(0, standard_cost - cache_cost)

    def get_all_tiers(self) -> list[CachePricingTier]:
        """Get all registered cache pricing tiers."""
        return list(self.tiers.values())


# Global cache pricing registry
_cache_pricing_registry = CachePricingRegistry()


def get_cache_pricing_tier(provider: str, model: str) -> CachePricingTier | None:
    """Get cache pricing tier for a provider/model (T569)."""
    return _cache_pricing_registry.get_tier(provider, model)


def calculate_cache_operation_savings(provider: str, model: str, tokens: int, operation: str = "read") -> float:
    """Calculate savings for a cache operation (T569)."""
    return _cache_pricing_registry.calculate_cache_savings(provider, model, tokens, operation)


def register_cache_pricing_tier(tier: CachePricingTier) -> None:
    """Register a cache pricing tier."""
    _cache_pricing_registry.register_tier(tier)
