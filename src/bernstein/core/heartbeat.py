"""Agent heartbeat and stall detection."""

from __future__ import annotations

import contextlib
import logging
import time
from typing import Any

from bernstein.core.models import ProgressSnapshot

logger = logging.getLogger(__name__)


def check_stale_agents(orch: Any) -> None:
    """Write WAKEUP / SHUTDOWN signals for agents that stopped heartbeating.

    Thresholds:
    - 60s without a heartbeat  -> WAKEUP
    - 120s without a heartbeat -> SHUTDOWN
    - 180s without a heartbeat -> handled by wall-clock kill in reap_dead_agents

    Only fires when an agent has at least one heartbeat on record (agents
    that never wrote a heartbeat are assumed to not support the protocol).

    Args:
        orch: Orchestrator instance.
    """
    now = time.time()
    for session in orch._agents.values():
        if session.status == "dead":
            continue
        hb = orch._signal_mgr.read_heartbeat(session.id)
        if hb is None:
            continue  # Agent never wrote a heartbeat -- skip
        age = now - hb.timestamp
        task_title = ", ".join(session.task_ids) if session.task_ids else "unknown task"
        elapsed = now - session.spawn_ts
        if age >= 120:
            with contextlib.suppress(OSError):
                orch._signal_mgr.write_shutdown(session.id, reason="no_heartbeat_120s", task_title=task_title)
        elif age >= 60:
            with contextlib.suppress(OSError):
                orch._signal_mgr.write_wakeup(
                    session.id,
                    task_title=task_title,
                    elapsed_s=elapsed,
                    last_activity_ago_s=age,
                )


# ------------------------------------------------------------------
# Stall detection via progress snapshots
# ------------------------------------------------------------------


def check_stalled_tasks(orch: Any) -> None:
    """Detect agents making no progress via consecutive identical snapshots.

    Fetches the latest progress snapshot for each active agent's tasks.
    Compares against the last seen snapshot tracked in the orchestrator.
    Escalates through WAKEUP → SHUTDOWN → kill based on stall count.

    Thresholds (each snapshot = ~60s):
    - 3 identical consecutive snapshots → WAKEUP signal
    - 5 identical consecutive snapshots → SHUTDOWN signal
    - 7 identical consecutive snapshots → kill process

    Only fires when a task has at least one snapshot on record.

    Args:
        orch: Orchestrator instance.
    """
    base = orch._config.server_url
    for session in orch._agents.values():
        if session.status == "dead":
            continue
        for task_id in session.task_ids:
            try:
                resp = orch._client.get(f"{base}/tasks/{task_id}/snapshots")
                resp.raise_for_status()
                snapshots_data: list[dict[str, Any]] = resp.json()
            except Exception:
                continue  # Server unavailable or task not found — skip

            if not snapshots_data:
                continue  # No snapshots yet

            # Parse the latest snapshot
            latest_raw = snapshots_data[-1]
            latest = ProgressSnapshot(
                timestamp=float(latest_raw["timestamp"]),
                files_changed=int(latest_raw.get("files_changed", 0)),
                tests_passing=int(latest_raw.get("tests_passing", -1)),
                errors=int(latest_raw.get("errors", 0)),
                last_file=str(latest_raw.get("last_file", "")),
            )

            # Skip if we have already processed this snapshot (same timestamp)
            last_ts = orch._last_snapshot_ts.get(task_id, 0.0)
            if latest.timestamp <= last_ts:
                continue

            # Compare with previous snapshot to track stall count
            prev: ProgressSnapshot | None = orch._last_snapshot.get(task_id)
            orch._last_snapshot_ts[task_id] = latest.timestamp
            orch._last_snapshot[task_id] = latest

            if prev is not None and prev.is_same_progress(latest):
                orch._stall_counts[task_id] = orch._stall_counts.get(task_id, 0) + 1
            else:
                orch._stall_counts[task_id] = 0

            count = orch._stall_counts[task_id]
            elapsed = time.time() - session.spawn_ts

            if count >= 7:
                logger.warning(
                    "Stall-killing agent %s (task %s): %d identical snapshots",
                    session.id,
                    task_id,
                    count,
                )
                with contextlib.suppress(Exception):
                    orch._spawner.kill(session)
                # Reset to prevent repeated kill calls before process exits
                orch._stall_counts[task_id] = 0
            elif count >= 5:
                logger.warning(
                    "Stall-shutdown agent %s (task %s): %d identical snapshots",
                    session.id,
                    task_id,
                    count,
                )
                with contextlib.suppress(OSError):
                    orch._signal_mgr.write_shutdown(
                        session.id,
                        reason="stalled_5min",
                        task_title=task_id,
                    )
            elif count >= 3:
                logger.info(
                    "Stall-wakeup agent %s (task %s): %d identical snapshots",
                    session.id,
                    task_id,
                    count,
                )
                with contextlib.suppress(OSError):
                    orch._signal_mgr.write_wakeup(
                        session.id,
                        task_title=task_id,
                        elapsed_s=elapsed,
                        last_activity_ago_s=elapsed,
                    )
