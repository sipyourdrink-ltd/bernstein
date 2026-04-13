"""Cost anomaly detection for orchestrator runs.

Produces ``AnomalySignal`` values the orchestrator can act on (log,
stop spawning, or kill an agent).  Decoupled from ``orchestrator.py``
to avoid circular imports.
"""

from __future__ import annotations

import json
import logging
import statistics
import time
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Sequence
    from pathlib import Path

    from bernstein.core.cost.cost_tracker import CostTracker
    from bernstein.core.models import AgentSession, CostAnomalyConfig

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class AnomalySignal:
    """A single anomaly detection result."""

    rule: str  # per_task_ceiling | token_ratio | burn_rate | retry_spiral | model_mismatch
    severity: str  # info | warning | critical
    action: str  # log | stop_spawning | kill_agent
    agent_id: str | None
    task_id: str | None
    message: str
    details: dict[str, Any]
    timestamp: float


@dataclass
class TierStats:
    """Aggregated cost statistics for a single complexity tier."""

    median_cost_usd: float
    p95_cost_usd: float
    sample_count: int


@dataclass
class CostBaseline:
    """Rolling baseline of cost and token-ratio statistics."""

    per_tier: dict[str, TierStats] = field(default_factory=lambda: {})  # pyright: ignore[reportUnknownVariableType]
    token_ratio_median: float = 0.0
    token_ratio_p95: float = 0.0
    sample_count: int = 0
    updated_at: float = 0.0


_COOLDOWNS: dict[str, float] = {"kill_agent": 0.0, "stop_spawning": 60.0, "log": 300.0}

_COMPLEXITY_TO_TIER: dict[str, str] = {
    "trivial": "small",
    "small": "small",
    "medium": "medium",
    "large": "large",
    "complex": "large",
}

_HEAVY_KEYWORDS = {"opus", "o1", "o3"}
_MEDIUM_KEYWORDS = {"sonnet", "gpt-4"}
_TIER_TO_MODEL_WEIGHT: dict[str, str] = {"small": "light", "medium": "medium", "large": "heavy"}


def _percentile(values: list[float], pct: float) -> float:
    """Return the *pct*-th percentile (nearest-rank)."""
    if not values:
        return 0.0
    s = sorted(values)
    return s[min(int(len(s) * pct / 100.0), len(s) - 1)]


def _model_tier(model: str) -> str:
    """Classify a model name into heavy / medium / light."""
    lo = model.lower()
    if any(kw in lo for kw in _HEAVY_KEYWORDS):
        return "heavy"
    return "medium" if any(kw in lo for kw in _MEDIUM_KEYWORDS) else "light"


def _signal(
    rule: str,
    severity: str,
    action: str,
    message: str,
    details: dict[str, Any],
    *,
    task_id: str | None = None,
    agent_id: str | None = None,
) -> AnomalySignal:
    """Build an ``AnomalySignal`` with sensible defaults."""
    return AnomalySignal(
        rule=rule,
        severity=severity,
        action=action,
        agent_id=agent_id,
        task_id=task_id,
        message=message,
        details=details,
        timestamp=time.time(),
    )


class CostAnomalyDetector:
    """Stateful anomaly detector wired into the orchestrator tick loop.

    Args:
        config: Thresholds and feature flags.
        workdir: Project root containing ``.sdd/``.
    """

    def __init__(self, config: CostAnomalyConfig, workdir: Path) -> None:
        self._config = config
        self._workdir = workdir
        self._baseline = CostBaseline()
        self._cooldowns: dict[str, float] = {}
        self._retry_costs: dict[str, float] = {}
        self._first_attempt_costs: dict[str, float] = {}
        self._recent_tasks: list[dict[str, Any]] = []
        self.load_baseline()

    def check_tick(
        self,
        agents: Sequence[AgentSession],  # reserved for future per-agent checks
        cost_tracker: CostTracker,
    ) -> list[AnomalySignal]:
        """Run per-tick checks (burn rate).

        Token-ratio checks require input/output breakdown unavailable on
        ``AgentSession``; those run at task-completion time instead.
        """
        if not self._config.enabled:
            return []
        return self._check_burn_rate(cost_tracker)

    def check_task_completion(
        self,
        task_id: str,
        complexity: str,
        cost_usd: float,
        tokens_in: int,
        tokens_out: int,
        *,
        is_retry: bool = False,
        original_task_id: str | None = None,
    ) -> list[AnomalySignal]:
        """Run anomaly checks after a task finishes, then update baseline."""
        if not self._config.enabled:
            return []
        signals: list[AnomalySignal] = []
        signals.extend(self._check_per_task_ceiling(task_id, complexity, cost_usd))
        signals.extend(self._check_token_ratio(task_id, tokens_in, tokens_out))
        if is_retry and original_task_id:
            signals.extend(self._check_retry_spiral(task_id, original_task_id, cost_usd))
        self._update_baseline(complexity, cost_usd, tokens_in, tokens_out)
        return signals

    def check_spawn(self, task_id: str, complexity: str, model: str) -> list[AnomalySignal]:
        """Advisory check before spawning an agent (model mismatch only)."""
        if not self._config.enabled:
            return []
        return self._check_model_mismatch(task_id, complexity, model)

    def record_signal(self, signal: AnomalySignal) -> None:
        """Append *signal* to ``.sdd/metrics/anomalies.jsonl``."""
        path = self._workdir / ".sdd" / "metrics" / "anomalies.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(json.dumps(asdict(signal)) + "\n")

    def load_baseline(self) -> None:
        """Load baseline from disk; reset to empty on missing/corrupt file."""
        path = self._workdir / ".sdd" / "metrics" / "cost_baseline.json"
        if not path.exists():
            return
        try:
            raw = json.loads(path.read_text())
            per_tier = {n: TierStats(**d) for n, d in raw.get("per_tier", {}).items()}
            self._baseline = CostBaseline(
                per_tier=per_tier,
                token_ratio_median=raw.get("token_ratio_median", 0.0),
                token_ratio_p95=raw.get("token_ratio_p95", 0.0),
                sample_count=raw.get("sample_count", 0),
                updated_at=raw.get("updated_at", 0.0),
            )
            self._recent_tasks = raw.get("recent_tasks", [])
        except (json.JSONDecodeError, TypeError, KeyError):
            log.warning("Corrupt cost baseline at %s — resetting", path)
            self._baseline = CostBaseline()
            self._recent_tasks = []

    def save_baseline(self) -> None:
        """Persist baseline and recent-task window to disk."""
        path = self._workdir / ".sdd" / "metrics" / "cost_baseline.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {
            "per_tier": {n: asdict(s) for n, s in self._baseline.per_tier.items()},
            "token_ratio_median": self._baseline.token_ratio_median,
            "token_ratio_p95": self._baseline.token_ratio_p95,
            "sample_count": self._baseline.sample_count,
            "updated_at": self._baseline.updated_at,
            "recent_tasks": self._recent_tasks,
        }
        path.write_text(json.dumps(data, indent=2) + "\n")

    def _check_per_task_ceiling(self, task_id: str, complexity: str, cost_usd: float) -> list[AnomalySignal]:
        """Flag tasks whose cost greatly exceeds the tier median."""
        tier = _COMPLEXITY_TO_TIER.get(complexity, "medium")
        stats = self._baseline.per_tier.get(tier)
        if stats is None or stats.sample_count < self._config.baseline_min_samples:
            return []
        median = stats.median_cost_usd
        if median <= 0:
            return []

        ratio = cost_usd / median
        key = f"per_task_ceiling:{task_id}"
        det = {"cost_usd": cost_usd, "median": median, "ratio": ratio, "tier": tier}
        msg = f"Task {task_id} cost ${cost_usd:.4f} is {ratio:.1f}x the {tier} tier median ${median:.4f}"

        if ratio > self._config.per_task_critical_multiplier and self._should_signal(key, "kill_agent"):
            return [_signal("per_task_ceiling", "critical", "kill_agent", msg, det, task_id=task_id)]
        if ratio > self._config.per_task_multiplier and self._should_signal(key, "log"):
            return [_signal("per_task_ceiling", "warning", "log", msg, det, task_id=task_id)]
        return []

    def _check_burn_rate(self, cost_tracker: CostTracker) -> list[AnomalySignal]:
        """Flag high budget utilisation."""
        status = cost_tracker.status()
        if status.budget_usd <= 0:
            return []
        pct = status.spent_usd / status.budget_usd * 100.0
        key = "burn_rate:run"
        det = {"spent_usd": status.spent_usd, "budget_usd": status.budget_usd, "pct": pct}
        msg = f"Budget {pct:.1f}% consumed (${status.spent_usd:.2f}/${status.budget_usd:.2f})"

        if pct > self._config.budget_stop_pct and self._should_signal(key, "stop_spawning"):
            return [_signal("burn_rate", "critical", "stop_spawning", msg, det)]
        if pct > self._config.budget_warn_pct and self._should_signal(key, "log"):
            return [_signal("burn_rate", "warning", "log", msg, det)]
        return []

    def _check_token_ratio(self, task_id: str, tokens_in: int, tokens_out: int) -> list[AnomalySignal]:
        """Flag tasks with an abnormally high output/input token ratio."""
        total = tokens_in + tokens_out
        if total < self._config.token_ratio_min_tokens:
            return []
        ratio = tokens_out / max(tokens_in, 1)
        if ratio <= self._config.token_ratio_max:
            return []
        key = f"token_ratio:{task_id}"
        if not self._should_signal(key, "kill_agent"):
            return []
        return [
            _signal(
                "token_ratio",
                "critical",
                "kill_agent",
                f"Task {task_id} output/input ratio {ratio:.1f} exceeds threshold {self._config.token_ratio_max}",
                {"tokens_in": tokens_in, "tokens_out": tokens_out, "ratio": ratio},
                task_id=task_id,
            )
        ]

    def _check_retry_spiral(self, task_id: str, original_task_id: str, cost_usd: float) -> list[AnomalySignal]:
        """Flag retry chains whose cumulative cost spirals out of control."""
        if original_task_id not in self._first_attempt_costs:
            self._first_attempt_costs[original_task_id] = cost_usd
        self._retry_costs[original_task_id] = self._retry_costs.get(original_task_id, 0.0) + cost_usd

        first_cost = self._first_attempt_costs[original_task_id]
        cumulative = self._retry_costs[original_task_id]
        if first_cost <= 0 or cumulative <= first_cost * self._config.retry_cost_multiplier:
            return []

        key = f"retry_spiral:{original_task_id}"
        if not self._should_signal(key, "stop_spawning"):
            return []
        mult = cumulative / first_cost
        return [
            _signal(
                "retry_spiral",
                "critical",
                "stop_spawning",
                f"Retry chain for {original_task_id} has cost ${cumulative:.4f} ({mult:.1f}x first attempt)",
                {
                    "original_task_id": original_task_id,
                    "first_cost": first_cost,
                    "cumulative": cumulative,
                    "multiplier": mult,
                },
                task_id=task_id,
            )
        ]

    def _check_model_mismatch(self, task_id: str, complexity: str, model: str) -> list[AnomalySignal]:
        """Advisory: flag heavy models assigned to trivial tasks."""
        mt = _model_tier(model)
        expected = _TIER_TO_MODEL_WEIGHT.get(_COMPLEXITY_TO_TIER.get(complexity, "medium"), "medium")
        if mt != "heavy" or expected != "light":
            return []
        key = f"model_mismatch:{task_id}"
        if not self._should_signal(key, "log"):
            return []
        return [
            _signal(
                "model_mismatch",
                "info",
                "log",
                f"Heavy model '{model}' assigned to {complexity} task {task_id}",
                {"model": model, "model_tier": mt, "complexity": complexity, "expected_tier": expected},
                task_id=task_id,
            )
        ]

    def _should_signal(self, key: str, action: str) -> bool:
        """Return True if the cooldown for *key* has elapsed."""
        now = time.time()
        cooldown = _COOLDOWNS.get(action, 300.0)
        if now - self._cooldowns.get(key, 0.0) < cooldown:
            return False
        self._cooldowns[key] = now
        return True

    def _update_baseline(self, complexity: str, cost_usd: float, tokens_in: int, tokens_out: int) -> None:
        """Append a completed task to the rolling window and recalculate."""
        tier = _COMPLEXITY_TO_TIER.get(complexity, "medium")
        self._recent_tasks.append({"tier": tier, "cost": cost_usd, "ratio": tokens_out / max(tokens_in, 1)})
        if len(self._recent_tasks) > self._config.baseline_window:
            self._recent_tasks = self._recent_tasks[-self._config.baseline_window :]
        self._recalculate_baseline()
        self.save_baseline()

    def _recalculate_baseline(self) -> None:
        """Rebuild tier stats and token-ratio stats from the recent-task window."""
        tier_costs: dict[str, list[float]] = {}
        ratios: list[float] = []
        for entry in self._recent_tasks:
            tier_costs.setdefault(entry["tier"], []).append(entry["cost"])
            ratios.append(entry["ratio"])

        per_tier: dict[str, TierStats] = {}
        for tier, costs in tier_costs.items():
            per_tier[tier] = TierStats(
                median_cost_usd=statistics.median(costs),
                p95_cost_usd=_percentile(costs, 95),
                sample_count=len(costs),
            )
        self._baseline = CostBaseline(
            per_tier=per_tier,
            token_ratio_median=statistics.median(ratios) if ratios else 0.0,
            token_ratio_p95=_percentile(ratios, 95),
            sample_count=len(self._recent_tasks),
            updated_at=time.time(),
        )
