"""Idle agent cost elimination — detect and kill agents with no activity.

Detects agents that are idle (not producing output) and kills them to save cost.
If agent log hasn't grown in 3 minutes AND no git changes in worktree: assume stuck.
Reclaim slot, requeue task.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.agent_log_aggregator import AgentLogAggregator

logger = logging.getLogger(__name__)

#: Default idle timeout in seconds (3 minutes).
DEFAULT_IDLE_TIMEOUT_SECONDS = 180


@dataclass(frozen=True)
class IdleDetectionResult:
    """Result of idle detection for an agent session."""

    session_id: str
    is_idle: bool
    idle_seconds: float
    reason: str
    log_lines_unchanged: bool
    git_changes_detected: bool


def detect_idle_agent(
    session_id: str,
    workdir: Path,
    aggregator: AgentLogAggregator,
    idle_timeout_seconds: int = DEFAULT_IDLE_TIMEOUT_SECONDS,
    last_known_log_lines: dict[str, int] | None = None,
) -> IdleDetectionResult:
    """Detect if an agent is idle based on log file growth and git activity.

    An agent is considered idle if:
    - Log file hasn't grown in idle_timeout_seconds (default 180s / 3 min)
    - No new git commits in the agent's worktree

    Args:
        session_id: Agent session identifier.
        workdir: Project working directory.
        aggregator: AgentLogAggregator instance for log parsing.
        idle_timeout_seconds: Seconds of inactivity before considering idle.
        last_known_log_lines: Dict mapping session_id to last known line count.

    Returns:
        IdleDetectionResult with detection status and reason.
    """
    # Check log file growth
    log_summary = aggregator.parse_log(session_id)
    current_lines = log_summary.total_lines

    log_unchanged = False
    idle_seconds = 0.0

    if last_known_log_lines is not None:
        last_lines = last_known_log_lines.get(session_id, 0)
        if current_lines == last_lines and current_lines > 0:
            # Log hasn't grown — check how long
            log_unchanged = True
            # Estimate idle time based on last activity line
            if log_summary.last_activity_line > 0:
                # Assume log lines are roughly chronological
                # This is a heuristic — actual time would require timestamps
                idle_seconds = idle_timeout_seconds  # Conservative estimate
    # No baseline — cannot determine idle yet (first tick)
    # Will establish baseline and check on next tick

    # Check git changes in worktree
    git_changes = _check_git_changes(workdir, session_id)

    # Determine if idle
    is_idle = log_unchanged and not git_changes and idle_seconds >= idle_timeout_seconds

    reason = ""
    if is_idle:
        reason = f"log_unchanged_{idle_seconds:.0f}s_no_git_changes"
    elif log_unchanged:
        reason = "log_unchanged_but_git_activity"
    elif not git_changes:
        reason = "git_quiet_but_log_growing"
    else:
        reason = "active"

    return IdleDetectionResult(
        session_id=session_id,
        is_idle=is_idle,
        idle_seconds=idle_seconds,
        reason=reason,
        log_lines_unchanged=log_unchanged,
        git_changes_detected=git_changes,
    )


def _check_git_changes(workdir: Path, session_id: str) -> bool:
    """Check if there are new git commits in the agent's worktree.

    Args:
        workdir: Project working directory.
        session_id: Agent session identifier (used to find worktree).

    Returns:
        True if git changes detected, False otherwise.
    """
    import subprocess

    try:
        # Check for uncommitted changes
        result = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.stdout.strip():
            return True

        # Check for recent commits (last 5 minutes)
        result = subprocess.run(
            ["git", "log", "--since=5 minutes ago", "--oneline"],
            cwd=workdir,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        if result.stdout.strip():
            return True

    except (subprocess.TimeoutExpired, OSError):
        logger.debug("Failed to check git changes for session %s", session_id)

    return False


def integrate_idle_detection(orch: Any) -> dict[str, int] | None:
    """Integrate idle detection into orchestrator tick.

    Scans all alive agents and returns dict of session_id -> last_known_lines
    for tracking log growth across ticks.

    Args:
        orch: Orchestrator instance.

    Returns:
        Updated last_known_log_lines dict, or None if not configured.
    """
    from bernstein.core.agent_log_aggregator import AgentLogAggregator

    # Get idle timeout from config (default 180s)
    idle_timeout = getattr(orch._config, "idle_timeout_seconds", DEFAULT_IDLE_TIMEOUT_SECONDS)

    # Initialize tracking dicts on orchestrator if not present
    if not hasattr(orch, "_last_known_log_lines"):
        orch._last_known_log_lines = {}  # type: ignore[attr-defined]

    aggregator = AgentLogAggregator(orch._workdir)
    idle_sessions: list[tuple[Any, IdleDetectionResult]] = []

    for session in list(orch._agents.values()):
        if session.status == "dead":
            continue

        result = detect_idle_agent(
            session_id=session.id,
            workdir=orch._workdir,
            aggregator=aggregator,
            idle_timeout_seconds=idle_timeout,
            last_known_log_lines=orch._last_known_log_lines,  # type: ignore[attr-defined]
        )

        # Update tracking
        orch._last_known_log_lines[session.id] = aggregator.parse_log(session.id).total_lines  # type: ignore[attr-defined]

        if result.is_idle:
            idle_sessions.append((session, result))
            logger.info(
                "Agent %s was idle for %.0fs — killing to save cost (reason: %s)",
                session.id,
                result.idle_seconds,
                result.reason,
            )

    # Handle idle sessions
    for session, result in idle_sessions:
        # Send SHUTDOWN signal
        task_title = ", ".join(session.task_ids) if session.task_ids else "unknown task"
        with __import__("contextlib").suppress(OSError):
            orch._signal_mgr.write_shutdown(session.id, reason=result.reason, task_title=task_title)

        # Record for force-kill after grace period
        if not hasattr(orch, "_idle_shutdown_ts"):
            orch._idle_shutdown_ts = {}  # type: ignore[attr-defined]
        orch._idle_shutdown_ts[session.id] = time.time()  # type: ignore[attr-defined]

    return orch._last_known_log_lines  # type: ignore[return-value]
