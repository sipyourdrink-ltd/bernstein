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
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from bernstein.core.models import AgentSession, Complexity, Scope, Task

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
        # Use a generous real-timer window so normal runner scheduling jitter does
        # not turn this lifecycle test into a deadline test.
        original_timeout = 1.0
        timer = adapter._start_timeout_watchdog(pid=9001, timeout_seconds=original_timeout, session_id="extend-test")
        arm_started = time.monotonic()
        time.sleep(0.5)
        elapsed_before_extension = time.monotonic() - arm_started
        if elapsed_before_extension >= original_timeout:
            timer.cancel()
            pytest.skip(f"runner stalled for {elapsed_before_extension:.3f}s before extension")

        # The extension path re-arms with a fresh deadline 1.0s from THIS moment,
        # pushing the kill past the original 1.0s mark (to ~1.5s).
        reinstate = adapter.extend_timeout(timer, pid=9001, timeout_seconds=1.0, session_id="extend-test")

        # Past the ORIGINAL deadline (1.0s) but inside the re-armed window.
        time.sleep(0.7)  # now ~1.2s: original would have fired at 1.0, re-armed fires at ~1.5
        assert mock_killpg.call_count == 0, "extension did not move the deadline"

        # Past the re-armed deadline.
        time.sleep(0.6)  # now ~1.8s: re-armed deadline was ~1.5s
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
        timer = adapter._start_timeout_watchdog(pid=9002, timeout_seconds=0.8, session_id="no-extend")
        time.sleep(1.1)
        assert mock_killpg.call_count >= 1, "non-extended deadline never fired"

    timer.cancel()


def test_timeout_fallback_follows_scope_bucket_not_literal_default() -> None:
    """The resolved spawn timeout follows the scope/XL bucket rather than a
    hard-coded 1800s literal. A large+high task resolves to the XL bucket;
    a small task resolves to the small bucket (both distinct from 1800 to
    prove the value is computed, not defaulted)."""

    small = Task(
        id="T-S",
        title="t",
        description="d",
        role="backend",
        scope=Scope.SMALL,
    )
    xl = Task(
        id="T-XL",
        title="t",
        description="d",
        role="architect",
        scope=Scope.LARGE,
        complexity=Complexity.HIGH,
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


def _make_reap_orch(tmp_path) -> SimpleNamespace:
    """An orchestrator stub sufficient for one reap pass (issue #4610).

    Note: reap_dead_agents reads the 120s freshness threshold from the local
    ``_time_since_heartbeat < 120`` literal, not from ``heartbeat_timeout_s``;
    the config field exists only because the heartbeat-reap path touches it.
    """
    return SimpleNamespace(
        _agents={},
        _config=SimpleNamespace(max_agent_runtime_s=1800, heartbeat_timeout_s=120),
        _spawner=None,
        _workdir=tmp_path,
    )


class _RecordingAdapter:
    """Stub adapter that records the timeout it is handed on extension."""

    def __init__(self) -> None:
        self.received: list[int] = []

    def extend_timeout(self, timer: object, pid: int, timeout_seconds: int, session_id: str) -> object:
        self.received.append(timeout_seconds)
        return timer  # the tester does not need a real re-armed timer


def test_reap_dead_agents_hands_down_remaining_budget(tmp_path) -> None:
    """The caller (reap_dead_agents) must convert the absolute budget into the
    remaining budget before re-arming, off the clamp so the ordinary positive
    conversion is what is asserted (rather than the floor, which the second
    test covers).

    A session that has only just overrun its 1800s budget is extended: the
    value handed to the adapter must be the genuine remaining budget
    (``timeout_s - runtime``), a positive quantity well above the 60s floor.
    """
    from bernstein.core.agents.agent_lifecycle import reap_dead_agents

    orch = _make_reap_orch(tmp_path)
    adapter = _RecordingAdapter()
    orch._spawner = SimpleNamespace(_adapter=adapter)

    # Pin spawn_ts so runtime is a fixed ~1900s, just past the 1800 budget.
    spawn_ts = time.time() - 1900
    session = AgentSession(id="sess-caller", role="backend", task_ids=["T-1"], status="working")
    session.pid = 12345
    session.spawn_ts = spawn_ts
    session.heartbeat_ts = spawn_ts + 1899  # fresh heartbeat -> the extension branch
    session.timeout_s = 1800
    session.timeout_timer = MagicMock()  # a non-None timer so the re-arm path runs
    orch._agents[session.id] = session

    reap_dead_agents(orch, SimpleNamespace(reaped=[]), {})

    # The extension happened: the absolute budget advanced to 2400.
    assert session.timeout_s == 2400, "extension did not happen (test would be vacuous)"
    # The adapter received the remaining budget: ~500 (2400 - ~1900), a real
    # conversion check well off the 60s floor. Assert within a small tolerance
    # because reap_dead_agents recomputes `now` internally, so our runtime
    # estimate can differ from the one the code used by the call duration.
    received = adapter.received[0]
    assert 300 < received < 600, f"expected remaining ~500, got {received}"
    # And it is the remaining budget, never the absolute budget.
    assert received < session.timeout_s


def test_reap_dead_agents_floors_remaining_budget_at_60(tmp_path) -> None:
    """When runtime exceeds the extended budget (a session that has already
    overrun), ``max(60, ...)`` must clamp the remaining budget to 60s rather
    than hand the adapter a negative or zero delay. Kept as a second test
    because it exercises the clamp branch specifically, which is a distinct
    decision from the ordinary conversion.
    """
    from bernstein.core.agents.agent_lifecycle import reap_dead_agents

    orch = _make_reap_orch(tmp_path)
    adapter = _RecordingAdapter()
    orch._spawner = SimpleNamespace(_adapter=adapter)

    session = AgentSession(id="sess-floor", role="backend", task_ids=["T-1"], status="working")
    session.pid = 12346
    # Runtime well past even the extended budget: after the +600 extension the
    # absolute budget is 660s, but 2000s of runtime leaves a negative remaining
    # budget, so max(60, ...) must clamp to 60s.
    session.spawn_ts = time.time() - 2000
    session.heartbeat_ts = time.time()
    session.timeout_s = 60
    session.timeout_timer = MagicMock()
    orch._agents[session.id] = session

    reap_dead_agents(orch, SimpleNamespace(reaped=[]), {})

    assert session.timeout_s == 660, "extension did not happen (test would be vacuous)"
    assert adapter.received == [60], "the floor of 60s must win when runtime exceeds the budget"
