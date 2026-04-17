"""Orchestrator cleanup: stop, drain, save state, restart.

Extracted from orchestrator.py as part of ORCH-009 decomposition.
Merges content from the earlier orchestrator_lifecycle.py extraction.
Functions here operate on an Orchestrator instance passed as the first
argument, keeping the Orchestrator class as the public facade.
"""

from __future__ import annotations

import contextlib
import logging
import os
import time
from typing import Any, cast

from bernstein.core.agent_recycling import (
    send_shutdown_signals,
)
from bernstein.core.orchestration.tick_pipeline import (
    fetch_all_tasks,
)
from bernstein.core.task_lifecycle import (
    process_completed_tasks,
)

logger = logging.getLogger(__name__)


def stop(orch: Any) -> None:
    """Signal the run loop to exit after the current tick.

    Also writes SHUTDOWN signal files to all active agents so they can
    save WIP and exit cleanly before the orchestrator terminates.

    Args:
        orch: The orchestrator instance.
    """
    orch._shutting_down.set()
    orch._running = False
    with contextlib.suppress(Exception):
        send_shutdown_signals(orch, reason="orchestrator_stopped")


def is_shutting_down(orch: Any) -> bool:
    """Return True when the orchestrator is draining for shutdown.

    Args:
        orch: The orchestrator instance.

    Returns:
        True if shutdown is in progress.
    """
    return orch._shutting_down.is_set()


def drain_before_cleanup(orch: Any, timeout_s: float | None = None) -> None:
    """Stop new work, send SHUTDOWN signals, reap completed agents, then drain executor.

    Sends SHUTDOWN signals to all active agents at the start of drain so
    they can save WIP and exit cleanly.  During the wait loop, continues
    reaping dead agents and processing completed tasks so that work
    finished during drain is not lost.

    Args:
        orch: The orchestrator instance.
        timeout_s: Maximum seconds to wait for agents to finish.  Defaults
            to ``orch._config.drain_timeout_s`` (60 s).
    """
    if orch._executor_drained:
        return

    if timeout_s is None:
        timeout_s = orch._config.drain_timeout_s

    # BUG-06: Send SHUTDOWN signals so agents save WIP and exit cleanly
    with contextlib.suppress(Exception):
        send_shutdown_signals(orch, reason="drain_before_cleanup")

    from bernstein.core.agent_lifecycle import reap_dead_agents
    from bernstein.core.orchestration.orchestrator import TickResult

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        active_sessions = [
            session
            for session in orch._agents.values()
            if session.status != "dead" and orch._spawner.check_alive(session)
        ]
        if not active_sessions:
            break

        # BUG-20: Reap dead agents and process completed tasks each
        # iteration so work that finishes during drain is merged.
        try:
            tasks_by_status = fetch_all_tasks(orch._client, orch._config.server_url)
            done_tasks = tasks_by_status.get("done", [])
            if done_tasks:
                process_completed_tasks(orch, done_tasks, TickResult())
            reap_dead_agents(orch, TickResult(), tasks_by_status)
        except Exception:
            logger.debug("Drain poll: task fetch/reap failed (non-critical)", exc_info=True)

        time.sleep(1.0)

    try:
        orch._executor.shutdown(wait=True, cancel_futures=True)
    except TypeError:
        orch._executor.shutdown(wait=True)
    orch._executor_drained = True
    logger.info("Executor drained before cleanup")


def save_session_state(orch: Any) -> None:
    """Persist session state for fast resume on next start.

    Queries the task server for current task statuses and writes a
    session snapshot to ``.sdd/runtime/session.json``.  Errors are
    silently caught -- session saving is best-effort.

    Args:
        orch: The orchestrator instance.
    """
    try:
        from bernstein.core.session import SessionState, save_session

        resp = orch._client.get(f"{orch._config.server_url}/tasks")
        resp.raise_for_status()

        tasks_data: Any = resp.json()
        task_list: list[dict[str, Any]] = []
        if isinstance(tasks_data, list):
            task_list = cast("list[dict[str, Any]]", tasks_data)
        elif isinstance(tasks_data, dict):
            raw_dict = cast("dict[str, Any]", tasks_data)
            task_list = cast("list[dict[str, Any]]", raw_dict.get("tasks", []))

        done_ids: list[str] = [str(t["id"]) for t in task_list if t.get("status") == "done"]
        pending_ids: list[str] = [str(t["id"]) for t in task_list if t.get("status") in ("claimed", "in_progress")]

        state = SessionState(
            saved_at=time.time(),
            goal="",
            completed_task_ids=done_ids,
            pending_task_ids=pending_ids,
            cost_spent=orch._cost_tracker.spent_usd,
        )
        save_session(orch._workdir, state)
        logger.info("Session state saved (%d done, %d pending)", len(done_ids), len(pending_ids))
    except Exception:
        logger.debug("Failed to save session state (best-effort)", exc_info=True)


def cleanup(orch: Any) -> None:
    """Release resources held by the orchestrator.

    Args:
        orch: The orchestrator instance.
    """
    # Save session state before releasing resources
    save_session_state(orch)

    # SOC 2: generate Merkle seal on shutdown when audit mode is active
    if orch._audit_mode and orch._audit_log is not None:
        try:
            from bernstein.core.merkle import compute_seal, save_seal

            audit_dir = orch._workdir / ".sdd" / "audit"
            merkle_dir = audit_dir / "merkle"
            _tree, seal = compute_seal(audit_dir)
            seal_path = save_seal(seal, merkle_dir)
            logger.info("Merkle audit seal written: %s (root=%s)", seal_path, seal["root_hash"])
        except Exception:
            logger.warning("Merkle seal generation on shutdown failed", exc_info=True)

    # Full git hygiene on shutdown. Never force-delete unmerged branches —
    # agents shutting down may have unpushed work we need to preserve.
    try:
        from bernstein.core.git_hygiene import run_hygiene

        active_ids: set[str] = set()
        try:
            active_ids = {s.id for s in orch._agents.values() if s.status != "dead"}
        except Exception:
            # Best-effort: if we cannot enumerate live agents, fall back to
            # an empty set. The merge-ancestry guard still prevents data loss.
            active_ids = set()
        run_hygiene(orch._workdir, full=True, active_session_ids=active_ids)
    except Exception:
        logger.debug("Git hygiene on shutdown failed (non-critical)", exc_info=True)

    # Stop cluster heartbeat client (unregisters from central server)
    if orch._heartbeat_client is not None:
        orch._heartbeat_client.stop()
        logger.info("Cluster heartbeat client stopped")

    # Cancel pending futures first
    for future in (orch._pending_ruff_future, orch._pending_test_future):
        if future is not None and not future.done():
            future.cancel()
    orch._pending_ruff_future = None
    orch._pending_test_future = None

    # Shut down the thread pool
    if not orch._executor_drained:
        try:
            orch._executor.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            # Python <3.9 doesn't have cancel_futures
            orch._executor.shutdown(wait=False)
    logger.info("Executor shut down, background test/ruff processes released")


def restart(orch: Any) -> None:
    """Replace the current process with a fresh orchestrator.

    BUG-22 fix: sends SHUTDOWN signals and drains active agents before
    calling ``os.execv`` so that running agents are not orphaned.

    Args:
        orch: The orchestrator instance.
    """
    import sys

    logger.info("Stopping active agents before restart")
    stop(orch)
    drain_before_cleanup(orch)
    cleanup(orch)
    logger.info("Exec'ing fresh orchestrator process")
    os.execv(sys.executable, [sys.executable, *sys.argv])
