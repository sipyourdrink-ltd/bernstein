"""Delegate a task to Bernstein from another agent or external workflow.

Allows any CLI agent or shell script to submit a task to a running Bernstein
server and optionally wait for completion.  The ``--json`` flag emits
machine-readable output so callers can parse the task ID without screen-
scraping Rich markup.
"""

from __future__ import annotations

import json
import time
from typing import Any

import click

from bernstein.cli.helpers import SERVER_URL, console, server_get, server_post


@click.command("delegate")
@click.option(
    "--task",
    "-t",
    "task_description",
    required=True,
    metavar="DESCRIPTION",
    help="Natural-language task description (becomes the task title).",
)
@click.option("--role", default="backend", show_default=True, help="Agent role for this task.")
@click.option(
    "--priority",
    type=click.IntRange(1, 3),
    default=2,
    show_default=True,
    help="Priority: 1=critical, 2=normal, 3=nice-to-have.",
)
@click.option(
    "--scope",
    type=click.Choice(["small", "medium", "large"]),
    default="medium",
    show_default=True,
    help="Estimated scope of work.",
)
@click.option(
    "--complexity",
    type=click.Choice(["low", "medium", "high"]),
    default="medium",
    show_default=True,
    help="Task complexity.",
)
@click.option(
    "--description",
    "-d",
    default="",
    metavar="TEXT",
    help="Extended description appended to the task body.",
)
@click.option(
    "--wait",
    "-w",
    is_flag=True,
    default=False,
    help="Block until the task reaches a terminal state (done/failed/cancelled).",
)
@click.option(
    "--timeout",
    default=300,
    show_default=True,
    metavar="SECONDS",
    help="Maximum seconds to wait when --wait is set (default 300).",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit machine-readable JSON: {task_id, status, poll_url}.",
)
def delegate(
    task_description: str,
    role: str,
    priority: int,
    scope: str,
    complexity: str,
    description: str,
    wait: bool,
    timeout: int,
    as_json: bool,
) -> None:
    """Delegate a task to Bernstein from any agent or shell workflow.

    Submits a new task to the running Bernstein server and returns its ID.
    Other agents can poll ``GET /tasks/<id>`` for status or pass ``--wait``
    to block until the task finishes.

    \b
    Examples:
      bernstein delegate --task 'fix flaky auth tests'
      bernstein delegate --task 'add billing integration tests' --role qa --wait
      bernstein delegate --task 'refactor auth module' --json
      bernstein delegate --task 'update docs' --priority 3 --scope small --json

    \b
    JSON output (--json):
      {"task_id": "abc123", "status": "open", "poll_url": "http://..."}

    \b
    Exit codes:
      0  Task created (or completed when --wait)
      1  Server unreachable, task failed, or --wait timeout
    """
    payload: dict[str, Any] = {
        "title": task_description,
        "description": description or task_description,
        "role": role,
        "priority": priority,
        "scope": scope,
        "complexity": complexity,
    }

    result = server_post("/tasks", payload)
    if result is None:
        if as_json:
            click.echo(json.dumps({"error": "server_unreachable", "server": SERVER_URL}))
        else:
            from bernstein.cli.errors import server_unreachable

            server_unreachable().print()
        raise SystemExit(1)

    task_id: str = result["id"]
    poll_url = f"{SERVER_URL}/tasks/{task_id}"

    if wait:
        _wait_for_completion(task_id, poll_url, timeout, as_json)
        return

    if as_json:
        click.echo(json.dumps({"task_id": task_id, "status": result.get("status", "open"), "poll_url": poll_url}))
    else:
        console.print(f"[green]Delegated:[/green] [bold]{task_id}[/bold] — {task_description}")
        console.print(f"[dim]Role: {role}  Priority: {priority}  Scope: {scope}[/dim]")
        console.print(f"[dim]Poll: bernstein delegate --task ... --wait  |  {poll_url}[/dim]")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


_TERMINAL = frozenset({"done", "failed", "cancelled"})
_POLL_INTERVAL = 2.0


def _wait_for_completion(task_id: str, poll_url: str, timeout: int, as_json: bool) -> None:
    """Poll task status until a terminal state is reached or timeout expires.

    Args:
        task_id: Bernstein task ID to poll.
        poll_url: Human-readable URL shown in output.
        timeout: Maximum wall-clock seconds to wait.
        as_json: When True, emit JSON to stdout instead of Rich output.

    Raises:
        SystemExit(1): When the task failed, was cancelled, or timeout expired.
    """
    deadline = time.monotonic() + timeout

    if not as_json:
        console.print(f"[dim]Waiting for task [bold]{task_id}[/bold] (timeout {timeout}s) …[/dim]")

    while time.monotonic() < deadline:
        data = server_get(f"/tasks/{task_id}")
        if data is None:
            if not as_json:
                console.print("[yellow]Server unreachable, retrying…[/yellow]")
            time.sleep(_POLL_INTERVAL)
            continue

        status: str = data.get("status", "unknown")

        if status in _TERMINAL:
            result_summary: str = data.get("result_summary") or ""
            if as_json:
                click.echo(
                    json.dumps(
                        {
                            "task_id": task_id,
                            "status": status,
                            "result_summary": result_summary,
                            "poll_url": poll_url,
                        }
                    )
                )
            elif status == "done":
                console.print(f"[green]Done:[/green] [bold]{task_id}[/bold]")
                if result_summary:
                    console.print(f"[dim]{result_summary}[/dim]")
            else:
                console.print(f"[red]{status.capitalize()}:[/red] [bold]{task_id}[/bold]")
                if result_summary:
                    console.print(f"[dim]{result_summary}[/dim]")
                raise SystemExit(1)
            return

        if not as_json:
            console.print(f"  [dim]status: {status}[/dim]")
        time.sleep(_POLL_INTERVAL)

    # Timeout path
    if as_json:
        click.echo(json.dumps({"task_id": task_id, "status": "timeout", "poll_url": poll_url}))
    else:
        console.print(f"[yellow]Timeout:[/yellow] task [bold]{task_id}[/bold] did not finish within {timeout}s")
        console.print(f"[dim]Poll manually: {poll_url}[/dim]")
    raise SystemExit(1)
