"""Tests for Track B run-command helpers."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from bernstein.cli.run_bootstrap import _signal_orchestrator_shutdown
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

    with patch("bernstein.cli.run_preflight.load_plan_from_yaml", return_value=[object(), object(), object()]):
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
        patch("bernstein.cli.run_bootstrap.server_get", side_effect=_fake_server_get),
        patch("bernstein.cli.run_bootstrap.time.sleep", return_value=None),
        patch("bernstein.cli.run_bootstrap.time.time", side_effect=_fake_time),
        patch("bernstein.cli.run_bootstrap._signal_orchestrator_shutdown") as shutdown_signal,
    ):
        result = _wait_for_run_completion(timeout_s=5.0)

    assert result == {"total": 2, "open": 0, "claimed": 0}
    # Defect-3 fix: completion detection must invoke the belt-and-braces
    # shutdown signal exactly once, as a backstop to the orchestrator's own
    # quiescence self-stop.
    shutdown_signal.assert_called_once()


def test_wait_for_run_completion_timeout_does_not_signal_shutdown() -> None:
    """If quiescence is never observed (timeout), no shutdown signal should fire --
    we only know completion happened when total > 0 and open == claimed == 0."""
    clock = {"now": 0.0}

    def _fake_time() -> float:
        clock["now"] += 10.0
        return clock["now"]

    with (
        patch("bernstein.cli.run_bootstrap.server_get", return_value={"total": 2, "open": 1, "claimed": 0}),
        patch("bernstein.cli.run_bootstrap.time.sleep", return_value=None),
        patch("bernstein.cli.run_bootstrap.time.time", side_effect=_fake_time),
        patch("bernstein.cli.run_bootstrap._signal_orchestrator_shutdown") as shutdown_signal,
    ):
        _wait_for_run_completion(timeout_s=5.0)

    shutdown_signal.assert_not_called()


def test_signal_orchestrator_shutdown_posts_to_shutdown_endpoint() -> None:
    """Happy path: orchestrator still up, POST /shutdown is sent and acknowledged."""
    fake_response = type(
        "FakeResponse",
        (),
        {
            "status_code": 200,
            "content": b'{"status": "shutting_down"}',
            "json": lambda self: {"status": "shutting_down"},
            "raise_for_status": lambda self: None,
        },
    )()

    with patch("bernstein.cli.run_bootstrap.httpx.post", return_value=fake_response) as post:
        _signal_orchestrator_shutdown(reason="test")

    post.assert_called_once()
    called_kwargs = post.call_args.kwargs
    assert called_kwargs["json"] == {"reason": "test"}


def test_signal_orchestrator_shutdown_treats_connection_refused_as_success() -> None:
    """The orchestrator's own quiescence self-stop may already have torn the
    server down by the time the CLI signals -- connection-refused must be
    logged and treated as success, never raised."""
    import httpx

    with patch("bernstein.cli.run_bootstrap.httpx.post", side_effect=httpx.ConnectError("refused")):
        # Must not raise.
        _signal_orchestrator_shutdown(reason="test")


def test_signal_orchestrator_shutdown_treats_404_as_success() -> None:
    """A 404 (route torn down after self-stop) is also treated as success."""
    fake_response = type(
        "FakeResponse",
        (),
        {"status_code": 404, "content": b"", "json": lambda self: None, "raise_for_status": lambda self: None},
    )()

    with patch("bernstein.cli.run_bootstrap.httpx.post", return_value=fake_response):
        # Must not raise.
        _signal_orchestrator_shutdown(reason="test")


def test_finalize_run_output_quiet_uses_summary_only() -> None:
    with (
        patch("bernstein.cli.run_bootstrap._wait_for_run_completion") as wait_for_completion,
        patch("bernstein.cli.run_preflight._show_run_summary") as show_summary,
    ):
        _finalize_run_output(quiet=True)

    wait_for_completion.assert_called_once()
    show_summary.assert_called_once()
