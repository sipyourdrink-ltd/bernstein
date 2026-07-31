"""Regression: a session with no OS PID must be judged from its file signals,
not crash the reap pass.

``AgentSession.pid`` is ``int | None``, and the remote-runtime-bridge spawn
path in ``spawner_core`` sets it to ``None`` before transitioning the session
to "working" - there is no local process to point at. ``reap_dead_agents``
then fed that ``None`` straight into ``_is_process_alive`` ->
``platform_compat.process_alive``, whose ``if pid <= 0`` guard raises
``TypeError: '<=' not supported between instances of 'NoneType' and 'int'``.

The orchestrator tick calls ``reap_dead_agents`` outside any ``try/except``,
so the exception escaped the whole tick rather than being contained to one
session. ``_probe_liveness_signals`` already guards the same call with
``bool(pid) and _is_process_alive(pid)``; this pins the same contract on the
reap path.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from bernstein.core.models import AgentSession

from bernstein.core.agents.agent_lifecycle import (
    _refresh_heartbeat_from_signals,
    reap_dead_agents,
)
from bernstein.core.config.platform_compat import process_alive


def _make_orch(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        _agents={},
        _config=SimpleNamespace(max_agent_runtime_s=1800, heartbeat_timeout_s=120),
        _workdir=tmp_path,
    )


def _bridge_session(session_id: str = "sess-bridge") -> AgentSession:
    """A session as the remote runtime bridge leaves it: working, but pid=None."""
    session = AgentSession(id=session_id, role="backend", task_ids=["T-1"], status="working")
    session.pid = None
    session.runtime_backend = "openclaw"
    session.spawn_ts = time.time()
    session.heartbeat_ts = time.time()
    session.timeout_s = 1800
    return session


def _touch(path: Path, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{}\n")
    os.utime(path, (mtime, mtime))


def test_reap_does_not_raise_for_none_pid_session(tmp_path: Path) -> None:
    """A bridge-spawned session (pid=None) must survive a whole reap pass.

    Pre-fix this raised TypeError out of ``reap_dead_agents`` and, because the
    tick's call site is unguarded, out of the tick itself.
    """
    orch = _make_orch(tmp_path)
    session = _bridge_session()
    orch._agents[session.id] = session

    reap_dead_agents(orch, SimpleNamespace(reaped=[]), {})


def test_none_pid_session_is_judged_from_heartbeat_file(tmp_path: Path) -> None:
    """With no PID to probe, a fresh heartbeat JSON is what keeps the session alive."""
    orch = _make_orch(tmp_path)
    session = _bridge_session("sess-json")
    session.heartbeat_ts = time.time() - 300  # stale enough to matter

    now = time.time()
    _touch(tmp_path / ".sdd" / "runtime" / "heartbeats" / f"{session.id}.json", now)

    _refresh_heartbeat_from_signals(orch, session, now)

    assert session.heartbeat_ts == now, "fresh heartbeat JSON must refresh a pid-less session"
    assert session.log_only_heartbeat_ticks == 0


def test_none_pid_session_with_no_signals_is_left_to_age_out(tmp_path: Path) -> None:
    """A pid-less session with no file signals must NOT be treated as alive.

    The guard has to be "no PID -> fall through to the file signals", not
    "no PID -> skip the whole liveness check and refresh anyway".
    """
    orch = _make_orch(tmp_path)
    session = _bridge_session("sess-silent")
    stale_ts = time.time() - 300
    session.heartbeat_ts = stale_ts

    _refresh_heartbeat_from_signals(orch, session, time.time())

    assert session.heartbeat_ts == stale_ts, "a silent pid-less session must not be refreshed"
    assert session.log_only_heartbeat_ticks == 0


def test_none_pid_session_reaped_on_stale_heartbeat(tmp_path: Path) -> None:
    """The pid-less session still reaches the heartbeat-timeout reap.

    Guarding the liveness probe must not accidentally make bridge sessions
    immortal: with no signals and a heartbeat older than the timeout, the
    session is reaped exactly like a local one.
    """
    killed: list[str] = []
    orch = _make_orch(tmp_path)
    orch._spawner = SimpleNamespace(kill=lambda s: killed.append(s.id))
    orch._evolution = None
    orch._record_provider_health = lambda session, success: None
    orch._signal_mgr = SimpleNamespace(clear_signals=lambda _sid: None)
    orch._file_ownership = {}
    orch._task_to_session = {}

    session = _bridge_session("sess-stale")
    session.task_ids = []  # no server round-trip needed for the retry/fail path
    session.heartbeat_ts = time.time() - 300  # > heartbeat_timeout_s of 120
    orch._agents[session.id] = session

    result: Any = SimpleNamespace(reaped=[])
    reap_dead_agents(orch, result, {})

    assert result.reaped == [session.id]
    assert killed == [session.id]


def test_process_alive_rejects_none_pid() -> None:
    """Hardening: the primitive itself answers False for a falsy pid.

    ``process_alive`` already returns False for 0 and negatives; None is the
    same "there is no process here" answer and must not raise on the way to it.
    """
    assert process_alive(None) is False  # type: ignore[arg-type]
    assert process_alive(0) is False
    assert process_alive(-1) is False
