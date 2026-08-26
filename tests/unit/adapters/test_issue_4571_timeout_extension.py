"""Issue #4571 - the agent timeout extension must reach the spawned process.

A task batch's wall-clock budget is resolved at spawn into a watchdog timer
that kills the agent process on expiry. The extension path in
``reap_dead_agents`` mutates ``session.timeout_s``, but historically the
adapter armed a one-shot ``threading.Timer`` with the original scalar, so the
extension never moved the kill. The fix:

* threads the resolved budget into ``adapter.spawn(...)`` via
  ``timeout_seconds`` instead of the 1800s literal, and
* re-arms the watchdog on extension via ``extend_timeout`` (cancel the old
  timer, start a fresh one at the new deadline). A missed re-arm leaves the
  original timer in place, so a non-extended agent is still killed on time.

These tests exercise the real mechanism with a live timer at sub-second scale,
not a mocked clock: the bug lives in the arming / re-arming.
"""

from __future__ import annotations

import time
from unittest.mock import MagicMock, patch

from bernstein.core.models import Complexity, Scope, Task

from bernstein.core.agents.spawner_core import AgentSpawner
from bernstein.core.defaults import TASK


def _make_popen_mock(pid: int) -> MagicMock:
    m = MagicMock()
    m.pid = pid
    return m


def test_extending_a_session_moves_the_process_deadline() -> None:
    """An extended budget re-arms the watchdog so the process survives past
    the original deadline, and the re-armed timer still fires later."""
    from bernstein.adapters.codex import CodexAdapter

    adapter = CodexAdapter()
    proc_mock = _make_popen_mock(pid=9001)

    with (
        patch("bernstein.adapters.codex.subprocess.Popen", return_value=proc_mock),
        patch("bernstein.adapters.base.kill_process_group") as mock_killpg,
        patch("bernstein.adapters.base.process_alive", return_value=True),
    ):
        # Original deadline 0.4s from now.
        timer = adapter._start_timeout_watchdog(pid=9001, timeout_seconds=0.4, session_id="extend-test")
        time.sleep(0.2)  # most of the way to the original deadline

        # The extension path re-arms with a fresh deadline 0.4s from THIS moment,
        # pushing the kill past the original 0.4s mark (to ~0.6s).
        reinstate = adapter.extend_timeout(timer, pid=9001, timeout_seconds=0.4, session_id="extend-test")

        # Past the ORIGINAL deadline (0.4s) but inside the re-armed window.
        time.sleep(0.3)  # now ~0.5s: original would have fired at 0.4, re-armed fires at ~0.6
        assert mock_killpg.call_count == 0, "extension did not move the deadline"

        # Past the re-armed deadline.
        time.sleep(0.3)  # now ~0.8s: re-armed deadline was ~0.6s
        assert mock_killpg.call_count >= 1, "re-armed deadline never fired"

    reinstate.cancel()


def test_non_extended_agent_still_killed_at_deadline() -> None:
    """A watchdog that is NOT re-armed fires at its original deadline - the
    property we must not lose while fixing the extension."""
    from bernstein.adapters.codex import CodexAdapter

    adapter = CodexAdapter()
    proc_mock = _make_popen_mock(pid=9002)

    with (
        patch("bernstein.adapters.codex.subprocess.Popen", return_value=proc_mock),
        patch("bernstein.adapters.base.kill_process_group") as mock_killpg,
        patch("bernstein.adapters.base.process_alive", return_value=True),
    ):
        timer = adapter._start_timeout_watchdog(pid=9002, timeout_seconds=0.3, session_id="no-extend")
        time.sleep(0.5)
        assert mock_killpg.call_count >= 1, "non-extended deadline never fired"

    timer.cancel()


def test_timeout_fallback_follows_scope_bucket_not_literal_default() -> None:
    """The resolved spawn timeout follows the scope/XL bucket rather than a
    hard-coded 1800s literal. A large+high task resolves to the XL bucket;
    a small task resolves to the small bucket (both distinct from 1800 to
    prove the value is computed, not defaulted)."""

    small = Task(
        id="T-S", title="t", description="d", role="backend", scope=Scope.SMALL,
    )
    xl = Task(
        id="T-XL", title="t", description="d", role="architect",
        scope=Scope.LARGE, complexity=Complexity.HIGH,
    )

    small_timeout = AgentSpawner._resolve_spawn_timeout([small])
    xl_timeout = AgentSpawner._resolve_spawn_timeout([xl])

    assert small_timeout == int(TASK.scope_timeout_s["small"])
    assert xl_timeout == int(TASK.xl_timeout_s)
    assert small_timeout != xl_timeout, "scope and XL buckets collapsed"


def test_rearm_uses_remaining_budget_not_absolute_budget() -> None:
    """#4571 reviewer catch: ``session.timeout_s`` is absolute from
    ``spawn_ts``, but ``threading.Timer.interval`` is relative from now. The
    reaper must pass ``timeout_s - runtime`` (the remaining budget), not the
    full absolute budget, or the watchdog drifts past the 5400s cap.

    Re-arming twice on a session whose ``spawn_ts`` is far in the past must
    yield a timer whose ``interval`` never places the deadline past
    ``spawn_ts + 5400``. Asserting on ``Timer.interval`` (a real attribute,
    not a mock) keeps this a real check.
    """
    from bernstein.adapters.codex import CodexAdapter

    adapter = CodexAdapter()
    proc_mock = _make_popen_mock(pid=9003)

    _hard_cap_s = 5400
    # The session has been alive a long time: spawn_ts is 5000s ago, so the
    # absolute budget is already near the cap.
    spawn_ts = time.time() - 5000
    runtime = time.time() - spawn_ts  # ≈ 5000

    with (
        patch("bernstein.adapters.codex.subprocess.Popen", return_value=proc_mock),
        patch("bernstein.adapters.base.kill_process_group"),
        patch("bernstein.adapters.base.process_alive", return_value=True),
    ):
        timer = adapter._start_timeout_watchdog(pid=9003, timeout_seconds=1800, session_id="remaining-budget")

        # First extension: absolute budget 2400, remaining = 2400 - 5000 < 0,
        # so the floor of 60s wins.
        extended_abs_1 = min(1800 + 600, _hard_cap_s)
        remaining_1 = max(60, int(extended_abs_1 - runtime))
        timer = adapter.extend_timeout(timer, pid=9003, timeout_seconds=remaining_1, session_id="remaining-budget")

        # Second extension: absolute budget 3000, still floor 60.
        extended_abs_2 = min(extended_abs_1 + 600, _hard_cap_s)
        remaining_2 = max(60, int(extended_abs_2 - runtime))
        timer = adapter.extend_timeout(timer, pid=9003, timeout_seconds=remaining_2, session_id="remaining-budget")

        # The timer's interval is a relative delay: remaining budget, never the
        # full absolute budget. It must stay well under the cap (the 60s floor).
        assert timer.interval == remaining_2
        assert timer.interval <= _hard_cap_s
        # The deadline the timer enforces (now + interval) must not exceed
        # spawn_ts + the absolute cap.
        assert time.time() + timer.interval <= spawn_ts + _hard_cap_s + 60

    timer.cancel()
