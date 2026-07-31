"""``_refresh_heartbeat_from_signals`` must find the agent log in every layout.

The reap tick refreshes ``session.heartbeat_ts`` from three signals: a live
PID, the heartbeat protocol JSON, and - as a bounded last resort - the agent
log / worktree ``.git`` mtime. The last resort probed one hardcoded path,
``<workdir>/.sdd/worktrees/<id>/.sdd/runtime/<id>.log``, which is only one of
the four layouts this codebase actually writes agent logs into:

* remote runtime bridge: ``session.log_path`` -> ``<spawn_cwd>/.sdd/logs/<id>.log``
  (``spawner_core._spawn_via_runtime_bridge``; also the container and
  sandbox-session spawn paths)
* current default worktrees: ``.sdd/runtime/worktrees/<id>/.sdd/runtime/<id>.log``
* legacy worktrees: ``.sdd/worktrees/<id>/.sdd/runtime/<id>.log``
* worktrees disabled: ``<workdir>/.sdd/runtime/<id>.log``

A session outside the legacy layout therefore had no log signal at all. That
is worst for a bridge-backed session, which additionally has ``pid=None`` (no
local process to probe) and gets no heartbeat JSON: ``bernstein.bridges``
never writes one, and the single pre-spawn touch in ``spawner_core`` is never
refreshed, so the remote run aged out at ``heartbeat_timeout_s`` no matter how
healthy it was. ``_resolve_agent_log_path`` / ``_resolve_agent_worktree_dir``
already resolve all four layouts for the sibling probe
``_probe_liveness_signals``; this pins that the reap tick uses them too.

The log stays a *weak* signal in every layout - the issue #3058 cap still
applies, see ``test_heartbeat_log_only_cap.py``.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bernstein.core.models import AgentSession

from bernstein.core.agents.agent_lifecycle import (
    _MAX_LOG_ONLY_HEARTBEAT_TICKS,
    _refresh_heartbeat_from_signals,
)


def _make_orch(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(_workdir=tmp_path)


def _touch(path: Path, mtime: float) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("line\n")
    os.utime(path, (mtime, mtime))


@patch("bernstein.core.agents.agent_lifecycle._is_process_alive", return_value=False)
def test_bridge_log_path_refreshes_heartbeat(_mock_alive, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """A bridge-backed session is judged from ``session.log_path``.

    Reproduces the real spawn state: ``_spawn_via_runtime_bridge`` sets
    ``pid=None`` and points ``log_path`` at ``<spawn_cwd>/.sdd/logs/<id>.log``,
    a directory the hardcoded probe never looked in. With no PID and no
    heartbeat JSON, that log is the session's only liveness signal.
    """
    orch = _make_orch(tmp_path)
    session = AgentSession(id="sess-bridge", role="backend", pid=None)
    session.runtime_backend = "openclaw"
    bridge_log = tmp_path / ".sdd" / "worktrees" / session.id / ".sdd" / "logs" / f"{session.id}.log"
    session.log_path = str(bridge_log)

    now = time.time() + 1
    _touch(bridge_log, now)
    _refresh_heartbeat_from_signals(orch, session, now)

    assert session.heartbeat_ts == now, "a fresh session.log_path must refresh the heartbeat"
    assert session.log_only_heartbeat_ticks == 1, "the log is still only a weak, capped signal"


@patch("bernstein.core.agents.agent_lifecycle._is_process_alive", return_value=False)
def test_bridge_log_path_signal_is_still_capped(_mock_alive, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """Resolving the bridge log must not hand it an uncapped refresh.

    ``session.log_path`` is the same stderr-tainted stream issue #3058 capped
    for the legacy layout, so a bridge session that only ever moves its log
    mtime must still age out rather than ride it to the hard cap.
    """
    orch = _make_orch(tmp_path)
    session = AgentSession(id="sess-bridge-cap", role="backend", pid=None)
    bridge_log = tmp_path / ".sdd" / "worktrees" / session.id / ".sdd" / "logs" / f"{session.id}.log"
    session.log_path = str(bridge_log)

    for tick in range(_MAX_LOG_ONLY_HEARTBEAT_TICKS):
        now = time.time() + tick + 1
        _touch(bridge_log, now)
        _refresh_heartbeat_from_signals(orch, session, now)
        assert session.heartbeat_ts == now

    capped_ts = session.heartbeat_ts
    now = time.time() + _MAX_LOG_ONLY_HEARTBEAT_TICKS + 1
    _touch(bridge_log, now)
    _refresh_heartbeat_from_signals(orch, session, now)
    assert session.heartbeat_ts == capped_ts, "capped tick must not refresh from the bridge log either"


@patch("bernstein.core.agents.agent_lifecycle._is_process_alive", return_value=False)
def test_current_default_worktree_layout_refreshes_heartbeat(_mock_alive, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """``.sdd/runtime/worktrees/<id>/`` is the current default worktree layout."""
    orch = _make_orch(tmp_path)
    session = AgentSession(id="sess-current", role="backend", pid=123)
    log_path = tmp_path / ".sdd" / "runtime" / "worktrees" / session.id / ".sdd" / "runtime" / f"{session.id}.log"

    now = time.time() + 1
    _touch(log_path, now)
    _refresh_heartbeat_from_signals(orch, session, now)

    assert session.heartbeat_ts == now


@patch("bernstein.core.agents.agent_lifecycle._is_process_alive", return_value=False)
def test_legacy_worktree_layout_still_refreshes_heartbeat(_mock_alive, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """The one layout the hardcoded probe did cover must keep working."""
    orch = _make_orch(tmp_path)
    session = AgentSession(id="sess-legacy", role="backend", pid=123)
    log_path = tmp_path / ".sdd" / "worktrees" / session.id / ".sdd" / "runtime" / f"{session.id}.log"

    now = time.time() + 1
    _touch(log_path, now)
    _refresh_heartbeat_from_signals(orch, session, now)

    assert session.heartbeat_ts == now


@patch("bernstein.core.agents.agent_lifecycle._is_process_alive", return_value=False)
def test_worktrees_disabled_root_log_refreshes_heartbeat(_mock_alive, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """With worktrees disabled the agent log sits at the workdir root."""
    orch = _make_orch(tmp_path)
    session = AgentSession(id="sess-noworktree", role="backend", pid=123)
    log_path = tmp_path / ".sdd" / "runtime" / f"{session.id}.log"

    now = time.time() + 1
    _touch(log_path, now)
    _refresh_heartbeat_from_signals(orch, session, now)

    assert session.heartbeat_ts == now


@patch("bernstein.core.agents.agent_lifecycle._is_process_alive", return_value=False)
def test_git_signal_resolves_current_default_worktree_layout(_mock_alive, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """The ``.git`` fallback resolves the same layouts as the log probe."""
    orch = _make_orch(tmp_path)
    session = AgentSession(id="sess-git", role="backend", pid=123)
    git_path = tmp_path / ".sdd" / "runtime" / "worktrees" / session.id / ".git"

    now = time.time() + 1
    _touch(git_path, now)
    _refresh_heartbeat_from_signals(orch, session, now)

    assert session.heartbeat_ts == now


@patch("bernstein.core.agents.agent_lifecycle._is_process_alive", return_value=False)
def test_root_repo_git_is_not_a_liveness_signal(_mock_alive, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    """A session with no per-agent worktree gets no git signal at all.

    The root ``<workdir>/.git`` is shared mutable state - the orchestrator's
    own git operations and every sibling agent touch it - so its freshness
    cannot be attributed to this session. Mirrors the rationale already
    documented in ``_probe_liveness_signals``.
    """
    orch = _make_orch(tmp_path)
    session = AgentSession(id="sess-rootgit", role="backend", pid=123)

    now = time.time() + 1
    _touch(tmp_path / ".git", now)
    _refresh_heartbeat_from_signals(orch, session, now)

    assert session.heartbeat_ts == 0.0, "root repo .git must never stand in for a worktree signal"
