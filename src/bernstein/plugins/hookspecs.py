"""Hook specifications — defines extension points for Bernstein plugins."""

from __future__ import annotations

from typing import Any

from bernstein.plugins import hookspec


class BernsteinSpec:
    """Hook specifications for Bernstein lifecycle events.

    Plugins implement one or more of these hooks via ``@hookimpl``.
    All hooks are called with keyword arguments so implementations may
    safely omit parameters they do not need.

    Hooks can be marked as ``@hookspec(background=True)`` to run them in the
    background without blocking the orchestrator's main tick loop.

    **Exit Code Semantics for Hook Commands:**
    - ``0``: Success.
    - ``2``: Blocking error. The orchestration pipeline will stop, and stderr
      will be surfaced to the operator.
    - Any other non-zero: Warning. Logged as a warning, but orchestration
      continues.

    **JSON Communication Contract:**
    - **Input (stdin):** Arguments are passed as a single-line JSON object.
      Environment variables (``BERNSTEIN_HOOK_<ARG>``) are also set for convenience.
    - **Output (stdout):** Optionally, hooks can return a JSON object.
      Standard fields:
      - ``status``: ``"ok"`` or ``"error"``.
      - ``message``: Human-readable message or error details.
      - ``data``: Hook-specific structured output.
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

    @hookspec
    def on_task_hook_rejection(
        self,
        task_id: str,
        operation: str,
        hook_name: str,
        error: str,
    ) -> None:
        """Called when a hook rejects a task operation (T589).

        Args:
            task_id: Task ID that was rejected.
            operation: Operation that was blocked (e.g. ``"create"``, ``"complete"``).
            hook_name: Name of the hook that rejected the operation.
            error: Error message from the hook.
        """

    @hookspec
    def on_compaction(
        self,
        session_id: str,
        reason: str,
        tokens_before: int,
        tokens_after: int,
    ) -> None:
        """Called when context compaction occurs for an agent session (T599).

        Args:
            session_id: Agent session ID.
            reason: Compaction trigger reason.
            tokens_before: Token count before compaction.
            tokens_after: Token count after compaction.
        """

    @hookspec(firstresult=True)
    def on_permission_denied(self, task_id: str, reason: str, tool: str, args: dict[str, Any]) -> str | None:
        """Called when a tool or action permission is denied.

        Implementations can return a structured retry hint (e.g. a safer command
        or narrowed path) to be surfaced to the agent or UI.

        Args:
            task_id: Unique task identifier.
            reason: Why the permission was denied.
            tool: Tool or action name that was blocked.
            args: Redacted/safe arguments of the blocked call.

        Returns:
            Optional retry hint string.
        """

    @hookspec
    def on_tool_error(self, session_id: str, tool: str, error: str, batch_id: str | None = None) -> None:
        """Called when a tool execution fails (non-zero exit or exception).

        Args:
            session_id: Unique agent session identifier.
            tool: Tool name that failed.
            error: Error message or exit code detail.
            batch_id: Optional ID grouping concurrent tool calls.
        """
