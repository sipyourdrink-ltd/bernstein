"""Sub-task streaming: parent agents observe child progress in real-time.

Provides progress tracking, intervention detection, and rendering for
parent tasks that have spawned child subtasks.  The parent agent polls
``get_session`` periodically and acts on ``should_intervene`` to decide
whether a stuck or failing subtask needs redirection, cancellation, or
retry.

Usage::

    from bernstein.core.tasks.subtask_streaming import (
        SubtaskProgress,
        StreamingSession,
        SubtaskStreamManager,
        render_progress_table,
    )

    mgr = SubtaskStreamManager()
    mgr.register_subtasks("T-parent", ("T-sub-1", "T-sub-2"))
    mgr.update_progress("T-sub-1", "running", 30.0, "parsing AST")
    session = mgr.get_session("T-parent")
    if mgr.should_intervene(session):
        actions = mgr.get_intervention_suggestions(session)
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, replace
from enum import StrEnum
from typing import Literal

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_STUCK_THRESHOLD_SECS: float = 300.0  # 5 minutes
_FAILURE_RATE_THRESHOLD: float = 0.50  # 50 %

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class SubtaskStatus(StrEnum):
    """Lifecycle status of a tracked subtask."""

    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SubtaskProgress:
    """Progress snapshot for a single subtask.

    Attributes:
        subtask_id: Unique identifier for this subtask.
        parent_task_id: The parent task that spawned this subtask.
        status: Current lifecycle status.
        progress_pct: Completion percentage (0.0 - 100.0).
        last_message: Most recent status message from the subtask agent.
        updated_at: Unix timestamp of the last progress update.
    """

    subtask_id: str
    parent_task_id: str
    status: Literal["pending", "running", "done", "failed"] = "pending"
    progress_pct: float = 0.0
    last_message: str = ""
    updated_at: float = 0.0


@dataclass(frozen=True)
class StreamingSession:
    """Aggregated progress snapshot for all subtasks of a parent task.

    Attributes:
        parent_task_id: The parent task that owns this session.
        subtasks: Immutable tuple of individual subtask progress snapshots.
        active_count: Number of subtasks currently running.
        completed_count: Number of subtasks that finished successfully.
        failed_count: Number of subtasks that failed.
    """

    parent_task_id: str
    subtasks: tuple[SubtaskProgress, ...]
    active_count: int
    completed_count: int
    failed_count: int


# ---------------------------------------------------------------------------
# Intervention suggestions
# ---------------------------------------------------------------------------


class InterventionAction(StrEnum):
    """Types of intervention a parent can take on a subtask."""

    REDIRECT = "redirect"
    CANCEL = "cancel"
    RETRY = "retry"


@dataclass(frozen=True)
class InterventionSuggestion:
    """A recommended intervention for a specific subtask.

    Attributes:
        subtask_id: The subtask that needs attention.
        action: The recommended action (redirect, cancel, retry).
        reason: Human-readable explanation.
    """

    subtask_id: str
    action: InterventionAction
    reason: str


# ---------------------------------------------------------------------------
# Manager
# ---------------------------------------------------------------------------


class SubtaskStreamManager:
    """Track and aggregate real-time progress for parent-child task trees.

    Thread safety: this class is **not** thread-safe.  Callers that share
    an instance across threads must provide external synchronisation.
    """

    def __init__(self) -> None:
        # parent_task_id -> {subtask_id -> SubtaskProgress}
        self._sessions: dict[str, dict[str, SubtaskProgress]] = {}

    # -- registration -------------------------------------------------------

    def register_subtasks(
        self,
        parent_id: str,
        subtask_ids: tuple[str, ...] | list[str],
    ) -> None:
        """Initialise tracking for a set of subtasks under a parent.

        Args:
            parent_id: The parent task identifier.
            subtask_ids: Ordered collection of subtask identifiers.

        Raises:
            ValueError: If ``subtask_ids`` is empty.
        """
        if not subtask_ids:
            raise ValueError("subtask_ids must not be empty")

        now = time.monotonic()
        entries: dict[str, SubtaskProgress] = {}
        for sid in subtask_ids:
            entries[sid] = SubtaskProgress(
                subtask_id=sid,
                parent_task_id=parent_id,
                status="pending",
                progress_pct=0.0,
                last_message="",
                updated_at=now,
            )
        self._sessions[parent_id] = entries
        logger.debug(
            "registered %d subtasks for parent %s",
            len(subtask_ids),
            parent_id,
        )

    # -- updates ------------------------------------------------------------

    def update_progress(
        self,
        subtask_id: str,
        status: Literal["pending", "running", "done", "failed"],
        progress_pct: float,
        message: str = "",
    ) -> None:
        """Update a subtask's progress.

        Args:
            subtask_id: The subtask to update.
            status: New lifecycle status.
            progress_pct: Completion percentage (clamped to 0-100).
            message: Optional human-readable status message.

        Raises:
            KeyError: If ``subtask_id`` is not tracked.
            ValueError: If ``status`` is not one of the valid literals.
        """
        valid_statuses = {"pending", "running", "done", "failed"}
        if status not in valid_statuses:
            raise ValueError(f"invalid status {status!r}, expected one of {valid_statuses}")

        parent_id = self._find_parent(subtask_id)
        old = self._sessions[parent_id][subtask_id]
        clamped_pct = max(0.0, min(100.0, progress_pct))

        self._sessions[parent_id][subtask_id] = replace(
            old,
            status=status,
            progress_pct=clamped_pct,
            last_message=message,
            updated_at=time.monotonic(),
        )

    # -- queries ------------------------------------------------------------

    def get_session(self, parent_id: str) -> StreamingSession:
        """Return an immutable snapshot of the streaming session.

        Args:
            parent_id: The parent task identifier.

        Returns:
            A frozen ``StreamingSession`` with current aggregate counts.

        Raises:
            KeyError: If ``parent_id`` has no registered subtasks.
        """
        entries = self._sessions[parent_id]
        subtasks = tuple(entries.values())
        active = sum(1 for s in subtasks if s.status == "running")
        completed = sum(1 for s in subtasks if s.status == "done")
        failed = sum(1 for s in subtasks if s.status == "failed")
        return StreamingSession(
            parent_task_id=parent_id,
            subtasks=subtasks,
            active_count=active,
            completed_count=completed,
            failed_count=failed,
        )

    # -- intervention -------------------------------------------------------

    def should_intervene(self, session: StreamingSession) -> bool:
        """Detect whether the parent should intervene on this session.

        Intervention is recommended when:
        - Any subtask has been running without an update for >5 minutes.
        - The failure rate exceeds 50 % of total subtasks.

        Args:
            session: The streaming session snapshot to evaluate.

        Returns:
            ``True`` if at least one intervention trigger fires.
        """
        if not session.subtasks:
            return False

        now = time.monotonic()

        # Check for stuck subtasks (>5 min since last update)
        for sub in session.subtasks:
            if sub.status == "running":
                elapsed = now - sub.updated_at
                if elapsed > _STUCK_THRESHOLD_SECS:
                    return True

        # Check failure rate
        total = len(session.subtasks)
        return total > 0 and session.failed_count / total > _FAILURE_RATE_THRESHOLD

    def get_intervention_suggestions(
        self,
        session: StreamingSession,
    ) -> list[InterventionSuggestion]:
        """Produce concrete suggestions for the parent to act on.

        Args:
            session: The streaming session snapshot to evaluate.

        Returns:
            A list of ``InterventionSuggestion`` instances, one per
            subtask that needs attention.  Empty if no intervention needed.
        """
        suggestions: list[InterventionSuggestion] = []
        now = time.monotonic()

        for sub in session.subtasks:
            if sub.status == "running":
                elapsed = now - sub.updated_at
                if elapsed > _STUCK_THRESHOLD_SECS:
                    suggestions.append(
                        InterventionSuggestion(
                            subtask_id=sub.subtask_id,
                            action=InterventionAction.REDIRECT,
                            reason=(f"subtask stuck for {elapsed:.0f}s (threshold {_STUCK_THRESHOLD_SECS:.0f}s)"),
                        )
                    )
            elif sub.status == "failed":
                suggestions.append(
                    InterventionSuggestion(
                        subtask_id=sub.subtask_id,
                        action=InterventionAction.RETRY,
                        reason="subtask failed — retry recommended",
                    )
                )

        # If failure rate is high, suggest cancelling remaining pending tasks
        total = len(session.subtasks)
        if total > 0 and session.failed_count / total > _FAILURE_RATE_THRESHOLD:
            for sub in session.subtasks:
                if sub.status == "pending":
                    suggestions.append(
                        InterventionSuggestion(
                            subtask_id=sub.subtask_id,
                            action=InterventionAction.CANCEL,
                            reason=(
                                f"failure rate {session.failed_count}/{total} "
                                f"exceeds {_FAILURE_RATE_THRESHOLD:.0%} threshold"
                            ),
                        )
                    )

        return suggestions

    # -- internals ----------------------------------------------------------

    def _find_parent(self, subtask_id: str) -> str:
        """Locate the parent task for a given subtask.

        Raises:
            KeyError: If the subtask is not tracked.
        """
        for parent_id, entries in self._sessions.items():
            if subtask_id in entries:
                return parent_id
        raise KeyError(f"subtask {subtask_id!r} is not tracked")


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _progress_bar(pct: float, width: int = 10) -> str:
    """Render a compact text progress bar.

    Args:
        pct: Percentage (0-100).
        width: Number of characters in the bar.

    Returns:
        A string like ``[=====     ]``.
    """
    filled = round(pct / 100 * width)
    return "[" + "=" * filled + " " * (width - filled) + "]"


def render_progress_table(session: StreamingSession) -> str:
    """Render a Markdown table summarising subtask progress.

    Args:
        session: The streaming session snapshot to render.

    Returns:
        A Markdown-formatted table string.
    """
    lines: list[str] = [
        f"## Subtask progress for {session.parent_task_id}",
        "",
        "| Subtask | Status | Progress | Message |",
        "|---------|--------|----------|---------|",
    ]
    for sub in session.subtasks:
        bar = _progress_bar(sub.progress_pct)
        lines.append(f"| {sub.subtask_id} | {sub.status} | {bar} {sub.progress_pct:5.1f}% | {sub.last_message} |")

    lines.append("")
    lines.append(
        f"**Active**: {session.active_count}  "
        f"**Completed**: {session.completed_count}  "
        f"**Failed**: {session.failed_count}"
    )
    return "\n".join(lines)
