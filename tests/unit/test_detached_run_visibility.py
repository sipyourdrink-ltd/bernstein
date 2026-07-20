"""Regression tests for issue gh-2744.

Non-interactive ``bernstein run`` detaches right after bootstrap.  When the
detached spawner refused the very first spawn (for example a model that no
adapter accepts), the CLI printed an empty summary and exited 0 - the refusal
reason never reached the terminal.

Two layers are covered:

1. ``_await_first_spawn_outcome`` - the bounded poll that classifies the
   first spawn outcome from the task server (``/health`` agent count plus
   failed-task reasons).

2. ``_finalize_run_output`` (non-TTY branch) - a refused first spawn must
   print the reason and exit non-zero; a healthy first spawn must print an
   explicit detach notice and keep the current exit-0 behaviour.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import time
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from bernstein.cli.run_bootstrap import _await_first_spawn_outcome

REFUSAL_REASON = (
    "Spawn failed permanently (model_not_configured): Refusing to spawn "
    "with a nonsensical model - configure role_model_policy or an adapter default."
)


def _server_get_factory(
    health: dict[str, Any] | None,
    failed_tasks: list[dict[str, Any]] | None,
) -> Any:
    """Build a ``server_get`` stub serving /health and /tasks?status=failed."""

    def _server_get(path: str) -> Any:
        if path.startswith("/health"):
            return health
        if path.startswith("/tasks?status=failed"):
            if failed_tasks is None:
                return None
            return {"tasks": failed_tasks, "total": len(failed_tasks), "limit": 50, "offset": 0}
        return None

    return _server_get


# ---------------------------------------------------------------------------
# Layer 1: outcome classification
# ---------------------------------------------------------------------------


class TestAwaitFirstSpawnOutcome:
    def test_permanent_spawn_failure_is_refused_with_reason(self) -> None:
        """A non-transient spawn failure surfaces immediately with its reason."""
        stub = _server_get_factory(
            health={"agent_count": 0},
            failed_tasks=[
                {
                    "id": "t1",
                    "status": "failed",
                    "result_summary": REFUSAL_REASON,
                    "completed_at": time.time(),
                }
            ],
        )
        with patch("bernstein.cli.run_bootstrap.server_get", side_effect=stub):
            outcome, reason = _await_first_spawn_outcome(timeout_s=1.0, poll_interval_s=0.01)
        assert outcome == "refused"
        assert reason == REFUSAL_REASON

    def test_live_agent_short_circuits_to_spawned(self) -> None:
        stub = _server_get_factory(health={"agent_count": 2}, failed_tasks=[])
        with patch("bernstein.cli.run_bootstrap.server_get", side_effect=stub):
            outcome, reason = _await_first_spawn_outcome(timeout_s=1.0, poll_interval_s=0.01)
        assert outcome == "spawned"
        assert reason is None

    def test_unreachable_server_returns_unknown_quickly(self) -> None:
        """No server means no verdict - bail out well before the timeout."""
        with patch("bernstein.cli.run_bootstrap.server_get", return_value=None):
            start = time.monotonic()
            outcome, reason = _await_first_spawn_outcome(timeout_s=30.0, poll_interval_s=0.01)
            elapsed = time.monotonic() - start
        assert outcome == "unknown"
        assert reason is None
        assert elapsed < 5.0

    def test_transient_failure_escalates_only_at_deadline(self) -> None:
        """A transient first failure gets the retry window before we give up."""
        transient = "Spawn failed (transient, attempt 1): adapter busy"
        stub = _server_get_factory(
            health={"agent_count": 0},
            failed_tasks=[
                {
                    "id": "t1",
                    "status": "failed",
                    "result_summary": transient,
                    "completed_at": time.time(),
                }
            ],
        )
        with patch("bernstein.cli.run_bootstrap.server_get", side_effect=stub):
            outcome, reason = _await_first_spawn_outcome(timeout_s=0.05, poll_interval_s=0.01)
        assert outcome == "refused"
        assert reason == transient

    def test_stale_failed_task_from_prior_run_is_ignored(self) -> None:
        """Failed tasks reloaded from a previous run must not fail this one."""
        stub = _server_get_factory(
            health={"agent_count": 0},
            failed_tasks=[
                {
                    "id": "t-old",
                    "status": "failed",
                    "result_summary": REFUSAL_REASON,
                    "completed_at": time.time() - 3600.0,
                }
            ],
        )
        with patch("bernstein.cli.run_bootstrap.server_get", side_effect=stub):
            outcome, _reason = _await_first_spawn_outcome(timeout_s=0.05, poll_interval_s=0.01)
        assert outcome == "unknown"

    def test_non_spawn_failure_reasons_are_ignored(self) -> None:
        """Only spawn failures count as refused-before-any-work."""
        stub = _server_get_factory(
            health={"agent_count": 0},
            failed_tasks=[
                {
                    "id": "t1",
                    "status": "failed",
                    "result_summary": "verification failed: tests red",
                    "completed_at": time.time(),
                }
            ],
        )
        with patch("bernstein.cli.run_bootstrap.server_get", side_effect=stub):
            outcome, _reason = _await_first_spawn_outcome(timeout_s=0.05, poll_interval_s=0.01)
        assert outcome == "unknown"


# ---------------------------------------------------------------------------
# Layer 2: non-TTY exit path
# ---------------------------------------------------------------------------

_NON_TTY_CAPS = MagicMock(supports_textual=False, is_tty=False)


class TestFinalizeRunOutputNonTty:
    def test_refused_first_spawn_prints_reason_and_exits_nonzero(self, capsys: pytest.CaptureFixture[str]) -> None:
        from bernstein.cli.run_preflight import _finalize_run_output

        with (
            patch("bernstein.cli.terminal_caps.detect_capabilities", return_value=_NON_TTY_CAPS),
            patch(
                "bernstein.cli.run_bootstrap._await_first_spawn_outcome",
                return_value=("refused", REFUSAL_REASON),
            ),
            patch("bernstein.cli.run_preflight._show_run_summary") as show_summary,
            patch("bernstein.cli.run_preflight._drain_completed_backlog_files") as drain,
        ):
            with pytest.raises(SystemExit) as excinfo:
                _finalize_run_output(quiet=False)

        assert excinfo.value.code == 1
        out = capsys.readouterr().out
        assert "Run failed before any work started" in out
        assert "nonsensical" in out
        assert "bernstein status" in out
        assert "retrospective.md" in out
        show_summary.assert_called_once()
        # Cleanup must still run despite the non-zero exit.
        drain.assert_called_once()

    def test_healthy_first_spawn_prints_detach_notice(self, capsys: pytest.CaptureFixture[str]) -> None:
        from bernstein.cli.run_preflight import _finalize_run_output

        with (
            patch("bernstein.cli.terminal_caps.detect_capabilities", return_value=_NON_TTY_CAPS),
            patch(
                "bernstein.cli.run_bootstrap._await_first_spawn_outcome",
                return_value=("spawned", None),
            ),
            patch("bernstein.cli.run_preflight._show_run_summary") as show_summary,
            patch("bernstein.cli.run_preflight._drain_completed_backlog_files"),
        ):
            _finalize_run_output(quiet=False)

        out = capsys.readouterr().out
        assert "continues in the background" in out
        assert "bernstein status" in out
        show_summary.assert_called_once()

    def test_unknown_outcome_keeps_exit_zero_with_detach_notice(self, capsys: pytest.CaptureFixture[str]) -> None:
        from bernstein.cli.run_preflight import _finalize_run_output

        with (
            patch("bernstein.cli.terminal_caps.detect_capabilities", return_value=_NON_TTY_CAPS),
            patch(
                "bernstein.cli.run_bootstrap._await_first_spawn_outcome",
                return_value=("unknown", None),
            ),
            patch("bernstein.cli.run_preflight._show_run_summary"),
            patch("bernstein.cli.run_preflight._drain_completed_backlog_files"),
        ):
            _finalize_run_output(quiet=False)

        out = capsys.readouterr().out
        assert "continues in the background" in out
