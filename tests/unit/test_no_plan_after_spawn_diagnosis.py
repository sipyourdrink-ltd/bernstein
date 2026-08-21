"""Regression tests for issue #3528: a spawn failure must not report success.

Ground truth (from the issue's reproduction): the manager agent's process
died about a second after spawn. The single root task (its own decompose
task) stayed ``claimed`` forever -- no plan was ever produced. The
orchestrator subprocess kept ticking (it is not "gone"), and the task never
reached quiescence (it is stuck ``claimed``, not terminal), so neither of
the CLI's two existing terminal-state checks fired. The non-interactive
detach path printed "Run continues in the background" and exited 0.

Three layers are covered:

1. ``_poll_no_plan_after_spawn`` -- the single-poll detector that recognises
   this shape from ``/status`` + ``/health`` without a confirmation window.
2. ``_finalize_run_output`` (non-TTY branch) -- a spawned-then-dead agent
   with no plan must raise a categorised error instead of printing the
   detach notice and returning.
3. The structured taxonomy itself -- the new ``NO_PLAN_PRODUCED`` category
   carries a real sysexits.h exit code and a non-empty hint, exactly like
   every other first-run category.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from bernstein.cli.run_bootstrap import _poll_no_plan_after_spawn
from bernstein.core.errors import (
    BernsteinFirstRunError,
    ErrorCategory,
    categorize_exception,
    exit_code_for,
    hint_for,
)


def _server_get_factory(
    *,
    status: dict[str, Any] | None,
    health: dict[str, Any] | None,
    counts: dict[str, Any] | None = None,
) -> Any:
    """Build a ``server_get`` stub serving /status, /health, /tasks/counts."""

    def _server_get(path: str) -> Any:
        if path.startswith("/status"):
            return status
        if path.startswith("/health"):
            return health
        if path.startswith("/tasks/counts"):
            return counts
        return None

    return _server_get


# ---------------------------------------------------------------------------
# Layer 1: _poll_no_plan_after_spawn detection
# ---------------------------------------------------------------------------


class TestPollNoPlanAfterSpawn:
    def test_dead_agent_with_stuck_root_task_is_detected(self) -> None:
        """The reproduced shape: one claimed task, zero live agents, no plan."""
        stub = _server_get_factory(
            status={"total": 1, "open": 0, "claimed": 1, "done": 0, "failed": 0},
            health={"agent_count": 0},
            counts={"total": 1, "open": 0, "claimed": 1, "in_progress": 0, "orphaned": 0, "done": 0, "failed": 0},
        )
        with patch("bernstein.cli.run_bootstrap.server_get", side_effect=stub):
            result = _poll_no_plan_after_spawn()
        assert result is not None
        assert result["total"] == 1

    def test_orphaned_root_task_is_also_detected(self) -> None:
        """A task the reaper marked orphaned (rather than left claimed) counts too.

        ``/status`` has no bucket for ``orphaned``, so it reads open=claimed=0
        even though one task is still stuck -- the full histogram is what
        catches it.
        """
        stub = _server_get_factory(
            status={"total": 1, "open": 0, "claimed": 0, "done": 0, "failed": 0},
            health={"agent_count": 0},
            counts={"total": 1, "open": 0, "claimed": 0, "in_progress": 0, "orphaned": 1, "done": 0, "failed": 0},
        )
        with patch("bernstein.cli.run_bootstrap.server_get", side_effect=stub):
            result = _poll_no_plan_after_spawn()
        assert result is not None

    def test_live_agent_is_not_a_death(self) -> None:
        """An agent still registered alive must never be diagnosed as dead."""
        stub = _server_get_factory(
            status={"total": 1, "open": 0, "claimed": 1, "done": 0, "failed": 0},
            health={"agent_count": 1},
        )
        with patch("bernstein.cli.run_bootstrap.server_get", side_effect=stub):
            result = _poll_no_plan_after_spawn()
        assert result is None

    def test_startup_window_with_no_declared_tasks_is_not_a_death(self) -> None:
        """Before anything is declared, agent_count == 0 means 'not yet', not 'died'."""
        stub = _server_get_factory(
            status={"total": 0, "open": 0, "claimed": 0, "done": 0, "failed": 0},
            health={"agent_count": 0},
        )
        with patch("bernstein.cli.run_bootstrap.server_get", side_effect=stub):
            result = _poll_no_plan_after_spawn()
        assert result is None

    def test_decomposed_plan_is_not_flagged(self) -> None:
        """Once the manager decomposed the goal, a plan exists -- a different
        agent later dying is a different failure, not this one."""
        stub = _server_get_factory(
            status={"total": 3, "open": 2, "claimed": 0, "done": 1, "failed": 0},
            health={"agent_count": 0},
            counts={"total": 3, "open": 2, "claimed": 0, "in_progress": 0, "orphaned": 0, "done": 1, "failed": 0},
        )
        with patch("bernstein.cli.run_bootstrap.server_get", side_effect=stub):
            result = _poll_no_plan_after_spawn()
        assert result is None

    def test_completed_run_is_not_flagged(self) -> None:
        """A run whose one task actually finished must not be diagnosed as a death."""
        stub = _server_get_factory(
            status={"total": 1, "open": 0, "claimed": 0, "done": 1, "failed": 0},
            health={"agent_count": 0},
            counts={"total": 1, "open": 0, "claimed": 0, "in_progress": 0, "orphaned": 0, "done": 1, "failed": 0},
        )
        with patch("bernstein.cli.run_bootstrap.server_get", side_effect=stub):
            result = _poll_no_plan_after_spawn()
        assert result is None

    def test_unclaimed_open_task_is_not_a_death(self) -> None:
        """An ``open`` task with no live agent is the ordinary gap before the
        next agent spawns for it, not evidence anything died."""
        stub = _server_get_factory(
            status={"total": 1, "open": 1, "claimed": 0, "done": 0, "failed": 0},
            health={"agent_count": 0},
            counts={"total": 1, "open": 1, "claimed": 0, "in_progress": 0, "orphaned": 0, "done": 0, "failed": 0},
        )
        with patch("bernstein.cli.run_bootstrap.server_get", side_effect=stub):
            result = _poll_no_plan_after_spawn()
        assert result is None

    def test_unreachable_server_returns_no_verdict(self) -> None:
        with patch("bernstein.cli.run_bootstrap.server_get", return_value=None):
            result = _poll_no_plan_after_spawn()
        assert result is None


# ---------------------------------------------------------------------------
# Layer 2: non-TTY exit path
# ---------------------------------------------------------------------------

_NON_TTY_CAPS = MagicMock(supports_textual=False, is_tty=False)


class TestFinalizeRunOutputNoPlan:
    def test_dead_agent_no_plan_raises_categorised_error(self) -> None:
        """A spawned-then-dead agent with no plan must not print the detach
        notice and exit 0 -- it must surface a stated, categorised reason."""
        from bernstein.cli.run_preflight import _finalize_run_output

        with (
            patch("bernstein.cli.terminal_caps.detect_capabilities", return_value=_NON_TTY_CAPS),
            patch(
                "bernstein.cli.run_bootstrap._await_first_spawn_outcome",
                return_value=("spawned", None),
            ),
            patch(
                "bernstein.cli.run_bootstrap._poll_no_plan_after_spawn",
                return_value={"total": 1, "claimed": 1},
            ),
            patch("bernstein.cli.run_preflight._show_run_summary"),
            patch("bernstein.cli.run_bootstrap._poll_quiescent_status", return_value=None),
            patch("bernstein.cli.run_preflight._drain_completed_backlog_files") as drain,
        ):
            with pytest.raises(BernsteinFirstRunError) as excinfo:
                _finalize_run_output(quiet=False)

        assert excinfo.value.category is ErrorCategory.NO_PLAN_PRODUCED
        # Cleanup must still run even though the diagnosis propagates as an
        # exception rather than a caught SystemExit.
        drain.assert_called_once()

    def test_healthy_spawn_with_no_death_keeps_detach_notice(self, capsys: pytest.CaptureFixture[str]) -> None:
        """No regression: a normally-progressing run keeps today's fast detach."""
        from bernstein.cli.run_preflight import _finalize_run_output

        with (
            patch("bernstein.cli.terminal_caps.detect_capabilities", return_value=_NON_TTY_CAPS),
            patch(
                "bernstein.cli.run_bootstrap._await_first_spawn_outcome",
                return_value=("spawned", None),
            ),
            patch("bernstein.cli.run_bootstrap._poll_no_plan_after_spawn", return_value=None),
            patch("bernstein.cli.run_preflight._show_run_summary"),
            patch("bernstein.cli.run_bootstrap._poll_quiescent_status", return_value=None),
            patch("bernstein.cli.run_preflight._drain_completed_backlog_files"),
        ):
            _finalize_run_output(quiet=False)

        out = capsys.readouterr().out
        assert "continues in the background" in out


# ---------------------------------------------------------------------------
# Layer 3: taxonomy wiring
# ---------------------------------------------------------------------------


class TestNoPlanProducedCategory:
    def test_carries_a_sysexits_exit_code(self) -> None:
        code = exit_code_for(ErrorCategory.NO_PLAN_PRODUCED)
        assert 64 <= code <= 78

    def test_categorize_exception_preserves_the_category(self) -> None:
        exc = BernsteinFirstRunError(
            "Spawned agent exited before producing a work plan",
            category=ErrorCategory.NO_PLAN_PRODUCED,
        )
        assert categorize_exception(exc) is ErrorCategory.NO_PLAN_PRODUCED

    def test_hint_renders_without_error(self) -> None:
        panel = hint_for(ErrorCategory.NO_PLAN_PRODUCED)
        assert panel is not None
