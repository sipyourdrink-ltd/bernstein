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
    def on_pre_task_create(
        self,
        task_id: str,
        role: str,
        title: str,
        description: str,
    ) -> None:
        """Called before a task is created — hooks may block by raising.

        Implementations running shell scripts can exit with code 2 to
        block task creation (T719).

        Args:
            task_id: Unique task identifier (pre-generated).
            role: Agent role assigned to the task.
            title: Human-readable task title.
            description: Full task description text.
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

    @hookspec
    def on_pre_compact(self, payload: Any) -> None:
        """Called before context compaction runs (T492)."""

    @hookspec
    def on_post_compact(self, payload: Any) -> None:
        """Called after context compaction completes (T492)."""

    @hookspec(firstresult=True)
    def on_pre_tool_use(
        self,
        session_id: str,
        tool: str,
        tool_input: dict[str, Any],
    ) -> str | None:
        """Called before a tool is executed — hooks may block with exit code 2 (T681).

        Implementations can return a structured denial hint or raise
        :class:`~bernstein.plugins.manager.HookBlockingError` via command hooks
        to prevent tool execution entirely.

        Args:
            session_id: Agent session identifier.
            tool: Tool name about to be executed.
            tool_input: Redacted/safe copy of the tool arguments.

        Returns:
            Optional denial hint string, or None to allow execution.
        """

    @hookspec
    def on_post_tool_use(
        self,
        session_id: str,
        tool: str,
        tool_input: dict[str, Any],
        result: str,
        success: bool,
    ) -> None:
        """Called after a tool execution completes, regardless of outcome (T681).

        Args:
            session_id: Agent session identifier.
            tool: Tool name that was executed.
            tool_input: Tool arguments that were passed.
            result: stdout/captured output from the tool.
            success: Whether the tool exited with code 0.
        """

    @hookspec
    def on_post_tool_use_failure(
        self,
        session_id: str,
        tool: str,
        tool_input: dict[str, Any],
        error: str,
        retries: int,
    ) -> None:
        """Called when a tool execution fails after retry attempts (T681).

        Args:
            session_id: Agent session identifier.
            tool: Tool name that failed.
            tool_input: Tool arguments that were passed.
            error: Combined error output from the tool.
            retries: Number of retry attempts made.
        """

    @hookspec
    def on_notification(self, session_id: str, level: str, message: str) -> None:
        """Called when an important event should be surfaced to the operator (T681).

        Args:
            session_id: Agent session identifier (or ``""`` for system-wide).
            level: Notification severity (``"info"``, ``"warn"``, ``"error"``).
            message: Human-readable notification text.
        """

    @hookspec
    def on_user_prompt_submit(self, session_id: str, prompt: str) -> None:
        """Called when a human submits a prompt via task description or TUI (T681).

        Args:
            session_id: Agent session that will receive the prompt.
            prompt: The submitted prompt text.
        """

    @hookspec
    def on_session_start(self, session_id: str, role: str, task_id: str) -> None:
        """Called when an agent session begins, before any tool runs (T681).

        Args:
            session_id: New agent session identifier.
            role: Agent role (e.g. ``"backend"``).
            task_id: Task the agent was spawned for.
        """

    @hookspec
    def on_session_end(self, session_id: str, role: str, reason: str) -> None:
        """Called when an agent session ends (normal exit, timeout, or crash) (T681).

        Args:
            session_id: Agent session identifier.
            role: Agent role that was running.
            reason: End reason (``"completed"``, ``"killed"``, ``"timeout"``, ``"crash"``).
        """

    @hookspec
    def on_stop(self, session_id: str, reason: str, signal: str = "SIGTERM") -> None:
        """Called when a stop/shutdown is initiated for an agent session (T681).

        Args:
            session_id: Agent session being stopped.
            reason: Stop reason (e.g. ``"user_initiated"``, ``"budget_exceeded"``).
            signal: OS signal sent to the agent process.
        """

    @hookspec
    def on_stop_failure(self, session_id: str, reason: str, error: str) -> None:
        """Called when a stop attempt fails to terminate the agent (T681).

        Args:
            session_id: Agent session that failed to stop.
            reason: Original stop reason attempted.
            error: Error explaining why stop failed.
        """

    @hookspec
    def on_subagent_start(self, session_id: str, sub_id: str, role: str) -> None:
        """Called when a parent agent spawns a sub-agent (T681).

        Args:
            session_id: Parent agent session identifier.
            sub_id: Sub-agent session identifier.
            role: Sub-agent role.
        """

    @hookspec
    def on_subagent_stop(self, session_id: str, sub_id: str, outcome: str) -> None:
        """Called when a sub-agent finishes or is aborted (T681).

        Args:
            session_id: Parent agent session identifier.
            sub_id: Sub-agent session identifier.
            outcome: Sub-agent outcome (``"completed"``, ``"aborted"``).
        """

    @hookspec
    def on_permission_request(
        self,
        session_id: str,
        tool: str,
        mode: str,
    ) -> None:
        """Called before permission resolution — hooks can observe or pre-decide (T681).

        In headless mode this fires before auto-deny/auto-allow resolution.

        Args:
            session_id: Agent session identifier.
            tool: Tool requesting permission.
            mode: Permission mode (``"allow"``, ``"deny"``, ``"ask"``).
        """

    @hookspec
    def on_setup(self, session_id: str, role: str, workdir: str) -> None:
        """Called during initial workspace/worktree setup for an agent (T681).

        Args:
            session_id: Agent session identifier.
            role: Agent role.
            workdir: Path to the agent's worktree directory.
        """

    @hookspec
    def on_teammate_idle(self, session_id: str, role: str, queue_depth: int) -> None:
        """Called when an agent reports it has no more work to do (T681).

        Useful for swarm orchestration and dynamic task fan-out.

        Args:
            session_id: Idle agent session identifier.
            role: Agent role.
            queue_depth: Number of remaining open tasks.
        """

    @hookspec
    def on_elicitation(
        self,
        session_id: str,
        prompt: str,
        options: list[str],
    ) -> None:
        """Called when an LLM requests human input (elicitation) (T681).

        Args:
            session_id: Agent session making the request.
            prompt: The elicitation question text.
            options: Allowed response options.
        """

    @hookspec
    def on_elicitation_result(
        self,
        session_id: str,
        prompt: str,
        response: str,
    ) -> None:
        """Called after human input is provided for a pending elicitation (T681).

        Args:
            session_id: Agent session that made the request.
            prompt: The original elicitation question.
            response: Human's response.
        """

    @hookspec
    def on_config_change(self, key: str, old_value: str, new_value: str) -> None:
        """Called when a relevant configuration value changes at runtime (T681).

        Args:
            key: Configuration key that changed (e.g. ``"model"``).
            old_value: Previous value.
            new_value: New value.
        """

    @hookspec
    def on_worktree_create(
        self,
        session_id: str,
        worktree_path: str,
        branch: str,
    ) -> None:
        """Called when a new git worktree is created for agent isolation (T681).

        Args:
            session_id: Agent session identifier.
            worktree_path: Path to the new worktree.
            branch: Branch name the worktree points to.
        """

    @hookspec
    def on_worktree_remove(self, session_id: str, worktree_path: str) -> None:
        """Called when an agent's worktree is cleaned up (T681).

        Args:
            session_id: Agent session identifier.
            worktree_path: Path to the removed worktree.
        """

    @hookspec
    def on_instructions_loaded(self, session_id: str, role: str, source_paths: list[str]) -> None:
        """Called after all instruction files (CLAUDE.md, AGENTS.md, etc.) are loaded (T681).

        Args:
            session_id: Agent session identifier.
            role: Agent role.
            source_paths: List of instruction file paths loaded.
        """

    @hookspec
    def on_cwd_changed(self, session_id: str, old_cwd: str, new_cwd: str) -> None:
        """Called when the agent's working directory changes (T681).

        Args:
            session_id: Agent session identifier.
            old_cwd: Previous working directory.
            new_cwd: New working directory.
        """

    @hookspec
    def on_file_changed(
        self,
        session_id: str,
        file_path: str,
        change_type: str,
    ) -> None:
        """Called when a file is created, modified, or deleted in the worktree (T681).

        Args:
            session_id: Agent session identifier.
            file_path: Path to the changed file.
            change_type: Type of change (``"created"``, ``"modified"``, ``"deleted"``).
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

    @hookspec
    def on_metric_record(
        self,
        metric_type: str,
        value: float,
        labels: dict[str, Any],
    ) -> None:
        """Called when a metric point is recorded.

        Plugins can consume this hook to observe, transform, or forward
        metric data in real time (e.g. ship to external monitoring).

        Args:
            metric_type: Metric type name (e.g. ``"task_duration_s"``).
            value: Numeric metric value.
            labels: Arbitrary key-value labels attached to the point.
        """
