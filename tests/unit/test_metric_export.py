"""Unit tests for metrics export."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import patch

from bernstein.core.differential_privacy import DPConfig
from bernstein.core.metric_export import export_metrics


def _collector_fixture() -> Any:
    task = SimpleNamespace(
        task_id="T-1",
        role="backend",
        model="sonnet",
        provider="anthropic",
        end_time=15.0,
        start_time=5.0,
        success=True,
        tokens_used=1234,
        cost_usd=0.12,
        error="",
    )
    agent = SimpleNamespace(
        agent_id="A-1",
        role="backend",
        tasks_completed=2,
        tasks_failed=0,
        total_tokens=2345,
        total_cost_usd=0.34,
    )
    collector = SimpleNamespace(
        get_metrics_summary=lambda: {"total_tasks": 1},
        task_metrics={"T-1": task},
        agent_metrics={"A-1": agent},
    )
    return collector


def test_export_metrics_writes_json_file(tmp_path: Path) -> None:
    output = tmp_path / "metrics.json"
    export_metrics(_collector_fixture(), output)

    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["summary"]["total_tasks"] == 1
    assert data["task_metrics"][0]["task_id"] == "T-1"
    assert data["agent_metrics"][0]["agent_id"] == "A-1"


def test_export_metrics_with_dp_uses_privatized_payload(tmp_path: Path) -> None:
    output = tmp_path / "metrics_dp.json"
    collector = _collector_fixture()
    with patch(
        "bernstein.core.differential_privacy.apply_dp_to_export",
        return_value={"dp": True, "exported_at": "x"},
    ):
        export_metrics(collector, output, dp_config=DPConfig(epsilon=1.0, delta=1e-5))

    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["dp"] is True


def test_export_metrics_empty_collections(tmp_path: Path) -> None:
    output = tmp_path / "empty.json"

    def _summary() -> dict[str, int]:
        return {}

    collector = SimpleNamespace(
        get_metrics_summary=_summary,
        task_metrics={},
        agent_metrics={},
    )
    export_metrics(cast("Any", collector), output)

    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["task_metrics"] == []
    assert data["agent_metrics"] == []


def test_export_metrics_timestamp_is_iso8601(tmp_path: Path) -> None:
    output = tmp_path / "timestamp.json"
    export_metrics(_collector_fixture(), output)
    data = json.loads(output.read_text(encoding="utf-8"))

    parsed = datetime.fromisoformat(data["exported_at"])
    assert parsed.year >= 2000


def test_export_metrics_includes_residency_attestations(tmp_path: Path) -> None:
    output = tmp_path / "residency.json"
    collector = _collector_fixture()
    collector.residency_attestations = [
        {
            "provider": "google_ai",
            "provider_region": "eu-west-1",
            "required_region": "eu",
            "compliant": True,
            "attestation": "gdpr-eu",
            "reason": "preferred_tier:T-1",
        }
    ]

    export_metrics(collector, output)

    data = json.loads(output.read_text(encoding="utf-8"))
    assert data["residency_attestations"][0]["provider"] == "google_ai"
