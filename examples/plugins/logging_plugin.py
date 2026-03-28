"""Example plugin that logs all task lifecycle events.

To use this plugin, add it to ``bernstein.yaml``:

    plugins:
      - examples.plugins.logging_plugin:LoggingPlugin

Or register it via an entry point in your ``pyproject.toml``:

    [project.entry-points."bernstein.plugins"]
    logging = "examples.plugins.logging_plugin:LoggingPlugin"
"""

from __future__ import annotations

from bernstein.plugins import hookimpl


class LoggingPlugin:
    """Prints task and agent lifecycle events to stdout."""

    @hookimpl
    def on_task_created(self, task_id: str, role: str, title: str) -> None:
        """Print a message when a task is created."""
        print(f"[plugin] Task {task_id} ({role}) created: {title}")

    @hookimpl
    def on_task_completed(self, task_id: str, role: str, result_summary: str) -> None:
        """Print a message when a task completes."""
        print(f"[plugin] Task {task_id} ({role}) completed: {result_summary}")

    @hookimpl
    def on_task_failed(self, task_id: str, role: str, error: str) -> None:
        """Print a message when a task fails."""
        print(f"[plugin] Task {task_id} ({role}) FAILED: {error}")

    @hookimpl
    def on_agent_spawned(self, session_id: str, role: str, model: str) -> None:
        """Print a message when an agent is spawned."""
        print(f"[plugin] Agent spawned: session={session_id} role={role} model={model}")

    @hookimpl
    def on_agent_reaped(self, session_id: str, role: str, outcome: str) -> None:
        """Print a message when an agent is reaped."""
        print(f"[plugin] Agent reaped: session={session_id} role={role} outcome={outcome}")

    @hookimpl
    def on_evolve_proposal(self, proposal_id: str, title: str, verdict: str) -> None:
        """Print a message when an evolution proposal receives a verdict."""
        print(f"[plugin] Evolve proposal {proposal_id} ({title!r}): {verdict}")
