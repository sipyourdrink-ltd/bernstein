"""Tests for behavior anomaly detection from task metrics."""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.core.behavior_anomaly import (
    BehaviorAnomalyAction,
    BehaviorAnomalyDetector,
    BehaviorMetrics,
)


def _write_history(metrics_dir: Path, rows: list[dict[str, float | int | str | bool | None]]) -> None:
    """Write synthetic tasks.jsonl history for anomaly tests."""
    metrics_dir.mkdir(parents=True, exist_ok=True)
    tasks_path = metrics_dir / "tasks.jsonl"
    with tasks_path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row) + "\n")


def test_detector_returns_no_signal_with_insufficient_history(tmp_path: Path) -> None:
    """The detector should stay quiet until it has enough baseline samples."""
    _write_history(
        tmp_path / ".sdd" / "metrics",
        [
            {"tokens_prompt": 100, "tokens_completion": 40, "files_modified": 2, "duration_seconds": 30.0},
            {"tokens_prompt": 110, "tokens_completion": 50, "files_modified": 3, "duration_seconds": 35.0},
        ],
    )
    detector = BehaviorAnomalyDetector(tmp_path, min_samples=3)

    signals = detector.detect(
        "task-1", "session-1", BehaviorMetrics(tokens_used=1000, files_modified=10, duration_s=300.0)
    )

    assert signals == []


def test_detector_emits_log_signal_for_single_metric_outlier(tmp_path: Path) -> None:
    """One outlier metric should produce a log-only anomaly signal."""
    _write_history(
        tmp_path / ".sdd" / "metrics",
        [
            {
                "tokens_prompt": 100 + index,
                "tokens_completion": 20 + index,
                "files_modified": 2,
                "duration_seconds": 30.0,
            }
            for index in range(12)
        ],
    )
    detector = BehaviorAnomalyDetector(tmp_path)

    signals = detector.detect(
        "task-1",
        "session-1",
        BehaviorMetrics(tokens_used=5000, files_modified=2, duration_s=31.0),
    )

    assert len(signals) == 1
    assert signals[0].action == BehaviorAnomalyAction.LOG.value
    assert signals[0].rule == "behavior_anomaly"


def test_detector_pauses_spawning_for_multiple_outliers(tmp_path: Path) -> None:
    """Two anomalous dimensions should pause further spawning."""
    _write_history(
        tmp_path / ".sdd" / "metrics",
        [
            {
                "tokens_prompt": 120,
                "tokens_completion": 40,
                "files_modified": 2 + (index % 2),
                "duration_seconds": 20 + index,
            }
            for index in range(15)
        ],
    )
    detector = BehaviorAnomalyDetector(tmp_path)

    signals = detector.detect(
        "task-2",
        "session-2",
        BehaviorMetrics(tokens_used=4000, files_modified=25, duration_s=250.0),
    )

    assert len(signals) == 1
    assert signals[0].action == BehaviorAnomalyAction.PAUSE_SPAWNING.value


def test_detector_kills_agent_for_three_extreme_outliers(tmp_path: Path) -> None:
    """Three strong outliers should escalate to kill-agent severity."""
    _write_history(
        tmp_path / ".sdd" / "metrics",
        [
            {
                "tokens_prompt": 100 + index,
                "tokens_completion": 50,
                "files_modified": 1 + (index % 2),
                "duration_seconds": 10 + index,
            }
            for index in range(20)
        ],
    )
    detector = BehaviorAnomalyDetector(tmp_path)

    signals = detector.detect(
        "task-3",
        "session-3",
        BehaviorMetrics(tokens_used=9000, files_modified=50, duration_s=500.0),
    )

    assert len(signals) == 1
    assert signals[0].action == BehaviorAnomalyAction.KILL_AGENT.value
    assert signals[0].agent_id == "session-3"
