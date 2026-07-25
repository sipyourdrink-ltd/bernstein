"""The run-outcome signal must not depend on which renderer the operator got.

Two gaps kept a run that did not meet its goal reporting success (issue #3010):

* the exit-code mapping existed only on the ``--quiet`` branch of
  ``_finalize_run_output``, and nothing turns ``--quiet`` on automatically;
* the interim retrospective was written without the task histogram, so a run
  with no terminal tasks at all rendered ``0/0`` and HEALTHY.

Both are pinned here. What this file does NOT cover is the third gap: in the
reported shape the orchestrator never exits, because its self-stop is nested
under "some task reached a terminal state". That needs a change to the main
tick loop and is tracked separately.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

QUIESCENT_UNHEALTHY = {"total": 1, "open": 0, "claimed": 0, "done": 0, "failed": 1}
QUIESCENT_HEALTHY = {"total": 1, "open": 0, "claimed": 0, "done": 1, "failed": 0}
FULL_UNHEALTHY = {"total": 1, "open": 0, "claimed": 0, "in_progress": 0, "orphaned": 0, "done": 0, "failed": 1}
FULL_HEALTHY = {"total": 1, "open": 0, "claimed": 0, "in_progress": 0, "orphaned": 0, "done": 1, "failed": 0}
HEALTH = {"agent_count": 0, "components": {"spawner": {"status": "down"}}}


def _server(status: dict[str, Any], full_counts: dict[str, Any]) -> Any:
    def fake_get(path: str) -> Any:
        if path == "/status":
            return status
        if path == "/health":
            return HEALTH
        if path == "/tasks/counts":
            return full_counts
        return None

    return fake_get


def _run_branch(
    *,
    supports_textual: bool,
    is_tty: bool,
    status: dict[str, Any] = QUIESCENT_UNHEALTHY,
    full_counts: dict[str, Any] = FULL_UNHEALTHY,
) -> int:
    """Drive one _finalize_run_output branch. Returns the process exit code."""
    import bernstein.cli.run_bootstrap as rb
    from bernstein.cli.run_preflight import _finalize_run_output

    caps = MagicMock(supports_textual=supports_textual, is_tty=is_tty)
    # `_restart_on_exit` must be explicitly falsy: a bare MagicMock attribute is
    # truthy, and the dashboard branch reads it to decide whether to re-exec the
    # whole CLI over this process.
    dashboard_app = MagicMock()
    dashboard_app.return_value = MagicMock(_restart_on_exit=False)
    with (
        patch.object(rb, "server_get", side_effect=_server(status, full_counts)),
        patch("bernstein.cli.terminal_caps.detect_capabilities", return_value=caps),
        patch("bernstein.cli.run_preflight._show_run_summary"),
        patch("bernstein.cli.run_preflight._try_fallback_display"),
        patch("bernstein.cli.run_preflight._drain_completed_backlog_files"),
        patch("bernstein.cli.run_bootstrap._await_first_spawn_outcome", return_value=("spawned", None)),
        patch("bernstein.cli.run_bootstrap.exec_restart", side_effect=AssertionError("must not re-exec")),
        patch("bernstein.cli.dashboard.BernsteinApp", dashboard_app),
    ):
        try:
            _finalize_run_output(quiet=False)
        except SystemExit as exc:
            return int(exc.code or 0)
    return 0


# ---------------------------------------------------------------------------
# Gap A: the exit code existed only on --quiet.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("branch", "supports_textual", "is_tty"),
    [
        ("textual dashboard", True, True),
        ("rich fallback", False, True),
        ("non-interactive", False, False),
    ],
)
def test_every_branch_reports_a_finished_unhealthy_run(branch: str, supports_textual: bool, is_tty: bool) -> None:
    """A finished run whose only task failed must not exit 0 on any branch.

    ``--quiet`` is not enabled automatically and no documented workflow passes
    it, so binding the outcome signal to that flag left every ordinary
    invocation reporting success. The reported reproduction of issue #3010 went
    down the non-interactive branch, which prints "Run continues in the
    background" and, before this, always exited 0.
    """
    from bernstein.core.retrospective import EXIT_RUN_UNHEALTHY

    assert _run_branch(supports_textual=supports_textual, is_tty=is_tty) == EXIT_RUN_UNHEALTHY, (
        f"the {branch} branch must map a finished, unhealthy run onto the outcome exit code"
    )


@pytest.mark.parametrize(
    ("branch", "supports_textual", "is_tty"),
    [
        ("textual dashboard", True, True),
        ("rich fallback", False, True),
        ("non-interactive", False, False),
    ],
)
def test_every_branch_still_exits_zero_on_a_healthy_run(branch: str, supports_textual: bool, is_tty: bool) -> None:
    """Control: a run that met its goal still exits 0 everywhere."""
    assert (
        _run_branch(
            supports_textual=supports_textual,
            is_tty=is_tty,
            status=QUIESCENT_HEALTHY,
            full_counts=FULL_HEALTHY,
        )
        == 0
    )


def test_a_still_running_run_is_not_reported_as_failed_on_any_branch() -> None:
    """Control: the branches that do not wait must not guess.

    They check once and report only an already-quiescent run. A run still in
    flight, or a server they cannot reach, is not a verdict, and the
    non-interactive branch in particular detaches by design.
    """
    in_flight = {"total": 1, "open": 1, "claimed": 0, "done": 0, "failed": 0}
    full_in_flight = {"total": 1, "open": 1, "claimed": 0, "in_progress": 0, "orphaned": 0, "done": 0, "failed": 0}
    for supports_textual, is_tty in ((True, True), (False, True), (False, False)):
        assert (
            _run_branch(
                supports_textual=supports_textual,
                is_tty=is_tty,
                status=in_flight,
                full_counts=full_in_flight,
            )
            == 0
        )

    import bernstein.cli.run_bootstrap as rb

    with patch.object(rb, "server_get", return_value=None):
        assert rb._poll_quiescent_status() is None


def test_single_poll_path_never_produces_the_orchestrator_gone_verdict(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The non-waiting branches report quiescence only, never absence.

    "The orchestrator is gone" is an inference from a process not being there,
    and it is only trustworthy after several observations spanning the recovery
    supervisor's window. A single poll cannot supply that, so this path must not
    reach for it even though the pidfile evidence is right there.
    """
    import bernstein.cli.run_bootstrap as rb

    runtime = tmp_path / ".sdd" / "runtime"
    runtime.mkdir(parents=True)
    (runtime / "spawner.pid").write_text("999999")  # dead: the gone shape
    monkeypatch.chdir(tmp_path)

    stuck = {"total": 1, "open": 1, "claimed": 0, "done": 0, "failed": 0}
    full_stuck = {"total": 1, "open": 1, "claimed": 0, "in_progress": 0, "orphaned": 0, "done": 0, "failed": 0}
    with patch.object(rb, "server_get", side_effect=_server(stuck, full_stuck)):
        assert rb._poll_quiescent_status() is None


def test_single_poll_path_respects_the_in_progress_veto(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """One definition of quiescence, shared with the waiting path.

    ``/status`` has no bucket for ``in_progress`` or ``orphaned``, so on its
    numbers alone a run with a task in either reads as finished. The full
    histogram vetoes that here exactly as it does in the wait.
    """
    import bernstein.cli.run_bootstrap as rb

    monkeypatch.chdir(tmp_path)
    looks_empty = {"total": 1, "open": 0, "claimed": 0, "done": 0, "failed": 0}
    really_running = {"total": 1, "open": 0, "claimed": 0, "in_progress": 1, "orphaned": 0, "done": 0, "failed": 0}
    with patch.object(rb, "server_get", side_effect=_server(looks_empty, really_running)):
        assert rb._poll_quiescent_status() is None

    all_done = {"total": 1, "open": 0, "claimed": 0, "in_progress": 0, "orphaned": 0, "done": 1, "failed": 0}
    with patch.object(rb, "server_get", side_effect=_server(looks_empty, all_done)):
        assert rb._poll_quiescent_status() is not None


# ---------------------------------------------------------------------------
# Gap C: the interim retrospective was written without the histogram.
# ---------------------------------------------------------------------------


def test_interim_retrospective_receives_the_task_histogram(tmp_path: Path) -> None:
    """A run with no terminal tasks must not render as 0/0 HEALTHY.

    ``generate_retrospective`` derives "declared but never finished" from the
    full histogram. The mid-run call sites passed nothing, so
    ``count_incomplete_declared(None)`` was 0 and every such run was HEALTHY.
    That artefact matters more than "interim" suggests: when quiescence holds
    with zero terminal tasks the tick loop never exits, so no final
    retrospective is ever written and this is the run's only verdict.
    """
    from bernstein.core.quality.retrospective import (
        count_incomplete_declared,
        run_healthy_from_status_counts,
    )

    histogram = {"total": 1, "open": 1, "claimed": 0, "in_progress": 0, "orphaned": 0, "done": 0, "failed": 0}
    assert count_incomplete_declared(None) == 0, "the None default is what made the report vacuously healthy"
    assert run_healthy_from_status_counts(None) is True
    assert count_incomplete_declared(histogram) == 1
    assert run_healthy_from_status_counts(histogram) is False

    # Both mid-run call sites must accept and forward the histogram.
    import inspect

    from bernstein.core.orchestration.orchestrator import Orchestrator
    from bernstein.core.orchestration.orchestrator_summary import generate_run_summary

    for fn in (Orchestrator._generate_run_summary, generate_run_summary):
        assert "full_status_counts" in inspect.signature(fn).parameters, (
            f"{fn.__qualname__} must accept the histogram, or its retrospective is vacuously healthy"
        )


def test_mid_run_retrospective_is_unhealthy_when_nothing_finished(tmp_path: Path) -> None:
    """End-to-end through the real writer: 0 done, 0 failed, 1 still open.

    This is the exact #3010 artefact. With the histogram threaded through, the
    report is not HEALTHY.
    """
    from bernstein.core.metrics import get_collector
    from bernstein.core.retrospective import generate_retrospective

    runtime_dir = tmp_path / ".sdd" / "runtime"
    runtime_dir.mkdir(parents=True)
    collector = get_collector(tmp_path / ".sdd" / "metrics")
    histogram = {"total": 1, "open": 1, "claimed": 0, "in_progress": 0, "orphaned": 0, "done": 0, "failed": 0}

    generate_retrospective(
        done_tasks=[],
        failed_tasks=[],
        collector=collector,
        runtime_dir=runtime_dir,
        run_start_ts=0.0,
        trigger_reason="mid-run",
        full_status_counts=histogram,
    )
    with_histogram = (runtime_dir / "retrospective.md").read_text(encoding="utf-8")

    generate_retrospective(
        done_tasks=[],
        failed_tasks=[],
        collector=collector,
        runtime_dir=runtime_dir,
        run_start_ts=0.0,
        trigger_reason="mid-run",
    )
    without_histogram = (runtime_dir / "retrospective.md").read_text(encoding="utf-8")

    assert "HEALTHY" in without_histogram, "baseline: without the histogram the report claims health"
    assert with_histogram != without_histogram, "the histogram must change the verdict, not just the prose"
    assert "HEALTHY" not in with_histogram.replace("UNHEALTHY", ""), (
        "a run where nothing reached a terminal state must not render as HEALTHY"
    )
