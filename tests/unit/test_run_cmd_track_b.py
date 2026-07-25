"""Tests for Track B run-command helpers."""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import time
from pathlib import Path
from types import SimpleNamespace
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
from bernstein.core.orchestration.process_utils import (
    LIVENESS_ALIVE,
    LIVENESS_GONE,
    LIVENESS_UNKNOWN,
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
        if path == "/tasks/counts":
            return None  # server without the full histogram: /status is the fallback
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


def test_wait_for_run_completion_timeout_returns_no_verdict() -> None:
    """A wait that times out while the ORCHESTRATOR IS STILL ALIVE returns None.

    Returning the last observed payload instead would hand the caller a
    mid-flight snapshot (open/claimed > 0 -- precisely why quiescence was never
    detected), and the run-outcome exit mapping would misreport a healthy
    long-running run as unhealthy. None is the explicit "no verdict" signal.
    """
    clock = {"now": 0.0}

    def _fake_time() -> float:
        clock["now"] += 10.0
        return clock["now"]

    with (
        patch("bernstein.cli.run_bootstrap.server_get", return_value={"total": 2, "open": 1, "claimed": 1}),
        patch("bernstein.cli.run_bootstrap.time.sleep", return_value=None),
        patch("bernstein.cli.run_bootstrap.time.time", side_effect=_fake_time),
        patch("bernstein.cli.run_bootstrap._orchestrator_liveness", return_value=(LIVENESS_ALIVE, 4242)),
        patch("bernstein.cli.run_bootstrap._signal_orchestrator_shutdown"),
    ):
        result = _wait_for_run_completion(timeout_s=5.0)

    assert result is None


def test_wait_for_run_completion_unreachable_server_returns_no_verdict() -> None:
    """An unreachable server for the whole wait is also "no verdict", not a failure."""
    clock = {"now": 0.0}

    def _fake_time() -> float:
        clock["now"] += 10.0
        return clock["now"]

    with (
        patch("bernstein.cli.run_bootstrap.server_get", return_value=None),
        patch("bernstein.cli.run_bootstrap.time.sleep", return_value=None),
        patch("bernstein.cli.run_bootstrap.time.time", side_effect=_fake_time),
        patch("bernstein.cli.run_bootstrap._orchestrator_liveness", return_value=(LIVENESS_ALIVE, 4242)),
        patch("bernstein.cli.run_bootstrap._signal_orchestrator_shutdown"),
    ):
        result = _wait_for_run_completion(timeout_s=5.0)

    assert result is None


# ---------------------------------------------------------------------------
# Issue #3010: orchestrator liveness -- not the task counts -- separates
# "still starting up" from "ended with work unfinished". Both show work
# outstanding.
#
# ``gone`` is never concluded from one reading: a dead orchestrator is a state
# the recovery supervisor restarts out of, so the verdict has to outlast it.
# The scripted sequences below therefore run for enough polls to either clear
# or fail to clear that confirmation window.
# ---------------------------------------------------------------------------

#: Ample for the confirmation window at the 5s-per-poll fake clock below.
_WAIT_TIMEOUT_S = 300.0


def _wait_with(status, *, liveness: list[tuple[str, int | None]], timeout_s: float = _WAIT_TIMEOUT_S):  # type: ignore[no-untyped-def]
    """Drive _wait_for_run_completion with a scripted (liveness, pid) sequence.

    The last entry repeats once the script runs out, so a one-entry script
    means "this state, held indefinitely".

    The clock advances monotonic and wall time together, as a real
    ``time.sleep`` does. The confirmation window is measured on monotonic, so a
    harness that fakes only ``time.time`` leaves it running on the real clock
    and no scripted sequence can ever satisfy it.
    """
    elapsed = {"s": 0.0}
    base_wall, base_mono = time.time(), time.monotonic()

    it = iter(liveness)

    def _fake_liveness(*_args, **_kwargs) -> tuple[str, int | None]:  # type: ignore[no-untyped-def]
        try:
            return next(it)
        except StopIteration:
            return liveness[-1]

    def _fake_get(path: str):  # type: ignore[no-untyped-def]
        # The full histogram mirrors /status here: these cases are all about
        # liveness, not about statuses /status cannot express.
        return status

    clock = SimpleNamespace(
        time=lambda: base_wall + elapsed["s"],
        monotonic=lambda: base_mono + elapsed["s"],
        sleep=lambda _s: elapsed.__setitem__("s", elapsed["s"] + 5.0),
    )

    with (
        patch("bernstein.cli.run_bootstrap.server_get", side_effect=_fake_get),
        patch("bernstein.cli.run_bootstrap.time", clock),
        patch("bernstein.cli.run_bootstrap._orchestrator_liveness", side_effect=_fake_liveness),
        patch("bernstein.cli.run_bootstrap._signal_orchestrator_shutdown"),
    ):
        return _wait_for_run_completion(timeout_s=timeout_s)


def test_startup_window_open_tasks_with_live_orchestrator_is_not_a_verdict() -> None:
    """State 1: tasks open + orchestrator ALIVE (startup) -> no verdict.

    Open tasks before the first spawn must never be mistaken for a finished
    run -- this is exactly why the counts alone cannot be the discriminator.
    """
    result = _wait_with(
        {"total": 1, "open": 1, "claimed": 0, "agent_count": 0},
        liveness=[(LIVENESS_ALIVE, 4242)],
    )
    assert result is None


def test_startup_window_before_pidfile_is_written_is_not_a_verdict() -> None:
    """State 1b: tasks open and NO pidfile yet -> still the startup window.

    "No pidfile" is ambiguous: not started yet, or an operator teardown removed
    it. Neither is death, so it must never be read as "gone".
    """
    result = _wait_with(
        {"total": 1, "open": 1, "claimed": 0, "agent_count": 0},
        liveness=[(LIVENESS_UNKNOWN, None)],
    )
    assert result is None


def test_orchestrator_gone_with_unfinished_tasks_is_a_verdict() -> None:
    """State 2: tasks non-terminal + orchestrator GONE (the #3010 shape).

    The orchestrator ran, then exited leaving the task `open`, and stayed gone
    for longer than any restart would have taken. It must be reported rather
    than waiting out the deadline and exiting 0.
    """
    status = {"total": 1, "open": 1, "claimed": 0, "done": 0, "failed": 0, "agent_count": 0}
    result = _wait_with(status, liveness=[(LIVENESS_ALIVE, 4242), (LIVENESS_GONE, 4242)])
    assert result == status


def test_stale_pidfile_with_dead_pid_is_a_verdict_without_seeing_it_alive() -> None:
    """State 2b: a crashed orchestrator that was never observed alive.

    A pidfile of this run naming a dead pid is positive evidence, so the
    verdict must not require having watched the process alive first -- it only
    requires the evidence to hold across the confirmation window.
    """
    status = {"total": 1, "open": 1, "claimed": 0, "done": 0, "failed": 0, "agent_count": 0}
    result = _wait_with(status, liveness=[(LIVENESS_GONE, 4242)])
    assert result == status


def test_transient_gone_reading_alone_is_not_a_verdict() -> None:
    """State 2c: one gone reading, then the supervisor's restart shows up.

    The restarted orchestrator appears alive under a new pid, which is exactly
    what the confirmation window exists to catch. No verdict.
    """
    status = {"total": 1, "open": 1, "claimed": 0, "done": 0, "failed": 0, "agent_count": 0}
    result = _wait_with(
        status,
        liveness=[(LIVENESS_GONE, 4242), (LIVENESS_ALIVE, 5150)],
        timeout_s=120.0,
    )
    assert result is None


def test_pid_change_mid_window_resets_the_confirmation_streak() -> None:
    """A different pid means something restarted it: the streak starts over.

    Two gone readings under one pid followed by two under another must not add
    up to a confirmed verdict, however many polls have gone by.
    """
    status = {"total": 1, "open": 1, "claimed": 0, "done": 0, "failed": 0, "agent_count": 0}
    result = _wait_with(
        status,
        liveness=[
            (LIVENESS_GONE, 4242),
            (LIVENESS_GONE, 4242),
            (LIVENESS_GONE, 5150),
            (LIVENESS_ALIVE, 5150),
        ],
        timeout_s=25.0,
    )
    assert result is None


def test_unknown_liveness_never_produces_a_verdict() -> None:
    """An unresolvable reading, held forever, is still not a verdict.

    This covers every ambiguous case at once: no pidfile, an unreadable one, a
    pidfile predating this run, and the two observers disagreeing. The wait
    times out and the run is left alone.
    """
    status = {"total": 1, "open": 1, "claimed": 0, "done": 0, "failed": 0, "agent_count": 0}
    result = _wait_with(status, liveness=[(LIVENESS_UNKNOWN, 4242)])
    assert result is None


def test_quiescent_run_is_a_verdict_regardless_of_orchestrator_state() -> None:
    """State 3: quiescent + all done -> verdict (healthy), as before."""
    status = {"total": 2, "open": 0, "claimed": 0, "done": 2, "failed": 0, "agent_count": 0}
    result = _wait_with(status, liveness=[(LIVENESS_UNKNOWN, None)])
    assert result == status


def test_live_agents_block_the_orchestrator_gone_verdict() -> None:
    """Belt-and-braces: if agents are still reported live, work may yet land."""
    result = _wait_with(
        {"total": 1, "open": 1, "claimed": 0, "agent_count": 2},
        liveness=[(LIVENESS_GONE, 4242)],
    )
    assert result is None


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
