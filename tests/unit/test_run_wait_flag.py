"""``bernstein run --wait`` blocks for the run's outcome without silencing it.

A non-interactive ``bernstein run`` detaches once the first agent is up, so a
caller that wants the run's exit code had to pass ``--quiet`` -- which also
suppresses the progress output. The two intents were welded together, and CI
callers had to give up their log to get an exit code. ``--wait`` asks only for
the wait, and takes the ceiling with it: a fleet that allows a run more than
the default hour has to be able to say so, or the CLI returns while its own
agents are still working.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock, patch

import pytest

QUIESCENT_UNHEALTHY = {"total": 1, "open": 0, "claimed": 0, "done": 0, "failed": 1}
FULL_UNHEALTHY = {"total": 1, "open": 0, "claimed": 0, "in_progress": 0, "orphaned": 0, "done": 0, "failed": 1}
HEALTH = {"agent_count": 0, "components": {"spawner": {"status": "down"}}}

_DEFAULT_S = 3600.0


def _server(path: str) -> Any:
    if path == "/status":
        return QUIESCENT_UNHEALTHY
    if path == "/health":
        return HEALTH
    if path == "/tasks/counts":
        return FULL_UNHEALTHY
    return None


def _finalize(*, quiet: bool, wait: float | None, supports_textual: bool, is_tty: bool) -> dict[str, Any]:
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
        seen["timeout_s"] = waited.call_args.kwargs.get("timeout_s") if waited.called else None
        seen["detached"] = detached.called
        seen["dashboard"] = dashboard_app.called
        seen["narrate_wait"] = no_plan.call_args.kwargs.get("narrate_wait") if no_plan.called else None
    return seen


def test_wait_is_a_registered_run_option() -> None:
    """The flag exists on ``bernstein run``, not only in the dispatcher."""
    import inspect

    from bernstein.cli.run_bootstrap import _run_impl, run

    option = {p.name: p for p in run.params}["wait"]
    assert "--wait" in option.opts
    assert "wait" in inspect.signature(run.callback).parameters
    assert "wait" in inspect.signature(_run_impl).parameters
    # Optional-value, not a bare flag: bare ``--wait`` still has to work, so a
    # revert to ``is_flag=True`` must fail here rather than in the fleet.
    assert option._flag_needs_value is True
    assert option.default is None


@pytest.mark.parametrize(
    ("supports_textual", "is_tty"),
    [(True, True), (False, True), (False, False)],
)
def test_wait_blocks_for_the_outcome_on_every_terminal(supports_textual: bool, is_tty: bool) -> None:
    """``--wait`` means the run's outcome, so no branch may detach or open a TUI."""
    seen = _finalize(quiet=False, wait=_DEFAULT_S, supports_textual=supports_textual, is_tty=is_tty)

    assert seen["waited"] is True
    assert seen["detached"] is False
    assert seen["dashboard"] is False
    # Non-zero is the point; the exact mapping is pinned by
    # test_run_outcome_reaches_every_branch and not restated here.
    assert seen["exit_code"] != 0


def test_wait_keeps_the_progress_output_that_quiet_suppresses() -> None:
    """The whole point of the flag: wait without going quiet."""
    assert _finalize(quiet=False, wait=_DEFAULT_S, supports_textual=False, is_tty=False)["narrate_wait"] is True
    assert _finalize(quiet=True, wait=None, supports_textual=False, is_tty=False)["narrate_wait"] is False


def test_quiet_still_waits_exactly_as_before() -> None:
    """``--quiet`` keeps implying the wait; the flag is additive."""
    seen = _finalize(quiet=True, wait=None, supports_textual=False, is_tty=False)

    assert seen["waited"] is True
    assert seen["detached"] is False


def test_without_either_flag_a_non_interactive_run_still_detaches() -> None:
    """Default behaviour is unchanged -- this is what makes the flag necessary."""
    seen = _finalize(quiet=False, wait=None, supports_textual=False, is_tty=False)

    assert seen["waited"] is False
    assert seen["detached"] is True


def test_bare_wait_uses_the_default_ceiling() -> None:
    """``--wait`` with no value keeps the hour the wait always had."""
    from bernstein.cli.run_bootstrap import _RUN_WAIT_DEFAULT_S, run

    ctx = run.make_context("run", ["--wait"])
    assert ctx.params["wait"] == _RUN_WAIT_DEFAULT_S == _DEFAULT_S


def test_wait_ceiling_reaches_the_waiter() -> None:
    """A named ceiling is honoured, not merely accepted.

    The fleet allows a run 7200s; a ``--wait`` pinned to an hour would return
    while its agents were still working, and the caller would bundle
    half-finished work.
    """
    from bernstein.cli.run_bootstrap import run

    assert run.make_context("run", ["--wait", "7200"]).params["wait"] == 7200.0
    assert _finalize(quiet=False, wait=7200.0, supports_textual=False, is_tty=False)["timeout_s"] == 7200.0
    assert _finalize(quiet=False, wait=None, supports_textual=False, is_tty=False)["timeout_s"] is None
    assert _finalize(quiet=True, wait=None, supports_textual=False, is_tty=False)["timeout_s"] == _DEFAULT_S


def test_wait_rejects_a_value_that_is_not_a_number() -> None:
    """``--wait soon`` must fail loudly, not silently fall back to the default."""
    import click

    from bernstein.cli.run_bootstrap import run

    with pytest.raises(click.BadParameter):
        run.make_context("run", ["--wait", "soon"])
