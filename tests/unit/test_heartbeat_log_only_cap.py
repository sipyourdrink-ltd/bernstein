"""Regression for issue #3058: stderr merged into the agent log must not
sustain the reap heartbeat indefinitely via _refresh_heartbeat_from_signals.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bernstein.core.models import AgentSession

from bernstein.core.agents import agent_lifecycle
from bernstein.core.agents.agent_lifecycle import (
    _MAX_LOG_ONLY_HEARTBEAT_TICKS,
    _refresh_heartbeat_from_signals,
)
from bernstein.core.defaults import ORCHESTRATOR


def _make_orch(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(_workdir=tmp_path)


def _touch(path: Path, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("line\n") if not path.exists() else path.write_text(path.read_text() + "line\n")
    os.utime(path, (mtime, mtime))


@patch("bernstein.core.agents.agent_lifecycle._is_process_alive", return_value=False)
def test_log_only_heartbeat_capped_after_max_ticks(_mock_alive, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """A dead-PID session whose log keeps getting fresh stderr writes (e.g. a
    retry loop or spinner, per issue #3058) may only ride that signal for
    _MAX_LOG_ONLY_HEARTBEAT_TICKS consecutive ticks, not indefinitely."""
    orch = _make_orch(tmp_path)
    session = AgentSession(id="sess-log", role="backend", pid=123)
    log_path = tmp_path / ".sdd" / "worktrees" / session.id / ".sdd" / "runtime" / f"{session.id}.log"

    for tick in range(_MAX_LOG_ONLY_HEARTBEAT_TICKS):
        now = time.time() + tick + 1
        _touch(log_path, now)
        _refresh_heartbeat_from_signals(orch, session, now)
        assert session.heartbeat_ts == now, f"tick {tick} should still refresh from the log"
        assert session.log_only_heartbeat_ticks == tick + 1

    # One more tick, log still fresh: the cap is now reached, so no refresh.
    stale_heartbeat_ts = session.heartbeat_ts
    now = time.time() + _MAX_LOG_ONLY_HEARTBEAT_TICKS + 1
    _touch(log_path, now)
    _refresh_heartbeat_from_signals(orch, session, now)
    assert session.heartbeat_ts == stale_heartbeat_ts, "capped tick must not refresh heartbeat_ts"


@patch("bernstein.core.agents.agent_lifecycle._is_process_alive", return_value=False)
def test_heartbeat_json_signal_is_never_capped(_mock_alive, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """The deliberate heartbeat protocol JSON is real evidence of progress
    (unlike the stderr-tainted log) and must keep refreshing the heartbeat
    past however many ticks the log-only cap would allow."""
    orch = _make_orch(tmp_path)
    session = AgentSession(id="sess-json", role="backend", pid=123)
    heartbeat_json = tmp_path / ".sdd" / "runtime" / "heartbeats" / f"{session.id}.json"

    for tick in range(_MAX_LOG_ONLY_HEARTBEAT_TICKS + 5):
        now = time.time() + tick + 1
        _touch(heartbeat_json, now)
        _refresh_heartbeat_from_signals(orch, session, now)
        assert session.heartbeat_ts == now
        assert session.log_only_heartbeat_ticks == 0


@patch("bernstein.core.agents.agent_lifecycle._is_process_alive")
def test_live_pid_resets_log_only_streak(mock_alive, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """A confirmed-live PID is real evidence of progress: it must reset the
    log-only streak so a later stall gets the full grace budget again,
    rather than inheriting an already-exhausted counter."""
    orch = _make_orch(tmp_path)
    session = AgentSession(id="sess-mixed", role="backend", pid=123)
    log_path = tmp_path / ".sdd" / "worktrees" / session.id / ".sdd" / "runtime" / f"{session.id}.log"

    mock_alive.return_value = False
    for tick in range(_MAX_LOG_ONLY_HEARTBEAT_TICKS):
        now = time.time() + tick + 1
        _touch(log_path, now)
        _refresh_heartbeat_from_signals(orch, session, now)
    assert session.log_only_heartbeat_ticks == _MAX_LOG_ONLY_HEARTBEAT_TICKS

    # PID confirmed alive on the next tick: real signal, resets the streak.
    mock_alive.return_value = True
    now = time.time() + _MAX_LOG_ONLY_HEARTBEAT_TICKS + 1
    _refresh_heartbeat_from_signals(orch, session, now)
    assert session.heartbeat_ts == now
    assert session.log_only_heartbeat_ticks == 0

    # Back to log-only: it gets the full budget again, not an exhausted one.
    mock_alive.return_value = False
    now = time.time() + _MAX_LOG_ONLY_HEARTBEAT_TICKS + 2
    _touch(log_path, now)
    _refresh_heartbeat_from_signals(orch, session, now)
    assert session.heartbeat_ts == now
    assert session.log_only_heartbeat_ticks == 1


@patch("bernstein.core.agents.agent_lifecycle._is_process_alive", return_value=False)
def test_worktree_git_signal_is_capped_like_the_log(_mock_alive, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """The worktree ``.git`` mtime shares the log's budget.

    It is the second entry in the same capped probe list, and in a linked
    worktree ``.git`` is a pointer file whose mtime does not move on commit,
    so it is no stronger a progress signal than the log. Without this case the
    cap could be dropped from the ``.git`` half alone and the log-only tests
    would stay green.
    """
    orch = _make_orch(tmp_path)
    session = AgentSession(id="sess-git", role="backend", pid=123)
    git_path = tmp_path / ".sdd" / "worktrees" / session.id / ".git"

    for tick in range(_MAX_LOG_ONLY_HEARTBEAT_TICKS):
        now = time.time() + tick + 1
        _touch(git_path, now)
        _refresh_heartbeat_from_signals(orch, session, now)
        assert session.heartbeat_ts == now, f"tick {tick} should still refresh from the worktree .git mtime"
        assert session.log_only_heartbeat_ticks == tick + 1

    capped_heartbeat_ts = session.heartbeat_ts
    now = time.time() + _MAX_LOG_ONLY_HEARTBEAT_TICKS + 1
    _touch(git_path, now)
    _refresh_heartbeat_from_signals(orch, session, now)
    assert session.heartbeat_ts == capped_heartbeat_ts, "capped tick must not refresh from .git either"


# ---------------------------------------------------------------------------
# The operator-visible half of issue #3058, driven through the real
# ``reap_dead_agents`` decision loop rather than the refresh helper alone.
#
# The helper tests above pin the counter. They do not pin what the counter is
# for: the wall-clock reaper extends ``session.timeout_s`` by 600s on any tick
# where the heartbeat looks younger than 120s, so an unbounded log-only refresh
# walks that timeout all the way to the 5400s hard cap and the stalled agent
# holds its worker slot for the full cap window. A refactor that kept the
# counter but moved or dropped the refresh call would leave the tests above
# green and put that behaviour straight back.
# ---------------------------------------------------------------------------

_HARD_CAP_S = 5400.0  # agent_lifecycle.reap_dead_agents absolute ceiling
_TICK_S = float(ORCHESTRATOR.tick_interval_s)
_HEARTBEAT_TIMEOUT_S = 120.0
_START_TIMEOUT_S = 1800.0


def _reap_orch(tmp_path: Path, session: AgentSession) -> SimpleNamespace:
    return SimpleNamespace(
        _agents={session.id: session},
        _config=SimpleNamespace(
            max_agent_runtime_s=_START_TIMEOUT_S,
            heartbeat_timeout_s=_HEARTBEAT_TIMEOUT_S,
        ),
        _workdir=tmp_path,
    )


def _walk_reap_loop(
    orch: SimpleNamespace,
    session: AgentSession,
    log_path: Path | None,
    *,
    t0: float,
    limit_s: float,
) -> dict[str, float | str]:
    """Tick ``reap_dead_agents`` on a simulated clock until it reaps.

    ``log_path`` is touched on every tick when given, modelling the merged
    stdout/stderr chatter of issue #3058: the mtime moves, nothing progresses.
    Returns the reap verdict, or an empty dict if ``limit_s`` is reached first.
    """
    verdict: dict[str, float | str] = {}

    def _hb_reap(_orch: object, sess: AgentSession, _r: object, _s: object, _now: float, age: float) -> None:
        verdict.update(reason="heartbeat_timeout", heartbeat_age_s=age)
        sess.status = "dead"

    def _wc_reap(_orch: object, sess: AgentSession, _r: object, _s: object, runtime: float) -> None:
        verdict.update(reason="wall_clock_timeout", runtime_s=runtime)
        sess.status = "dead"

    sim = 0.0
    with (
        patch.object(agent_lifecycle, "_reap_heartbeat_timeout", _hb_reap),
        patch.object(agent_lifecycle, "_reap_wall_clock_timeout", _wc_reap),
    ):
        while sim <= limit_s:
            now = t0 + sim
            if log_path is not None:
                _touch(log_path, now)
            with patch.object(agent_lifecycle, "time", SimpleNamespace(time=lambda now=now: now)):
                agent_lifecycle.reap_dead_agents(orch, SimpleNamespace(reaped=[]), {})
            if verdict:
                verdict["at_s"] = sim
                return verdict
            sim += _TICK_S
    return verdict


@patch("bernstein.core.agents.agent_lifecycle._is_process_alive", return_value=False)
def test_chattering_log_no_longer_rides_the_reaper_to_the_hard_cap(_mock_alive, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """A stalled agent whose merged log keeps moving is reaped on the heartbeat
    path, not held to the 5400s wall-clock hard cap (issue #3058)."""
    t0 = time.time()
    session = AgentSession(id="sess-reap", role="backend", pid=123, task_ids=["T-1"])
    session.spawn_ts = t0
    session.heartbeat_ts = t0
    session.timeout_s = _START_TIMEOUT_S
    orch = _reap_orch(tmp_path, session)
    log_path = tmp_path / ".sdd" / "worktrees" / session.id / ".sdd" / "runtime" / f"{session.id}.log"

    verdict = _walk_reap_loop(orch, session, log_path, t0=t0, limit_s=_HARD_CAP_S + 2 * _TICK_S)

    assert verdict, "the agent was never reaped at all"
    assert verdict["reason"] == "heartbeat_timeout", (
        f"reaped by {verdict['reason']} at {verdict['at_s']}s: log mtime alone kept the heartbeat "
        "young enough for the wall-clock reaper to keep extending session.timeout_s"
    )
    # Budget: the log-only grace, then the heartbeat has to age out, then the
    # tick that observes it. Derived rather than hardcoded so a retune of either
    # constant moves the bound with it.
    budget_s = _MAX_LOG_ONLY_HEARTBEAT_TICKS * _TICK_S + _HEARTBEAT_TIMEOUT_S + _TICK_S
    assert verdict["at_s"] <= budget_s, f"reaped at {verdict['at_s']}s, past the {budget_s}s budget"
    assert session.timeout_s == _START_TIMEOUT_S, (
        f"session.timeout_s ratcheted to {session.timeout_s}s: the log-only refresh is still feeding "
        "the wall-clock extension in reap_dead_agents"
    )


@patch("bernstein.core.agents.agent_lifecycle._is_process_alive", return_value=False)
def test_fresh_log_still_defers_the_reap_inside_the_budget(_mock_alive, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Control for issue #3012: inside its budget a fresh log still pushes the
    heartbeat forward, so an agent that is quiet but writing is not reaped on a
    heartbeat that was already one second from the timeout."""
    t0 = time.time()
    session = AgentSession(id="sess-defer", role="backend", pid=123, task_ids=["T-1"])
    session.spawn_ts = t0
    session.heartbeat_ts = t0 - (_HEARTBEAT_TIMEOUT_S - 1.0)
    session.timeout_s = _START_TIMEOUT_S
    orch = _reap_orch(tmp_path, session)
    log_path = tmp_path / ".sdd" / "worktrees" / session.id / ".sdd" / "runtime" / f"{session.id}.log"

    # Stop one tick short of the cap: every tick in this window must defer.
    inside_budget_s = (_MAX_LOG_ONLY_HEARTBEAT_TICKS - 1) * _TICK_S
    verdict = _walk_reap_loop(orch, session, log_path, t0=t0, limit_s=inside_budget_s)

    assert not verdict, f"reaped at {verdict.get('at_s')}s despite a log written on the same tick"
    assert session.heartbeat_ts >= t0, "a fresh log inside the budget must still refresh heartbeat_ts"
    assert session.status != "dead"
