"""Orchestrator run loop: startup, main loop, and shutdown coordination.

Extracted from orchestrator.py as part of ORCH-009 decomposition.
Functions here operate on an Orchestrator instance passed as the first
argument, keeping the Orchestrator class as the public facade.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from bernstein.core.task_lifecycle import (
    collect_completion_data,
)

if TYPE_CHECKING:
    from bernstein.core.models import (
        AgentSession,
    )
    from bernstein.core.tick_pipeline import (
        CompletionData,
    )

logger = logging.getLogger(__name__)


def run(orch: Any) -> None:
    """Run the orchestrator loop until stopped.

    Blocks the calling thread. Call ``stop()`` from another thread or
    a signal handler to break the loop. Individual tick failures are
    caught and logged so a single bad tick cannot kill the loop.

    Args:
        orch: The orchestrator instance.
    """
    orch._running = True
    logger.info(
        "Orchestrator started (poll=%ds, max_agents=%d, server=%s)",
        orch._config.poll_interval_s,
        orch._config.max_agents,
        orch._config.server_url,
    )
    # Start cluster heartbeat client (registers this node with central server)
    if orch._heartbeat_client is not None:
        orch._heartbeat_client.start()
        logger.info("Cluster heartbeat client started")
    orch._post_bulletin("status", "run started")
    orch._notify("run.started", "Bernstein run started", "Agents are being spawned.")
    # Reconcile tasks left in "claimed" from a previous run whose agents no
    # longer exist.  Must happen after the server is confirmed reachable but
    # before the first tick.
    orch._reconcile_claimed_tasks()
    _run_started_extra: dict[str, object] = {}
    if orch._workflow_executor is not None:
        _run_started_extra["workflow_name"] = orch._workflow_executor.definition.name
        _run_started_extra["workflow_hash"] = orch._workflow_executor.definition_hash
    orch._recorder.record(
        "run_started",
        run_id=orch._run_id,
        max_agents=orch._config.max_agents,
        budget_usd=orch._config.budget_usd,
        git_sha=orch._replay_metadata.git_sha,
        git_branch=orch._replay_metadata.git_branch,
        config_hash=orch._replay_metadata.config_hash,
        **_run_started_extra,
    )
    # WAL recovery: detect uncommitted entries from crashed previous runs.
    # Must run after WAL writer is initialized (in __init__) so that
    # acknowledgement entries are written to the current run's WAL.
    try:
        orch._recover_from_wal()
    except Exception:
        logger.exception("WAL recovery failed (non-fatal) — continuing startup")
    # Audit log integrity check: verify the last N HMAC-chained entries.
    try:
        from bernstein.core.audit_integrity import verify_on_startup

        _integrity = verify_on_startup(orch._workdir / ".sdd")
        if not _integrity.valid:
            logger.warning(
                "Audit integrity check found %d error(s) — review with 'bernstein audit verify'",
                len(_integrity.errors),
            )
        elif _integrity.entries_checked > 0:
            logger.info(
                "Audit integrity OK (%d entries verified in %.1fms)",
                _integrity.entries_checked,
                _integrity.duration_ms,
            )
    except Exception:
        logger.exception("Audit integrity check failed (non-fatal) — continuing startup")
    # Zombie cleanup: terminate orphaned agent processes from prior crashed runs.
    try:
        from bernstein.core.zombie_cleanup import scan_and_cleanup_zombies

        _zr = scan_and_cleanup_zombies(orch._workdir)
        if _zr.orphans_found:
            logger.info(
                "Zombie cleanup: found=%d killed=%d stale=%d errors=%d",
                _zr.orphans_found,
                _zr.orphans_killed,
                _zr.stale_removed,
                len(_zr.errors),
            )
    except Exception:
        logger.exception("Zombie cleanup failed (non-fatal) — continuing startup")
    consecutive_failures = 0
    max_consecutive_failures = 10

    from bernstein.core.orchestrator import TickResult  # noqa: TC001

    while orch._running or _has_active_agents(orch):
        tick_result: TickResult | None = None
        try:
            tick_result = orch.tick()
            consecutive_failures = 0
        except Exception:
            consecutive_failures += 1
            logger.exception(
                "Tick %d failed (%d consecutive failures)",
                orch._tick_count,
                consecutive_failures,
            )
            if consecutive_failures >= max_consecutive_failures:
                logger.error(
                    "Stopping after %d consecutive tick failures",
                    consecutive_failures,
                )
                break
        if orch._config.dry_run:
            break
        # Adaptive backoff: double sleep when idle, reset when work is found.
        # On server failure: sleep longer to give supervisor time to restart.
        server_failures = getattr(orch, "_consecutive_server_failures", 0)
        if server_failures > 0:
            # Backoff: 5s, 10s, 15s, 20s, 30s (capped)
            time.sleep(min(5.0 * server_failures, 30.0))
        elif tick_result is not None and (
            tick_result.spawned or tick_result.verified or tick_result.retried or tick_result.open_tasks > 0
        ):
            orch._idle_multiplier = 1
            time.sleep(orch._config.poll_interval_s)
        else:
            orch._idle_multiplier = min(orch._idle_multiplier * 2, 8)
            time.sleep(min(orch._config.poll_interval_s * orch._idle_multiplier, 30.0))

        # Hot-reload bernstein.yaml config (mutable fields only)
        orch._maybe_reload_config()

        # Check if a restart was requested (own source code changed)
        restart_flag = orch._workdir / ".sdd" / "runtime" / "restart_requested"
        needs_restart = False
        if restart_flag.exists():
            restart_flag.unlink(missing_ok=True)
            needs_restart = True
        elif orch._config.evolve_mode and orch._check_source_changed():
            needs_restart = True

        if needs_restart:
            logger.info("Restarting orchestrator (own code updated)")
            orch._save_session_state()
            orch._restart()
            return  # _restart calls os.execv, but just in case

    orch._drain_before_cleanup()
    orch._cleanup()
    orch._post_bulletin("status", "run stopped")
    orch._recorder.record(
        "run_completed",
        run_id=orch._run_id,
        ticks=orch._tick_count,
        fingerprint=orch._recorder.fingerprint(),
    )
    logger.info(
        "Orchestrator stopped (replay: %s, fingerprint: %s)",
        orch._recorder.path,
        orch._recorder.fingerprint()[:16] + "...",
    )


def _has_active_agents(orch: Any) -> bool:
    """Return True if any agents are still alive (not dead).

    Args:
        orch: The orchestrator instance.

    Returns:
        True if at least one agent is alive.
    """
    alive = sum(1 for s in orch._agents.values() if s.status != "dead")
    if alive > 0 and not orch._running:
        logger.info("Orchestrator draining: %d agent(s) still active", alive)
    return alive > 0


def _collect_completion_data(orch: Any, session: AgentSession) -> CompletionData:
    """Delegate to task_lifecycle.collect_completion_data.

    Args:
        orch: The orchestrator instance.
        session: The agent session whose completion data to collect.

    Returns:
        CompletionData for the session.
    """
    return collect_completion_data(orch._workdir, session)


def _run_scheduled_dependency_scan(orch: Any) -> None:
    """Run the weekly dependency scan and enqueue remediation tasks.

    Args:
        orch: The orchestrator instance.
    """
    from bernstein.core.orchestrator_tick import _run_scheduled_dependency_scan as _impl

    _impl(orch)


def _load_existing_dependency_scan_task_titles(orch: Any) -> set[str]:
    """Load open remediation task titles so weekly scans do not duplicate them.

    Args:
        orch: The orchestrator instance.

    Returns:
        Set of existing task titles.
    """
    from bernstein.core.orchestrator_tick import _load_existing_dependency_scan_task_titles as _impl

    return _impl(orch)


def _create_dependency_fix_task(
    orch: Any,
    finding: Any,
    existing_titles: set[str],
) -> str | None:
    """Create one remediation task per vulnerable package.

    Args:
        orch: The orchestrator instance.
        finding: The vulnerability finding.
        existing_titles: Set of existing task titles for dedup.

    Returns:
        The title of the created task, or None if skipped/failed.
    """
    from bernstein.core.orchestrator_tick import _create_dependency_fix_task as _impl

    return _impl(orch, finding, existing_titles)
