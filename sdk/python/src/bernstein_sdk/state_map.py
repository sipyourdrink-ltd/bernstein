"""Bidirectional state mappings between Bernstein task states and external trackers.

Usage::

    from bernstein_sdk.state_map import JiraToBernstein, BernsteinToJira

    b_status = JiraToBernstein.map("In Progress")   # → TaskStatus.IN_PROGRESS
    jira_status = BernsteinToJira.map(TaskStatus.DONE)  # → "Done"
"""

from __future__ import annotations

from bernstein_sdk.models import TaskStatus

# ---------------------------------------------------------------------------
# Jira ↔ Bernstein
# ---------------------------------------------------------------------------

# These are the default Jira status names shipped with the standard
# "Scrum" and "Kanban" project templates.  Projects with custom workflows
# will need to override mappings via JiraToBernstein.register().
_JIRA_TO_BERNSTEIN: dict[str, TaskStatus] = {
    # Backlog / todo
    "backlog": TaskStatus.OPEN,
    "to do": TaskStatus.OPEN,
    "open": TaskStatus.OPEN,
    "new": TaskStatus.OPEN,
    "selected for development": TaskStatus.OPEN,
    # Active
    "in progress": TaskStatus.IN_PROGRESS,
    "in review": TaskStatus.IN_PROGRESS,
    "under review": TaskStatus.IN_PROGRESS,
    "code review": TaskStatus.IN_PROGRESS,
    # Waiting
    "blocked": TaskStatus.BLOCKED,
    "waiting": TaskStatus.BLOCKED,
    "on hold": TaskStatus.BLOCKED,
    "waiting for review": TaskStatus.BLOCKED,
    # Terminal
    "done": TaskStatus.DONE,
    "closed": TaskStatus.DONE,
    "resolved": TaskStatus.DONE,
    "won't do": TaskStatus.CANCELLED,
    "wont do": TaskStatus.CANCELLED,
    "cancelled": TaskStatus.CANCELLED,
    "duplicate": TaskStatus.CANCELLED,
}

_BERNSTEIN_TO_JIRA: dict[TaskStatus, str] = {
    TaskStatus.OPEN: "To Do",
    TaskStatus.CLAIMED: "In Progress",
    TaskStatus.IN_PROGRESS: "In Progress",
    TaskStatus.DONE: "Done",
    TaskStatus.FAILED: "Done",  # fallback — Jira has no native "Failed"
    TaskStatus.BLOCKED: "Blocked",
    TaskStatus.CANCELLED: "Won't Do",
    TaskStatus.ORPHANED: "To Do",  # re-open for retry
}


class JiraToBernstein:
    """Map a Jira issue status name to a :class:`TaskStatus`.

    Case-insensitive.  Unknown statuses fall back to ``TaskStatus.OPEN``.

    Example::

        status = JiraToBernstein.map("In Progress")   # → TaskStatus.IN_PROGRESS
        status = JiraToBernstein.map("Custom Status") # → TaskStatus.OPEN (fallback)
    """

    _overrides: dict[str, TaskStatus] = {}

    @classmethod
    def map(
        cls, jira_status: str, fallback: TaskStatus = TaskStatus.OPEN
    ) -> TaskStatus:
        """Map *jira_status* (case-insensitive) to a :class:`TaskStatus`."""
        key = jira_status.lower().strip()
        return cls._overrides.get(key) or _JIRA_TO_BERNSTEIN.get(key, fallback)

    @classmethod
    def register(cls, jira_status: str, bernstein_status: TaskStatus) -> None:
        """Register a project-specific mapping that overrides the defaults.

        Args:
            jira_status: The Jira status label (case-insensitive).
            bernstein_status: The :class:`TaskStatus` to map to.
        """
        cls._overrides[jira_status.lower().strip()] = bernstein_status


class BernsteinToJira:
    """Map a :class:`TaskStatus` to a Jira status transition name.

    Example::

        label = BernsteinToJira.map(TaskStatus.DONE)  # → "Done"
    """

    _overrides: dict[TaskStatus, str] = {}

    @classmethod
    def map(cls, status: TaskStatus, fallback: str = "To Do") -> str:
        """Return the Jira status name for *status*."""
        return cls._overrides.get(status) or _BERNSTEIN_TO_JIRA.get(status, fallback)

    @classmethod
    def register(cls, bernstein_status: TaskStatus, jira_status: str) -> None:
        """Override the default Jira target for a given Bernstein state."""
        cls._overrides[bernstein_status] = jira_status


# ---------------------------------------------------------------------------
# Linear ↔ Bernstein
# ---------------------------------------------------------------------------

# Linear uses fixed state *types*: triage, backlog, unstarted, started,
# completed, cancelled.  Projects may also add custom state *names* within
# each type.  We map both type and common names.
_LINEAR_TO_BERNSTEIN: dict[str, TaskStatus] = {
    # Type-level
    "triage": TaskStatus.OPEN,
    "backlog": TaskStatus.OPEN,
    "unstarted": TaskStatus.OPEN,
    "todo": TaskStatus.OPEN,
    "started": TaskStatus.IN_PROGRESS,
    "in progress": TaskStatus.IN_PROGRESS,
    "in review": TaskStatus.IN_PROGRESS,
    "completed": TaskStatus.DONE,
    "done": TaskStatus.DONE,
    "cancelled": TaskStatus.CANCELLED,
    "canceled": TaskStatus.CANCELLED,
    "duplicate": TaskStatus.CANCELLED,
    "blocked": TaskStatus.BLOCKED,
    "waiting": TaskStatus.BLOCKED,
}

_BERNSTEIN_TO_LINEAR: dict[TaskStatus, str] = {
    TaskStatus.OPEN: "Todo",
    TaskStatus.CLAIMED: "In Progress",
    TaskStatus.IN_PROGRESS: "In Progress",
    TaskStatus.DONE: "Done",
    TaskStatus.FAILED: "Cancelled",  # Linear has no native "Failed"
    TaskStatus.BLOCKED: "Blocked",
    TaskStatus.CANCELLED: "Cancelled",
    TaskStatus.ORPHANED: "Todo",
}


class LinearToBernstein:
    """Map a Linear issue state name to a :class:`TaskStatus`.

    Matches against both the state *type* (``"started"``) and the state
    *name* (``"In Progress"``), case-insensitive.  Unknown values fall back
    to ``TaskStatus.OPEN``.

    Example::

        status = LinearToBernstein.map("In Progress")  # → TaskStatus.IN_PROGRESS
        status = LinearToBernstein.map("started")       # → TaskStatus.IN_PROGRESS
    """

    _overrides: dict[str, TaskStatus] = {}

    @classmethod
    def map(
        cls, linear_state: str, fallback: TaskStatus = TaskStatus.OPEN
    ) -> TaskStatus:
        """Map *linear_state* (case-insensitive) to a :class:`TaskStatus`."""
        key = linear_state.lower().strip()
        return cls._overrides.get(key) or _LINEAR_TO_BERNSTEIN.get(key, fallback)

    @classmethod
    def register(cls, linear_state: str, bernstein_status: TaskStatus) -> None:
        """Register a workspace-specific state mapping."""
        cls._overrides[linear_state.lower().strip()] = bernstein_status


class BernsteinToLinear:
    """Map a :class:`TaskStatus` to a Linear state name.

    Example::

        name = BernsteinToLinear.map(TaskStatus.DONE)  # → "Done"
    """

    _overrides: dict[TaskStatus, str] = {}

    @classmethod
    def map(cls, status: TaskStatus, fallback: str = "Todo") -> str:
        """Return the Linear state name for *status*."""
        return cls._overrides.get(status) or _BERNSTEIN_TO_LINEAR.get(status, fallback)

    @classmethod
    def register(cls, bernstein_status: TaskStatus, linear_state: str) -> None:
        """Override the default Linear target for a given Bernstein state."""
        cls._overrides[bernstein_status] = linear_state
