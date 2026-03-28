"""Hook specifications — defines extension points for Bernstein plugins."""

from __future__ import annotations

from bernstein.plugins import hookspec


class BernsteinSpec:
    """Hook specifications for Bernstein lifecycle events.

    Plugins implement one or more of these hooks via ``@hookimpl``.
    All hooks are called with keyword arguments so implementations may
    safely omit parameters they do not need.
    """

    @hookspec
    def on_task_created(self, task_id: str, role: str, title: str) -> None:
        """Called immediately after a task is created on the task server.

        Args:
            task_id: Unique task identifier.
            role: Agent role assigned to the task (e.g. ``"backend"``).
            title: Human-readable task title.
        """

    @hookspec
    def on_task_completed(self, task_id: str, role: str, result_summary: str) -> None:
        """Called when a task transitions to the ``done`` state.

        Args:
            task_id: Unique task identifier.
            role: Agent role that completed the task.
            result_summary: Short description of what was accomplished.
        """

    @hookspec
    def on_task_failed(self, task_id: str, role: str, error: str) -> None:
        """Called when a task transitions to the ``failed`` state.

        Args:
            task_id: Unique task identifier.
            role: Agent role that was working the task.
            error: Error message or failure reason.
        """

    @hookspec
    def on_agent_spawned(self, session_id: str, role: str, model: str) -> None:
        """Called right after a new agent session is spawned.

        Args:
            session_id: Unique agent session identifier.
            role: Agent role (e.g. ``"qa"``, ``"backend"``).
            model: Model identifier used for the session.
        """

    @hookspec
    def on_agent_reaped(self, session_id: str, role: str, outcome: str) -> None:
        """Called when an agent session is reaped by the janitor.

        Args:
            session_id: Unique agent session identifier.
            role: Agent role that was reaped.
            outcome: Outcome string (e.g. ``"completed"``, ``"timed_out"``).
        """

    @hookspec
    def on_evolve_proposal(self, proposal_id: str, title: str, verdict: str) -> None:
        """Called when an evolution proposal receives a verdict.

        Args:
            proposal_id: Unique proposal identifier.
            title: Proposal title.
            verdict: Final verdict (e.g. ``"accepted"``, ``"rejected"``).
        """
