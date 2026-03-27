"""Per-run cost budget tracker.

Tracks cumulative token usage and cost per orchestrator run.  Emits
warnings at configurable thresholds (80% / 95% / 100%) and tells the
orchestrator when to stop spawning agents.

Cost data is persisted to ``.sdd/runtime/costs/{run_id}.json`` so that
the ``GET /costs/{run_id}`` endpoint and the CLI can report budget
status for any run, even after restart.

This module is about *budget enforcement*.  Model selection / ROI
optimization lives in ``cost.py`` — do not conflate the two.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

from bernstein.core.cost import _MODEL_COST_USD_PER_1K  # pyright: ignore[reportPrivateUsage]
from bernstein.core.models import (
    AgentCostSummary,
    ModelCostBreakdown,
    RunCostProjection,
    RunCostReport,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Threshold defaults
# ---------------------------------------------------------------------------

DEFAULT_WARN_THRESHOLD: float = 0.80
DEFAULT_CRITICAL_THRESHOLD: float = 0.95
DEFAULT_HARD_STOP_THRESHOLD: float = 1.00


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class TokenUsage:
    """A single token-usage record for one agent invocation.

    Attributes:
        input_tokens: Number of input (prompt) tokens consumed.
        output_tokens: Number of output (completion) tokens consumed.
        model: Model name (e.g. ``"sonnet"``, ``"opus"``).
        cost_usd: Computed cost in USD for this usage record.
        agent_id: The agent session that incurred the cost.
        task_id: The task the agent was working on.
        timestamp: Unix timestamp of when the usage was recorded.
    """

    input_tokens: int
    output_tokens: int
    model: str
    cost_usd: float
    agent_id: str
    task_id: str
    timestamp: float = field(default_factory=time.time)

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "model": self.model,
            "cost_usd": self.cost_usd,
            "agent_id": self.agent_id,
            "task_id": self.task_id,
            "timestamp": self.timestamp,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TokenUsage:
        """Deserialise from a dict."""
        return cls(
            input_tokens=int(d["input_tokens"]),
            output_tokens=int(d["output_tokens"]),
            model=str(d["model"]),
            cost_usd=float(d["cost_usd"]),
            agent_id=str(d["agent_id"]),
            task_id=str(d["task_id"]),
            timestamp=float(d.get("timestamp", 0.0)),
        )


@dataclass(frozen=True)
class BudgetStatus:
    """Snapshot of the current budget state for a run.

    Attributes:
        run_id: Unique identifier for the orchestrator run.
        budget_usd: Total budget cap in USD (0 = unlimited).
        spent_usd: Cumulative spend so far.
        remaining_usd: Budget minus spend (clamped to >= 0).
        percentage_used: Spend as a fraction of budget (0.0-1.0+).
        should_warn: True when spend >= warning threshold.
        should_stop: True when spend >= hard-stop threshold.
    """

    run_id: str
    budget_usd: float
    spent_usd: float
    remaining_usd: float
    percentage_used: float
    should_warn: bool
    should_stop: bool

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-safe dict."""
        import math

        remaining = self.remaining_usd if math.isfinite(self.remaining_usd) else 0.0
        return {
            "run_id": self.run_id,
            "budget_usd": self.budget_usd,
            "spent_usd": round(self.spent_usd, 6),
            "remaining_usd": round(remaining, 6),
            "percentage_used": round(self.percentage_used, 4),
            "should_warn": self.should_warn,
            "should_stop": self.should_stop,
        }


# ---------------------------------------------------------------------------
# Cost estimation helper
# ---------------------------------------------------------------------------


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Estimate cost in USD for a given model and token counts.

    Uses the ``_MODEL_COST_USD_PER_1K`` pricing table from ``cost.py``.
    The table stores a blended per-1k-token rate (input+output combined),
    so we sum the tokens and multiply.

    Args:
        model: Model name (e.g. ``"sonnet"``, ``"opus"``).
        input_tokens: Number of input tokens.
        output_tokens: Number of output tokens.

    Returns:
        Estimated cost in USD.
    """
    model_lower = model.lower()
    rate: float = 0.005  # safe fallback
    for key, cost in _MODEL_COST_USD_PER_1K.items():
        if key in model_lower:
            rate = cost
            break
    total_tokens = input_tokens + output_tokens
    return (total_tokens / 1000.0) * rate


# ---------------------------------------------------------------------------
# CostTracker
# ---------------------------------------------------------------------------


@dataclass
class CostTracker:
    """Per-run cost tracker with budget enforcement.

    Tracks cumulative spend, records per-agent token usage, emits log
    warnings at configurable thresholds, and persists state to disk.

    Args:
        run_id: Unique identifier for the orchestrator run.
        budget_usd: Dollar cap for this run (0 = unlimited).
        warn_threshold: Fraction (0-1) at which a warning is logged.
        critical_threshold: Fraction (0-1) at which a critical warning is logged.
        hard_stop_threshold: Fraction (0-1) at which ``should_stop`` becomes True.
    """

    run_id: str
    budget_usd: float = 0.0
    warn_threshold: float = DEFAULT_WARN_THRESHOLD
    critical_threshold: float = DEFAULT_CRITICAL_THRESHOLD
    hard_stop_threshold: float = DEFAULT_HARD_STOP_THRESHOLD

    # Mutable tracking state (not constructor args)
    _spent_usd: float = field(default=0.0, init=False, repr=False)
    _usages: list[TokenUsage] = field(default_factory=list[TokenUsage], init=False, repr=False)
    _warned: bool = field(default=False, init=False, repr=False)
    _critical_warned: bool = field(default=False, init=False, repr=False)

    # ---- recording --------------------------------------------------------

    def record(
        self,
        agent_id: str,
        task_id: str,
        model: str,
        input_tokens: int,
        output_tokens: int,
        cost_usd: float | None = None,
    ) -> BudgetStatus:
        """Record token usage for an agent and return updated budget status.

        If *cost_usd* is ``None``, the cost is estimated from the model
        pricing table.

        Args:
            agent_id: Agent session ID.
            task_id: Task ID the agent was working on.
            model: Model name used.
            input_tokens: Input tokens consumed.
            output_tokens: Output tokens consumed.
            cost_usd: Explicit cost override; estimated if omitted.

        Returns:
            Current ``BudgetStatus`` after recording.
        """
        if cost_usd is None:
            cost_usd = estimate_cost(model, input_tokens, output_tokens)

        usage = TokenUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            model=model,
            cost_usd=cost_usd,
            agent_id=agent_id,
            task_id=task_id,
        )
        self._usages.append(usage)
        self._spent_usd += cost_usd

        status = self.status()
        self._emit_threshold_warnings(status)
        return status

    # ---- status -----------------------------------------------------------

    def status(self) -> BudgetStatus:
        """Return the current budget status snapshot.

        Returns:
            Immutable ``BudgetStatus`` with remaining budget, percentage
            used, and stop/warn flags.
        """
        if self.budget_usd <= 0:
            # Unlimited budget — never warn or stop
            return BudgetStatus(
                run_id=self.run_id,
                budget_usd=0.0,
                spent_usd=self._spent_usd,
                remaining_usd=float("inf"),
                percentage_used=0.0,
                should_warn=False,
                should_stop=False,
            )

        pct = self._spent_usd / self.budget_usd if self.budget_usd > 0 else 0.0
        remaining = max(self.budget_usd - self._spent_usd, 0.0)
        return BudgetStatus(
            run_id=self.run_id,
            budget_usd=self.budget_usd,
            spent_usd=self._spent_usd,
            remaining_usd=remaining,
            percentage_used=pct,
            should_warn=pct >= self.warn_threshold,
            should_stop=pct >= self.hard_stop_threshold,
        )

    @property
    def spent_usd(self) -> float:
        """Total USD spent so far."""
        return self._spent_usd

    @property
    def usages(self) -> list[TokenUsage]:
        """All recorded token usage entries (read-only copy)."""
        return list(self._usages)

    # ---- persistence ------------------------------------------------------

    def save(self, base_dir: Path) -> Path:
        """Persist cost data to ``.sdd/runtime/costs/{run_id}.json``.

        Creates the directory if it does not exist.

        Args:
            base_dir: The ``.sdd`` directory (or any parent under which
                ``runtime/costs/`` will be created).

        Returns:
            Path to the written JSON file.
        """
        costs_dir = base_dir / "runtime" / "costs"
        costs_dir.mkdir(parents=True, exist_ok=True)
        file_path = costs_dir / f"{self.run_id}.json"

        data: dict[str, Any] = {
            "run_id": self.run_id,
            "budget_usd": self.budget_usd,
            "spent_usd": round(self._spent_usd, 6),
            "warn_threshold": self.warn_threshold,
            "critical_threshold": self.critical_threshold,
            "hard_stop_threshold": self.hard_stop_threshold,
            "usages": [u.to_dict() for u in self._usages],
        }
        file_path.write_text(json.dumps(data, indent=2))
        return file_path

    @classmethod
    def load(cls, base_dir: Path, run_id: str) -> CostTracker | None:
        """Load a previously persisted CostTracker from disk.

        Args:
            base_dir: The ``.sdd`` directory.
            run_id: Run identifier to look up.

        Returns:
            Restored ``CostTracker``, or ``None`` if the file doesn't exist
            or is corrupt.
        """
        file_path = base_dir / "runtime" / "costs" / f"{run_id}.json"
        if not file_path.exists():
            return None
        try:
            data = json.loads(file_path.read_text())
            tracker = cls(
                run_id=data["run_id"],
                budget_usd=float(data.get("budget_usd", 0.0)),
                warn_threshold=float(data.get("warn_threshold", DEFAULT_WARN_THRESHOLD)),
                critical_threshold=float(data.get("critical_threshold", DEFAULT_CRITICAL_THRESHOLD)),
                hard_stop_threshold=float(data.get("hard_stop_threshold", DEFAULT_HARD_STOP_THRESHOLD)),
            )
            for u_dict in data.get("usages", []):
                usage = TokenUsage.from_dict(u_dict)
                tracker._usages.append(usage)
                tracker._spent_usd += usage.cost_usd
            return tracker
        except Exception as exc:
            logger.warning("Failed to load cost tracker for run %s: %s", run_id, exc)
            return None

    # ---- reporting --------------------------------------------------------

    def shareable_summary(
        self,
        tasks_done: int = 0,
        tasks_failed: int = 0,
        total_duration_s: float = 0.0,
    ) -> str:
        """Return a markdown run-summary snippet suitable for sharing.

        Computes savings vs an all-Opus baseline using current usages.

        Args:
            tasks_done: Number of tasks that completed successfully.
            tasks_failed: Number of tasks that failed.
            total_duration_s: Wall-clock duration of the run in seconds.

        Returns:
            Multi-line markdown string.
        """
        opus_cost_per_1k = _MODEL_COST_USD_PER_1K["opus"]
        savings = 0.0
        for u in self._usages:
            if "opus" not in u.model.lower():
                total_tokens = u.input_tokens + u.output_tokens
                if total_tokens > 0:
                    opus_est = (total_tokens / 1000.0) * opus_cost_per_1k
                    savings += max(opus_est - u.cost_usd, 0.0)

        actual = self._spent_usd
        single_agent = actual + savings
        savings_pct = (savings / single_agent * 100) if single_agent > 0 else 0.0

        mins = int(total_duration_s // 60)
        secs = int(total_duration_s % 60)
        time_str = f"{mins}m {secs}s" if mins > 0 else f"{secs}s"

        lines: list[str] = ["🎼 Bernstein run summary"]
        lines.append(f"   Tasks: {tasks_done} completed" + (f", {tasks_failed} failed" if tasks_failed else ""))
        if total_duration_s > 0:
            lines.append(f"   Time:  {time_str}")
        if single_agent > actual:
            lines.append(f"   Cost:  ${actual:.2f} (vs ~${single_agent:.2f} single agent)")
            lines.append(f"   Saved: ${savings:.2f} ({savings_pct:.0f}%)")
        else:
            lines.append(f"   Cost:  ${actual:.2f}")
        return "\n".join(lines)

    # ---- breakdowns & projection ------------------------------------------

    def agent_summaries(self) -> list[AgentCostSummary]:
        """Build per-agent cost summaries from recorded usages.

        Returns:
            List of :class:`AgentCostSummary` sorted by total cost descending.
        """
        data: dict[str, dict[str, Any]] = {}
        for u in self._usages:
            if u.agent_id not in data:
                data[u.agent_id] = {"total": 0.0, "count": 0, "models": {}}
            data[u.agent_id]["total"] += u.cost_usd
            data[u.agent_id]["count"] += 1
            bucket: dict[str, float] = data[u.agent_id]["models"]
            bucket[u.model] = bucket.get(u.model, 0.0) + u.cost_usd

        return [
            AgentCostSummary(
                agent_id=aid,
                total_cost_usd=round(d["total"], 6),
                task_count=int(d["count"]),
                model_breakdown={m: round(c, 6) for m, c in d["models"].items()},
            )
            for aid, d in sorted(data.items(), key=lambda kv: kv[1]["total"], reverse=True)
        ]

    def model_breakdowns(self) -> list[ModelCostBreakdown]:
        """Build per-model cost breakdowns from recorded usages.

        Returns:
            List of :class:`ModelCostBreakdown` sorted by total cost descending.
        """
        data: dict[str, dict[str, Any]] = {}
        for u in self._usages:
            if u.model not in data:
                data[u.model] = {"total": 0.0, "tokens": 0, "count": 0}
            data[u.model]["total"] += u.cost_usd
            data[u.model]["tokens"] += u.input_tokens + u.output_tokens
            data[u.model]["count"] += 1

        return [
            ModelCostBreakdown(
                model=model,
                total_cost_usd=round(d["total"], 6),
                total_tokens=int(d["tokens"]),
                invocation_count=int(d["count"]),
            )
            for model, d in sorted(data.items(), key=lambda kv: kv[1]["total"], reverse=True)
        ]

    def project(self, tasks_done: int, tasks_remaining: int) -> RunCostProjection:
        """Project total run cost based on completed-task history.

        Uses ``current_cost / tasks_done`` as the per-task average and
        multiplies by ``tasks_remaining`` to estimate the remaining spend.
        Confidence is 0 with no data and approaches 1.0 after 5+ tasks.

        Args:
            tasks_done: Number of tasks completed so far.
            tasks_remaining: Number of tasks still outstanding.

        Returns:
            :class:`RunCostProjection` with estimate and confidence.
        """
        current = self._spent_usd
        avg_per_task = (current / tasks_done) if tasks_done > 0 else 0.0
        projected_total = current + avg_per_task * max(tasks_remaining, 0)
        confidence = min(tasks_done / 5.0, 1.0) if tasks_done > 0 else 0.0

        within_budget = True if self.budget_usd <= 0 else projected_total <= self.budget_usd

        return RunCostProjection(
            run_id=self.run_id,
            tasks_done=tasks_done,
            tasks_remaining=max(tasks_remaining, 0),
            current_cost_usd=round(current, 6),
            projected_total_usd=round(projected_total, 6),
            avg_cost_per_task_usd=round(avg_per_task, 6),
            budget_usd=self.budget_usd,
            within_budget=within_budget,
            confidence=round(confidence, 3),
        )

    def report(self, tasks_done: int = 0, tasks_remaining: int = 0) -> RunCostReport:
        """Build a full cost report for this run.

        Args:
            tasks_done: Tasks completed; used for projection (0 = no projection).
            tasks_remaining: Tasks still outstanding; used for projection.

        Returns:
            :class:`RunCostReport` with per-agent, per-model, and projection data.
        """
        projection: RunCostProjection | None = None
        if tasks_done > 0 or tasks_remaining > 0:
            projection = self.project(tasks_done, tasks_remaining)

        return RunCostReport(
            run_id=self.run_id,
            total_spent_usd=round(self._spent_usd, 6),
            budget_usd=self.budget_usd,
            per_agent=self.agent_summaries(),
            per_model=self.model_breakdowns(),
            projection=projection,
        )

    def save_metrics(self, metrics_dir: Path) -> Path:
        """Persist a cost report to ``.sdd/metrics/costs_{run_id}.json``.

        Creates the directory if it does not exist.  Handles zero/missing
        budget gracefully — ``budget_usd=0`` is written as-is (unlimited).

        Args:
            metrics_dir: The ``.sdd/metrics`` directory path.

        Returns:
            Path to the written JSON file.
        """
        from pathlib import Path as _Path

        metrics_path = _Path(str(metrics_dir))
        metrics_path.mkdir(parents=True, exist_ok=True)
        file_path = metrics_path / f"costs_{self.run_id}.json"
        r = self.report()
        file_path.write_text(json.dumps(r.to_dict(), indent=2))
        logger.debug("Cost report for run %s saved to %s", self.run_id, file_path)
        return file_path

    # ---- internal ---------------------------------------------------------

    def _emit_threshold_warnings(self, status: BudgetStatus) -> None:
        """Log warnings when budget thresholds are crossed.

        Each threshold is logged at most once per tracker lifetime.
        """
        if self.budget_usd <= 0:
            return

        if status.percentage_used >= self.hard_stop_threshold:
            logger.warning(
                "BUDGET EXCEEDED for run %s: $%.2f / $%.2f (%.0f%%) — stopping agent spawns",
                self.run_id,
                status.spent_usd,
                self.budget_usd,
                status.percentage_used * 100,
            )
        elif status.percentage_used >= self.critical_threshold and not self._critical_warned:
            self._critical_warned = True
            logger.warning(
                "BUDGET CRITICAL for run %s: $%.2f / $%.2f (%.0f%%)",
                self.run_id,
                status.spent_usd,
                self.budget_usd,
                status.percentage_used * 100,
            )
        elif status.percentage_used >= self.warn_threshold and not self._warned:
            self._warned = True
            logger.warning(
                "Budget warning for run %s: $%.2f / $%.2f (%.0f%%)",
                self.run_id,
                status.spent_usd,
                self.budget_usd,
                status.percentage_used * 100,
            )
