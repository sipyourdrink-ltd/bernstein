"""Explain command — show routing rationale, execution trace, and outcome for a task."""
# TODO(D6): Not yet wired into main.py CLI group. WIP — routing explanation
# reconstructs router logic heuristically and may diverge from live
# core/router.py decisions. Wire in once _explain_routing is validated
# against actual router state. See p0-documentation-overhaul.md.

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click
from rich.panel import Panel
from rich.table import Table

from bernstein.cli.helpers import console, server_get

if TYPE_CHECKING:
    from bernstein.core.traces import AgentTrace


# ---------------------------------------------------------------------------
# Routing explanation logic (mirrors _select_model_config in core/router.py)
# ---------------------------------------------------------------------------


def _explain_routing(task: dict[str, Any]) -> tuple[str, str, str]:
    """Return (model, effort, reason) describing the routing decision.

    Reconstructs the router's decision tree from task metadata so the user
    can see *why* a specific model was chosen without needing live bandit data.

    Args:
        task: Task dict as returned by GET /tasks/{id}.

    Returns:
        Tuple of (model, effort, human-readable reason).
    """
    role = task.get("role", "")
    priority = task.get("priority", 2)
    scope = task.get("scope", "medium")
    complexity = task.get("complexity", "medium")
    model_hint: str | None = task.get("model")
    effort_hint: str | None = task.get("effort")

    if model_hint or effort_hint:
        m = model_hint or "sonnet"
        e = effort_hint or "high"
        return m, e, f"Manager-specified override (model={m!r}, effort={e!r})"

    if role == "manager":
        return "opus", "max", "High-stakes role 'manager' → always routed to opus/max"

    if role in ("architect", "security"):
        return "opus", "max", f"High-stakes role '{role}' → always routed to opus/max"

    if scope == "large":
        return "opus", "max", "Large-scope task (scope=large) → always routed to opus/max"

    if priority == 1:
        return "opus", "max", "Critical priority (P1) → always routed to opus/max"

    if complexity == "high":
        return (
            "sonnet",
            "high",
            "High-complexity task → sonnet/high (heuristic fallback; L1 fast-path skipped)",
        )

    return (
        "sonnet",
        "high",
        "Standard task → sonnet/high (epsilon-greedy bandit or heuristic default)",
    )


# ---------------------------------------------------------------------------
# Rendering helpers
# ---------------------------------------------------------------------------

_STEP_COLORS: dict[str, str] = {
    "spawn": "blue",
    "orient": "cyan",
    "plan": "yellow",
    "edit": "green",
    "verify": "magenta",
    "complete": "bold green",
    "fail": "bold red",
}

_STATUS_COLORS: dict[str, str] = {
    "done": "green",
    "failed": "red",
    "in_progress": "yellow",
    "claimed": "cyan",
    "open": "white",
    "blocked": "magenta",
    "cancelled": "red",
}


def _render_task_panel(task: dict[str, Any]) -> None:
    status = task.get("status", "unknown")
    color = _STATUS_COLORS.get(status, "white")

    grid = Table.grid(padding=(0, 2))
    grid.add_row("[bold]ID[/bold]", task.get("id", "?"))
    grid.add_row("[bold]Title[/bold]", task.get("title", "?"))
    grid.add_row("[bold]Role[/bold]", task.get("role", "?"))
    grid.add_row("[bold]Status[/bold]", f"[{color}]{status}[/{color}]")
    grid.add_row("[bold]Priority[/bold]", f"P{task.get('priority', 2)}")
    grid.add_row("[bold]Scope[/bold]", task.get("scope", "?"))
    grid.add_row("[bold]Complexity[/bold]", task.get("complexity", "?"))
    if task.get("assigned_agent"):
        grid.add_row("[bold]Agent[/bold]", task["assigned_agent"])
    desc = task.get("description", "")
    if desc:
        grid.add_row("[bold]Description[/bold]", desc[:200] + ("…" if len(desc) > 200 else ""))

    console.print(Panel(grid, title="[bold blue]Task[/bold blue]", border_style="blue"))


def _render_routing_panel(task: dict[str, Any]) -> None:
    model, effort, reason = _explain_routing(task)

    grid = Table.grid(padding=(0, 2))
    grid.add_row("[bold]Model[/bold]", f"[cyan]{model}[/cyan]")
    grid.add_row("[bold]Effort[/bold]", f"[cyan]{effort}[/cyan]")
    grid.add_row("[bold]Reason[/bold]", reason)
    if task.get("batch_eligible"):
        grid.add_row("[bold]Batch[/bold]", "[dim]eligible (is_batch=True for non-P1 tasks)[/dim]")

    console.print(Panel(grid, title="[bold yellow]Routing Decision[/bold yellow]", border_style="yellow"))


def _render_trace_panel(trace: AgentTrace) -> None:
    outcome_color = {"success": "green", "failed": "red", "unknown": "dim"}.get(trace.outcome, "dim")

    header = Table.grid(padding=(0, 2))
    header.add_row("[bold]Session[/bold]", trace.session_id)
    header.add_row("[bold]Model[/bold]", f"{trace.model}/{trace.effort}")
    header.add_row("[bold]Outcome[/bold]", f"[{outcome_color}]{trace.outcome}[/{outcome_color}]")
    if trace.duration_s is not None:
        header.add_row("[bold]Duration[/bold]", f"{trace.duration_s:.1f}s")
    header.add_row("[bold]Steps[/bold]", str(len(trace.steps)))

    console.print(Panel(header, title="[bold]Execution Trace[/bold]", border_style="dim"))

    if trace.steps:
        steps_table = Table(show_header=True, header_style="bold magenta", padding=(0, 1), box=None)
        steps_table.add_column("#", style="dim", width=4, justify="right")
        steps_table.add_column("Type", width=10)
        steps_table.add_column("Detail")
        steps_table.add_column("Files", style="dim")

        for i, step in enumerate(trace.steps, 1):
            color = _STEP_COLORS.get(step.type, "white")
            files_str = ", ".join(step.files[:2]) + ("…" if len(step.files) > 2 else "")
            steps_table.add_row(
                str(i),
                f"[{color}]{step.type}[/{color}]",
                step.detail[:80] if step.detail else "",
                files_str[:60],
            )

        console.print(steps_table)


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------


@click.command("explain")
@click.argument("task_id")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Output raw JSON.",
)
@click.option(
    "--traces-dir",
    default=".sdd/traces",
    show_default=True,
    help="Directory containing trace files.",
)
def explain_cmd(task_id: str, as_json: bool, traces_dir: str) -> None:
    """Explain routing, execution, and outcome for a task.

    \b
    Shows three sections:
      • Routing decision — why this model/effort was chosen
      • Execution trace  — what the agent did, step by step
      • Outcome          — success/failure with result summary

    \b
    Reads task metadata from the running server and traces from
    .sdd/traces/. Works without a running server if traces exist.

    \b
    Examples:
      bernstein explain abc123
      bernstein explain abc123 --json
    """
    # 1. Fetch task from server
    task: dict[str, Any] | None = server_get(f"/tasks/{task_id}")
    if task is None:
        console.print(
            f"[red]Task not found or server unreachable:[/red] {task_id}\n"
            "[dim]Start the server with 'bernstein run' or check the task ID.[/dim]"
        )
        raise SystemExit(1)

    # 2. Load traces
    from bernstein.core.traces import TraceStore

    store = TraceStore(Path(traces_dir))
    traces = store.read_by_task(task_id)

    # 3. JSON output
    if as_json:
        model, effort, reason = _explain_routing(task)
        output: dict[str, Any] = {
            "task": task,
            "routing": {"model": model, "effort": effort, "reason": reason},
            "traces": [t.to_dict() for t in traces],
        }
        console.print_json(json.dumps(output))
        return

    # 4. Rich display
    _render_task_panel(task)
    _render_routing_panel(task)

    if not traces:
        console.print(
            Panel(
                "[dim]No execution traces found for this task.[/dim]",
                title="[bold]Execution Trace[/bold]",
                border_style="dim",
            )
        )
    else:
        # Most recent trace
        _render_trace_panel(traces[-1])
        if len(traces) > 1:
            console.print(
                f"[dim]({len(traces) - 1} earlier trace(s) — use 'bernstein trace {task_id}' for full history)[/dim]"
            )

    result = task.get("result_summary")
    if result:
        status = task.get("status", "unknown")
        border = "green" if status == "done" else "red" if status == "failed" else "dim"
        console.print(Panel(result, title="[bold]Result Summary[/bold]", border_style=border))
