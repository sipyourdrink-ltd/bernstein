"""Metric record types and file-based metrics collection for the evolution system."""

from __future__ import annotations

import contextlib
import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Protocol

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Metric record types
# ---------------------------------------------------------------------------


@dataclass
class MetricRecord:
    """Base class for metric records."""

    timestamp: float
    task_id: str | None = None
    role: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "task_id": self.task_id,
            "role": self.role,
        }


@dataclass
class TaskMetrics(MetricRecord):
    """Metrics for a completed task."""

    model: str | None = None
    provider: str | None = None
    duration_seconds: float = 0.0
    tokens_prompt: int = 0
    tokens_completion: int = 0
    cost_usd: float = 0.0
    janitor_passed: bool = True
    files_modified: int = 0
    lines_added: int = 0
    lines_deleted: int = 0

    def to_dict(self) -> dict[str, Any]:
        base = super().to_dict()
        base.update(
            {
                "model": self.model,
                "provider": self.provider,
                "duration_seconds": self.duration_seconds,
                "tokens_prompt": self.tokens_prompt,
                "tokens_completion": self.tokens_completion,
                "cost_usd": self.cost_usd,
                "janitor_passed": self.janitor_passed,
                "files_modified": self.files_modified,
                "lines_added": self.lines_added,
                "lines_deleted": self.lines_deleted,
            }
        )
        return base


@dataclass
class AgentMetrics(MetricRecord):
    """Metrics for an agent session."""

    agent_id: str | None = None
    lifetime_seconds: float = 0.0
    tasks_completed: int = 0
    heartbeat_failures: int = 0
    sleep_incidents: int = 0
    context_tokens: int = 0

    def to_dict(self) -> dict[str, Any]:
        base = super().to_dict()
        base.update(
            {
                "agent_id": self.agent_id,
                "lifetime_seconds": self.lifetime_seconds,
                "tasks_completed": self.tasks_completed,
                "heartbeat_failures": self.heartbeat_failures,
                "sleep_incidents": self.sleep_incidents,
                "context_tokens": self.context_tokens,
            }
        )
        return base


@dataclass
class CostMetrics(MetricRecord):
    """Cost metrics for a provider."""

    provider: str | None = None
    model: str | None = None
    tier: str | None = None
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    rate_limit_remaining: int | None = None
    free_tier_remaining: int | None = None

    def to_dict(self) -> dict[str, Any]:
        base = super().to_dict()
        base.update(
            {
                "provider": self.provider,
                "model": self.model,
                "tier": self.tier,
                "tokens_in": self.tokens_in,
                "tokens_out": self.tokens_out,
                "cost_usd": self.cost_usd,
                "rate_limit_remaining": self.rate_limit_remaining,
                "free_tier_remaining": self.free_tier_remaining,
            }
        )
        return base


@dataclass
class QualityMetrics(MetricRecord):
    """Quality metrics."""

    janitor_pass_rate: float = 0.0
    human_approval_rate: float = 0.0
    rollback_rate: float = 0.0
    test_pass_rate: float = 0.0
    rework_rate: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        base = super().to_dict()
        base.update(
            {
                "janitor_pass_rate": self.janitor_pass_rate,
                "human_approval_rate": self.human_approval_rate,
                "rollback_rate": self.rollback_rate,
                "test_pass_rate": self.test_pass_rate,
                "rework_rate": self.rework_rate,
            }
        )
        return base


# ---------------------------------------------------------------------------
# Collector protocol & implementation
# ---------------------------------------------------------------------------


class MetricsCollector(Protocol):
    """Protocol for metrics collection."""

    def record_task_metrics(self, metrics: TaskMetrics) -> None: ...
    def record_agent_metrics(self, metrics: AgentMetrics) -> None: ...
    def record_cost_metrics(self, metrics: CostMetrics) -> None: ...
    def record_quality_metrics(self, metrics: QualityMetrics) -> None: ...
    def get_recent_task_metrics(self, hours: int = 24) -> list[TaskMetrics]: ...
    def get_recent_cost_metrics(self, hours: int = 24) -> list[CostMetrics]: ...


class FileMetricsCollector:
    """Collects and stores metrics to JSONL files in .sdd/metrics/."""

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.metrics_dir = state_dir / "metrics"
        self.metrics_dir.mkdir(parents=True, exist_ok=True)

        self._task_metrics: list[TaskMetrics] = []
        self._agent_metrics: list[AgentMetrics] = []
        self._cost_metrics: list[CostMetrics] = []
        self._quality_metrics: list[QualityMetrics] = []

    def record_task_metrics(self, metrics: TaskMetrics) -> None:
        self._task_metrics.append(metrics)
        self._append_to_file("tasks.jsonl", metrics.to_dict())

    def record_agent_metrics(self, metrics: AgentMetrics) -> None:
        self._agent_metrics.append(metrics)
        self._append_to_file("agents.jsonl", metrics.to_dict())

    def record_cost_metrics(self, metrics: CostMetrics) -> None:
        self._cost_metrics.append(metrics)
        self._append_to_file("costs.jsonl", metrics.to_dict())

    def record_quality_metrics(self, metrics: QualityMetrics) -> None:
        self._quality_metrics.append(metrics)
        self._append_to_file("quality.jsonl", metrics.to_dict())

    def _append_to_file(self, filename: str, data: dict[str, Any]) -> None:
        filepath = self.metrics_dir / filename
        with filepath.open("a") as f:
            f.write(json.dumps(data) + "\n")

    def get_recent_task_metrics(self, hours: int = 24) -> list[TaskMetrics]:
        cutoff = time.time() - (hours * 3600)
        return [m for m in self._task_metrics if m.timestamp >= cutoff]

    def get_recent_cost_metrics(self, hours: int = 24) -> list[CostMetrics]:
        cutoff = time.time() - (hours * 3600)
        return [m for m in self._cost_metrics if m.timestamp >= cutoff]

    def get_recent_agent_metrics(self, hours: int = 24) -> list[AgentMetrics]:
        cutoff = time.time() - (hours * 3600)
        return [m for m in self._agent_metrics if m.timestamp >= cutoff]

    def get_recent_quality_metrics(self, hours: int = 24) -> list[QualityMetrics]:
        cutoff = time.time() - (hours * 3600)
        return [m for m in self._quality_metrics if m.timestamp >= cutoff]

    def load_from_files(self) -> None:
        self._task_metrics = self._load_from_file("tasks.jsonl", TaskMetrics)
        self._agent_metrics = self._load_from_file("agents.jsonl", AgentMetrics)
        self._cost_metrics = self._load_from_file("costs.jsonl", CostMetrics)
        self._quality_metrics = self._load_from_file("quality.jsonl", QualityMetrics)

    def _load_from_file(self, filename: str, cls: type[Any]) -> list[Any]:
        filepath = self.metrics_dir / filename
        if not filepath.exists():
            return []
        records: list[Any] = []
        with filepath.open() as f:
            for line in f:
                if line.strip():
                    data: dict[str, Any] = json.loads(line)
                    with contextlib.suppress(TypeError):
                        records.append(cls(**data))
        return records
