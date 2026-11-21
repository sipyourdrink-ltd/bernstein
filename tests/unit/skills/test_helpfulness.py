"""Tests for local skill helpfulness attribution (#1720, Track 5)."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest

from bernstein.core.skills.activation_log import ActivationRecord, log_activation
from bernstein.core.skills.helpfulness import build_helpfulness_report, helpfulness_path, write_helpfulness_report


def _write_task_completion(
    workdir: Path, task_id: str, *, success: bool, role: str = "backend", timestamp: float | None = 1.0
) -> None:
    metrics_dir = workdir / ".sdd" / "metrics"
    metrics_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "metric_type": "task_completion_time",
        "value": 3.0,
        "labels": {
            "task_id": task_id,
            "role": role,
            "model": "sonnet",
            "success": str(success),
        },
    }
    if timestamp is not None:
        payload["timestamp"] = timestamp
    with (metrics_dir / "task_completion_time_2026-05-22.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True))
        handle.write("\n")


def test_build_helpfulness_report_uses_beta_bernoulli_scores(tmp_path: Path) -> None:
    log_activation(ActivationRecord(skill="pytest-helper", role="backend", task_id="task-1"), workdir=tmp_path)
    log_activation(ActivationRecord(skill="pytest-helper", role="backend", task_id="task-2"), workdir=tmp_path)
    log_activation(ActivationRecord(skill="docs-helper", role="docs", task_id="task-2"), workdir=tmp_path)
    log_activation(ActivationRecord(skill="ignored-helper", role="qa", task_id="missing-outcome"), workdir=tmp_path)
    _write_task_completion(tmp_path, "task-1", success=True)
    _write_task_completion(tmp_path, "task-2", success=False, role="docs")

    report = build_helpfulness_report(
        tmp_path,
        now=datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
    )

    assert report.generated_at == "2026-05-22T12:00:00.000Z"
    assert report.unmatched_activations == 1
    pytest_helper = report.skills["pytest-helper"]
    assert pytest_helper.successes == 1
    assert pytest_helper.failures == 1
    assert pytest_helper.alpha == 2.0
    assert pytest_helper.beta == 2.0
    assert pytest_helper.posterior_mean == 0.5
    assert pytest_helper.by_role["backend"].observations == 2
    docs_helper = report.skills["docs-helper"]
    assert docs_helper.successes == 0
    assert docs_helper.failures == 1
    assert docs_helper.posterior_mean == 1.0 / 3.0
    assert "ignored-helper" not in report.skills


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        pytest.param([(20, True), (10, False)], (1, 0), id="newest-first"),
        pytest.param([(10, False), (20, True)], (1, 0), id="newest-last"),
        pytest.param([(10, True), (10, False)], (0, 1), id="equal-times"),
        pytest.param([(10, False), (10, True)], (1, 0), id="equal-times-reversed"),
        pytest.param([(None, True), (None, False)], (0, 1), id="undated"),
        pytest.param([(None, False), (None, True)], (1, 0), id="undated-reversed"),
        pytest.param([(0, True), (None, False)], (1, 0), id="dated-before-undated"),
    ],
)
def test_out_of_order_completions_use_recorded_time(
    tmp_path: Path, rows: list[tuple[float | None, bool]], expected: tuple[int, int]
) -> None:
    log_activation(ActivationRecord(skill="format", role="qa", task_id="task-1"), workdir=tmp_path)
    for timestamp, success in rows:
        _write_task_completion(tmp_path, "task-1", success=success, timestamp=timestamp)

    score = build_helpfulness_report(tmp_path).skills["format"]
    assert (score.successes, score.failures) == expected


def test_write_helpfulness_report_persists_stable_json(tmp_path: Path) -> None:
    log_activation(ActivationRecord(skill="pytest-helper", role="backend", task_id="task-1"), workdir=tmp_path)
    _write_task_completion(tmp_path, "task-1", success=True)

    path = write_helpfulness_report(
        tmp_path,
        now=datetime(2026, 5, 22, 12, 0, tzinfo=UTC),
    )

    assert path == helpfulness_path(tmp_path)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["skills"]["pytest-helper"]["posterior_mean"] == 2.0 / 3.0
    assert path.read_text(encoding="utf-8").endswith("\n")
