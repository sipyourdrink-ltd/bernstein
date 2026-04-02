"""Behavior anomaly detection for completed agent tasks."""

from __future__ import annotations

import json
import logging
import statistics
import time
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING

from bernstein.core.cost_anomaly import AnomalySignal

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


class BehaviorAnomalyAction(StrEnum):
    """Actions the orchestrator may take for anomalous agent behavior."""

    LOG = "log"
    PAUSE_SPAWNING = "stop_spawning"
    KILL_AGENT = "kill_agent"


@dataclass(frozen=True)
class BehaviorBaselineMetric:
    """Baseline statistics for one behavior metric."""

    mean: float
    stddev: float
    sample_count: int


@dataclass(frozen=True)
class BehaviorMetrics:
    """Observed metrics for one completed agent session."""

    tokens_used: int
    files_modified: int
    duration_s: float


@dataclass(frozen=True)
class MetricDeviation:
    """Deviation of one metric from the learned baseline."""

    metric: str
    value: float
    mean: float
    stddev: float
    zscore: float


@dataclass(frozen=True)
class BehaviorBaseline:
    """Baseline statistics across all tracked behavior metrics."""

    tokens_used: BehaviorBaselineMetric
    files_modified: BehaviorBaselineMetric
    duration_s: BehaviorBaselineMetric


class BehaviorAnomalyDetector:
    """Detect unusually expensive or slow agent behavior from metrics history."""

    def __init__(
        self,
        workdir: Path,
        *,
        sigma_threshold: float = 3.0,
        min_samples: int = 10,
    ) -> None:
        self._workdir = workdir
        self._sigma_threshold = sigma_threshold
        self._min_samples = min_samples

    def load_baseline(self) -> BehaviorBaseline | None:
        """Build a behavior baseline from ``.sdd/metrics/tasks.jsonl``."""
        metrics_path = self._workdir / ".sdd" / "metrics" / "tasks.jsonl"
        if not metrics_path.exists():
            return None

        tokens: list[float] = []
        files_modified: list[float] = []
        durations: list[float] = []
        with metrics_path.open(encoding="utf-8") as handle:
            for raw_line in handle:
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    logger.debug("Skipping malformed metrics line in %s", metrics_path)
                    continue
                tokens_prompt = payload.get("tokens_prompt", 0)
                tokens_completion = payload.get("tokens_completion", 0)
                tokens_value = payload.get("tokens_used")
                if isinstance(tokens_value, int | float):
                    tokens.append(float(tokens_value))
                elif isinstance(tokens_prompt, int | float) and isinstance(tokens_completion, int | float):
                    tokens.append(float(tokens_prompt) + float(tokens_completion))

                files_value = payload.get("files_modified", 0)
                if isinstance(files_value, int | float):
                    files_modified.append(float(files_value))

                duration_value = payload.get("duration_seconds", 0.0)
                if isinstance(duration_value, int | float):
                    durations.append(float(duration_value))

        if min(len(tokens), len(files_modified), len(durations)) < self._min_samples:
            return None
        return BehaviorBaseline(
            tokens_used=self._build_metric(tokens),
            files_modified=self._build_metric(files_modified),
            duration_s=self._build_metric(durations),
        )

    def detect(
        self,
        task_id: str,
        session_id: str | None,
        metrics: BehaviorMetrics,
    ) -> list[AnomalySignal]:
        """Detect anomalous behavior for the provided completed-task metrics."""
        baseline = self.load_baseline()
        if baseline is None:
            return []

        deviations = [
            deviation
            for deviation in (
                self._deviation("tokens_used", float(metrics.tokens_used), baseline.tokens_used),
                self._deviation("files_modified", float(metrics.files_modified), baseline.files_modified),
                self._deviation("duration_s", metrics.duration_s, baseline.duration_s),
            )
            if deviation is not None
        ]
        if not deviations:
            return []

        max_zscore = max(deviation.zscore for deviation in deviations)
        action = self._action_for_deviations(deviations, max_zscore)
        message = f"Behavior anomaly for task {task_id}: " + ", ".join(
            f"{deviation.metric} z={deviation.zscore:.1f}" for deviation in deviations
        )
        details = {
            "task_id": task_id,
            "session_id": session_id,
            "deviations": [
                {
                    "metric": deviation.metric,
                    "value": deviation.value,
                    "mean": deviation.mean,
                    "stddev": deviation.stddev,
                    "zscore": round(deviation.zscore, 3),
                }
                for deviation in deviations
            ],
        }
        severity = "critical" if action == BehaviorAnomalyAction.KILL_AGENT else "warning"
        return [
            AnomalySignal(
                rule="behavior_anomaly",
                severity=severity,
                action=action.value,
                agent_id=session_id,
                task_id=task_id,
                message=message,
                details=details,
                timestamp=time.time(),
            )
        ]

    def _build_metric(self, values: list[float]) -> BehaviorBaselineMetric:
        """Compute mean and standard deviation for one metric series."""
        return BehaviorBaselineMetric(
            mean=statistics.fmean(values),
            stddev=statistics.pstdev(values),
            sample_count=len(values),
        )

    def _deviation(
        self,
        metric_name: str,
        value: float,
        baseline: BehaviorBaselineMetric,
    ) -> MetricDeviation | None:
        """Return a deviation record when ``value`` exceeds the sigma threshold."""
        if baseline.sample_count < self._min_samples or baseline.stddev <= 0:
            return None
        zscore = abs(value - baseline.mean) / baseline.stddev
        if zscore <= self._sigma_threshold:
            return None
        return MetricDeviation(
            metric=metric_name,
            value=value,
            mean=baseline.mean,
            stddev=baseline.stddev,
            zscore=zscore,
        )

    def _action_for_deviations(
        self,
        deviations: list[MetricDeviation],
        max_zscore: float,
    ) -> BehaviorAnomalyAction:
        """Map deviation severity to an orchestrator action."""
        del max_zscore
        if len(deviations) >= 3:
            return BehaviorAnomalyAction.KILL_AGENT
        if len(deviations) >= 2:
            return BehaviorAnomalyAction.PAUSE_SPAWNING
        return BehaviorAnomalyAction.LOG
