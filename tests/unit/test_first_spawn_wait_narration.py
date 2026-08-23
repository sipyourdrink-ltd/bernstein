"""Regression tests for issue #4257: the first-agent wait must not be silent.

On a TTY, ``bernstein run`` prints nothing between bootstrap and the
dashboard opening. When the first agent takes a couple of seconds to register,
the terminal just sits there, and the operator cannot tell whether the run is
starting, hung, or broken. The wait itself is correctly bounded -- it returns
as soon as ``agent_count > 0`` -- so the fix is to narrate it, not remove it.

``_await_first_spawn_outcome`` polls ``/health`` every ``_FIRST_SPAWN_POLL_S``.
The narration is opt-in (``narrate_wait=True``): the first poll runs before any
status is shown, so a fast start stays silent; only when a poll fails to
produce a verdict does a transient Rich status appear and clear on exit.

These tests assert on captured output only -- never on timing.
"""

# pyright: reportPrivateUsage=false

from __future__ import annotations

import io
from typing import Any
from unittest.mock import patch

from rich.console import Console

from bernstein.cli.run_bootstrap import _await_first_spawn_outcome

_STATUS_TEXT = "Waiting for the first agent to register"


def _captured_console() -> tuple[Console, io.StringIO]:
    """A terminal-forced console writing to an in-memory buffer."""
    buffer = io.StringIO()
    return Console(file=buffer, force_terminal=True, color_system=None, width=80, height=24), buffer


def test_fast_start_prints_nothing() -> None:
    """When the first poll already reports an agent, no status is shown."""
    console, buffer = _captured_console()

    def _server_get(path: str) -> Any:
        if path.startswith("/health"):
            return {"agent_count": 1}
        return None

    with (
        patch("bernstein.cli.run_bootstrap.server_get", side_effect=_server_get),
        patch("bernstein.cli.run_bootstrap.console", console),
    ):
        outcome, reason = _await_first_spawn_outcome(narrate_wait=True, timeout_s=1.0, poll_interval_s=0.01)

    assert outcome == "spawned"
    assert reason is None
    assert _STATUS_TEXT not in buffer.getvalue()


def test_slow_start_shows_waiting_status() -> None:
    """When a later poll reports the agent, the wait is narrated and cleared."""
    console, buffer = _captured_console()
    healths = iter([{"agent_count": 0}, {"agent_count": 0}, {"agent_count": 1}])

    def _server_get(path: str) -> Any:
        if path.startswith("/health"):
            return next(healths)
        return None

    with (
        patch("bernstein.cli.run_bootstrap.server_get", side_effect=_server_get),
        patch("bernstein.cli.run_bootstrap.console", console),
    ):
        outcome, reason = _await_first_spawn_outcome(narrate_wait=True, timeout_s=1.0, poll_interval_s=0.01)

    assert outcome == "spawned"
    assert reason is None
    assert _STATUS_TEXT in buffer.getvalue()


def test_narration_disabled_stays_silent() -> None:
    """The non-interactive and ``--quiet`` paths (``narrate_wait=False``) stay quiet."""
    console, buffer = _captured_console()
    healths = iter([{"agent_count": 0}, {"agent_count": 0}, {"agent_count": 1}])

    def _server_get(path: str) -> Any:
        if path.startswith("/health"):
            return next(healths)
        return None

    with (
        patch("bernstein.cli.run_bootstrap.server_get", side_effect=_server_get),
        patch("bernstein.cli.run_bootstrap.console", console),
    ):
        outcome, _reason = _await_first_spawn_outcome(narrate_wait=False, timeout_s=1.0, poll_interval_s=0.01)

    assert outcome == "spawned"
    assert _STATUS_TEXT not in buffer.getvalue()
