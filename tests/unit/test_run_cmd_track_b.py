"""Tests for Track B run-command helpers."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from bernstein.cli.run_cmd import (
    RunCostEstimate,
    _emit_preflight_runtime_warnings,
    _estimate_run_preview,
    _finalize_run_output,
    _wait_for_run_completion,
)


def test_estimate_run_preview_uses_plan_task_count(tmp_path: Path) -> None:
    plan_file = tmp_path / "plan.yaml"
    plan_file.write_text("name: Demo\n", encoding="utf-8")

    with patch("bernstein.cli.run_cmd.load_plan_from_yaml", return_value=[object(), object(), object()]):
        estimate = _estimate_run_preview(
            workdir=tmp_path,
            plan_file=plan_file,
            goal=None,
            seed_file=None,
            model_override="sonnet",
        )

    assert estimate.task_count == 3
    assert estimate.model == "sonnet"


def test_emit_preflight_runtime_warnings_aborts_on_high_cost() -> None:
    estimate = RunCostEstimate(task_count=12, model="sonnet", low_usd=4.0, high_usd=12.5)
    with patch("click.confirm", return_value=False):
        with pytest.raises(SystemExit):
            _emit_preflight_runtime_warnings(
                workdir=Path.cwd(),
                estimate=estimate,
                auto_approve=False,
                quiet=True,
            )


def test_wait_for_run_completion_returns_quiescent_status() -> None:
    status_calls = iter(
        [
            {"total": 2, "open": 1, "claimed": 1},
            {"total": 2, "open": 0, "claimed": 0},
        ]
    )
    health_calls = iter(
        [
            {"agent_count": 1},
            {"agent_count": 0},
        ]
    )
    clock = {"now": 0.0}

    def _fake_server_get(path: str):  # type: ignore[no-untyped-def]
        if path == "/status":
            return next(status_calls)
        return next(health_calls)

    def _fake_time() -> float:
        clock["now"] += 0.1
        return clock["now"]

    with (
        patch("bernstein.cli.run_cmd.server_get", side_effect=_fake_server_get),
        patch("bernstein.cli.run_cmd.time.sleep", return_value=None),
        patch("bernstein.cli.run_cmd.time.time", side_effect=_fake_time),
    ):
        result = _wait_for_run_completion(timeout_s=5.0)

    assert result == {"total": 2, "open": 0, "claimed": 0}


def test_finalize_run_output_quiet_uses_summary_only() -> None:
    with (
        patch("bernstein.cli.run_cmd._wait_for_run_completion") as wait_for_completion,
        patch("bernstein.cli.run_cmd._show_run_summary") as show_summary,
    ):
        _finalize_run_output(quiet=True)

    wait_for_completion.assert_called_once()
    show_summary.assert_called_once()
