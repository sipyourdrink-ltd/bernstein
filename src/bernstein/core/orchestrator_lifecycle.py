"""Orchestrator lifecycle helpers: startup, shutdown, drain, restart.

Extracted from orchestrator.py (ORCH-009) to reduce file size.
Functions here operate on an Orchestrator instance passed as the first
argument, keeping the Orchestrator class as the public facade.
"""

from __future__ import annotations

import logging
import time
from typing import Any, cast

logger = logging.getLogger(__name__)


def drain_before_cleanup(orch: Any, timeout_s: float = 10.0) -> None:
    """Stop new work, wait briefly for active agents, then drain executor.

    Args:
        orch: The orchestrator instance.
        timeout_s: Maximum seconds to wait for agents to finish.
    """
    if orch._executor_drained:
        return

    deadline = time.time() + timeout_s
    while time.time() < deadline:
        active_sessions = [
            session
            for session in orch._agents.values()
            if session.status != "dead" and orch._spawner.check_alive(session)
        ]
        if not active_sessions:
            break
        time.sleep(0.2)

    try:
        orch._executor.shutdown(wait=True, cancel_futures=True)
    except TypeError:
        orch._executor.shutdown(wait=True)
    orch._executor_drained = True
    logger.info("Executor drained before cleanup")


def save_session_state(orch: Any) -> None:
    """Persist session state for fast resume on next start.

    Queries the task server for current task statuses and writes a
    session snapshot to ``.sdd/runtime/session.json``.

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
            typed_data: dict[str, Any] = cast("dict[str, Any]", tasks_data)
            task_list = cast("list[dict[str, Any]]", typed_data.get("tasks", []))

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


def cleanup_orchestrator(orch: Any) -> None:
    """Release resources held by the orchestrator.

    Args:
        orch: The orchestrator instance.
    """
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

    # Full git hygiene on shutdown
    try:
        from bernstein.core.git_hygiene import run_hygiene

        run_hygiene(orch._workdir, full=True)
    except Exception:
        logger.debug("Git hygiene on shutdown failed (non-critical)", exc_info=True)

    # Stop cluster heartbeat client
    if orch._heartbeat_client is not None:
        orch._heartbeat_client.stop()
        logger.info("Cluster heartbeat client stopped")

    # Cancel pending futures
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
            orch._executor.shutdown(wait=False)
    logger.info("Executor shut down, background test/ruff processes released")


def reconcile_claimed_tasks(orch: Any) -> int:
    """Unclaim orphaned tasks from previous orchestrator runs.

    On startup the ``_task_to_session`` map is empty, so any task that
    the server still considers "claimed" is orphaned.

    Args:
        orch: The orchestrator instance.

    Returns:
        Number of tasks that were unclaimed.
    """
    try:
        resp = orch._client.get(f"{orch._config.server_url}/tasks?status=claimed")
        resp.raise_for_status()
        claimed: Any = resp.json()
    except Exception:
        return 0

    unclaimed = 0
    task_items: list[dict[str, Any]] = cast(
        "list[dict[str, Any]]",
        claimed if isinstance(claimed, list) else claimed.get("tasks", []),
    )
    for task_dict in task_items:
        task_id: str = str(task_dict.get("id", ""))
        if task_id not in orch._task_to_session:
            try:
                orch._client.post(f"{orch._config.server_url}/tasks/{task_id}/force-claim")
                unclaimed += 1
                logger.info("Unclaimed orphan task %s (%s)", task_id, str(task_dict.get("title", "")))
            except Exception:
                pass

    if unclaimed:
        logger.warning("Reconciled %d orphaned claimed tasks from previous run", unclaimed)
    return unclaimed
