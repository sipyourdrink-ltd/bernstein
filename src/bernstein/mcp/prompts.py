"""Built-in MCP prompts for the Bernstein server.

The MCP spec defines a `prompts/list` and `prompts/get` surface so a client
can browse server-side reusable prompt templates and parametrise them by
argument. Common ecosystem implementations ship at least a small built-in
prompt catalogue so an auto-discovery host can show users what the server is
useful for without sending a single tool call first.

This module registers a small catalogue tailored to Bernstein's orchestration
surface. Prompts are templates rendered server-side from parameters; they
take no live process state and do not call the task server, so they are
cheap and safe to expose unconditionally.
"""

from __future__ import annotations

import textwrap
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcp.server.fastmcp import FastMCP


def _orchestrate_goal_template(
    goal: str,
    role: str = "backend",
    scope: str = "medium",
) -> str:
    """Render the orchestration-kickoff prompt body."""
    return textwrap.dedent(
        f"""\
        You are about to drive a Bernstein orchestration run.

        Goal: {goal}
        Suggested role: {role}
        Scope: {scope}

        Plan the smallest sequence of Bernstein tool calls that lands the goal:
        1. Use `bernstein_run` to post the task with the suggested role and scope.
        2. Poll `bernstein_status` until the task settles, or call `bernstein_tasks`
           filtered by status to inspect progress.
        3. If a subtask appears blocked, report it to the user with the task id
           and the status you observed. Do not approve or complete it: an
           approval is only for a task waiting on an approval decision, and a
           completion summary must describe work that actually ran.

        Stop when the goal is done or when a tool returns an error you cannot
        recover from. Report the final task ids and statuses to the user.
        """
    ).strip()


def _triage_failed_tasks_template(limit: int = 5) -> str:
    """Render the triage-failed-tasks prompt body."""
    return textwrap.dedent(
        f"""\
        Triage the latest failed Bernstein tasks.

        Steps:
        1. Call `bernstein_tasks` with `status="failed"` to list candidates.
        2. For up to {limit} tasks, inspect the result summary and surface the
           shortest reproduction in the conversation.
        3. Group failures by likely cause (env, flaky test, real bug) and
           propose one next action per group.

        Output a compact table with columns: task_id, role, cause, next action.
        """
    ).strip()


def _cost_recap_template(window: str = "today") -> str:
    """Render the cost-recap prompt body."""
    return textwrap.dedent(
        f"""\
        Produce a cost recap for the {window} window.

        Steps:
        1. Call `bernstein_cost` to fetch the per-role breakdown.
        2. List roles in descending cost order; collapse rows under $0.01.
        3. Flag any role whose share exceeds 50% of the total as a concentration risk.

        Output: one short paragraph plus a per-role table.
        """
    ).strip()


def register_prompt_resources(mcp: FastMCP[None]) -> None:
    """Register the built-in prompt catalogue on a FastMCP server.

    Exposes three orchestration-focused prompts via the MCP `prompts/list`
    and `prompts/get` routes. Each prompt is a pure render of its arguments
    so the surface is deterministic and cheap.

    Args:
        mcp: The FastMCP server to register the prompts on.
    """

    @mcp.prompt(
        name="orchestrate_goal",
        description="Plan a Bernstein orchestration run for a single goal.",
    )
    def orchestrate_goal(  # pyright: ignore[reportUnusedFunction]
        goal: str,
        role: str = "backend",
        scope: str = "medium",
    ) -> str:
        return _orchestrate_goal_template(goal=goal, role=role, scope=scope)

    @mcp.prompt(
        name="triage_failed_tasks",
        description="Triage the most recent failed tasks and propose next actions.",
    )
    def triage_failed_tasks(  # pyright: ignore[reportUnusedFunction]
        limit: int = 5,
    ) -> str:
        return _triage_failed_tasks_template(limit=limit)

    @mcp.prompt(
        name="cost_recap",
        description="Summarise Bernstein cost by role for a stated window.",
    )
    def cost_recap(  # pyright: ignore[reportUnusedFunction]
        window: str = "today",
    ) -> str:
        return _cost_recap_template(window=window)
