"""``bernstein run --wait`` blocks for the run's outcome without silencing it.

A non-interactive ``bernstein run`` detaches once the first agent is up, so a
caller that wants the run's exit code had to pass ``--quiet`` -- which also
suppresses the progress output. The two intents were welded together, and CI
callers had to give up their log to get an exit code. ``--wait`` asks only for
the wait.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

QUIESCENT_UNHEALTHY = {"total": 1, "open": 0, "claimed": 0, "done": 0, "failed": 1}
FULL_UNHEALTHY = {"total": 1, "open": 0, "claimed": 0, "in_progress": 0, "orphaned": 0, "done": 0, "failed": 1}
HEALTH = {"agent_count": 0, "components": {"spawner": {"status": "down"}}}


def _server(path: str) -> Any:
    if path == "/status":
        return QUIESCENT_UNHEALTHY
    if path == "/health":
        return HEALTH
    if path == "/tasks/counts":
        return FULL_UNHEALTHY
    return None


def _finalize(*, quiet: bool, wait: bool, supports_textual: bool, is_tty: bool) -> dict[str, Any]:
    """Drive one branch. Returns which collaborators the branch reached."""
    import bernstein.cli.run_bootstrap as rb
    from bernstein.cli.run_preflight import _finalize_run_output

    caps = MagicMock(supports_textual=supports_textual, is_tty=is_tty)
    dashboard_app = MagicMock()
    dashboard_app.return_value = MagicMock(_restart_on_exit=False)
    seen: dict[str, Any] = {"exit_code": 0}

    with (
        patch.object(rb, "server_get", side_effect=_server),
        patch("bernstein.cli.terminal_caps.detect_capabilities", return_value=caps),
        patch("bernstein.cli.run_preflight._show_run_summary"),
        patch("bernstein.cli.run_preflight._try_fallback_display"),
        patch("bernstein.cli.run_preflight._drain_completed_backlog_files"),
        patch("bernstein.cli.run_preflight._raise_if_no_plan_after_spawn") as no_plan,
        patch("bernstein.cli.dashboard.BernsteinApp", dashboard_app),
        patch.object(rb, "_wait_for_run_completion", return_value=QUIESCENT_UNHEALTHY) as waited,
        patch.object(rb, "_await_first_spawn_outcome", return_value=("spawned", None)) as detached,
        patch.object(rb, "_poll_no_plan_after_spawn", return_value=None),
    ):
        try:
            _finalize_run_output(quiet=quiet, wait=wait)
        except SystemExit as exc:
            seen["exit_code"] = int(exc.code or 0)
        seen["waited"] = waited.called
        seen["detached"] = detached.called
        seen["dashboard"] = dashboard_app.called
        seen["narrate_wait"] = no_plan.call_args.kwargs.get("narrate_wait") if no_plan.called else None
    return seen


def test_wait_is_a_registered_run_option() -> None:
    """The flag exists on ``bernstein run``, not only in the dispatcher."""
    import inspect

    from bernstein.cli.run_bootstrap import _run_impl, run

    assert "--wait" in {p.opts[0] for p in run.params}
    assert "wait" in inspect.signature(run.callback).parameters
    assert "wait" in inspect.signature(_run_impl).parameters


@pytest.mark.parametrize(
    ("supports_textual", "is_tty"),
    [(True, True), (False, True), (False, False)],
)
def test_wait_blocks_for_the_outcome_on_every_terminal(supports_textual: bool, is_tty: bool) -> None:
    """``--wait`` means the run's outcome, so no branch may detach or open a TUI."""
    seen = _finalize(quiet=False, wait=True, supports_textual=supports_textual, is_tty=is_tty)

    assert seen["waited"] is True
    assert seen["detached"] is False
    assert seen["dashboard"] is False
    # Non-zero is the point; the exact mapping is pinned by
    # test_run_outcome_reaches_every_branch and not restated here.
    assert seen["exit_code"] != 0


def test_wait_keeps_the_progress_output_that_quiet_suppresses() -> None:
    """The whole point of the flag: wait without going quiet."""
    assert _finalize(quiet=False, wait=True, supports_textual=False, is_tty=False)["narrate_wait"] is True
    assert _finalize(quiet=True, wait=False, supports_textual=False, is_tty=False)["narrate_wait"] is False


def test_quiet_still_waits_exactly_as_before() -> None:
    """``--quiet`` keeps implying the wait; the flag is additive."""
    seen = _finalize(quiet=True, wait=False, supports_textual=False, is_tty=False)

    assert seen["waited"] is True
    assert seen["detached"] is False


def test_without_either_flag_a_non_interactive_run_still_detaches() -> None:
    """Default behaviour is unchanged -- this is what makes the flag necessary."""
    seen = _finalize(quiet=False, wait=False, supports_textual=False, is_tty=False)

    assert seen["waited"] is False
    assert seen["detached"] is True
