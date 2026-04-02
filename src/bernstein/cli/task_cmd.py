"""Task lifecycle commands for Bernstein CLI.

This module contains all task-related commands:
  cancel, add_task, list_tasks, approve, reject, pending, review_cmd, sync, logs_cmd, plan

All commands are registered with the main CLI group in main.py.
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, cast

import click

from bernstein.cli.helpers import (
    STATUS_COLORS,
    console,
    is_json,
    print_json,
    server_get,
    server_post,
)

# This will be populated by main.py after it creates the CLI group
_cli: Any = None


def _set_cli(cli_group: Any) -> None:  # type: ignore[reportUnusedFunction]
    """Set the main CLI group (called by main.py)."""
    global _cli
    _cli = cli_group


@click.command("compose", hidden=True)
@click.argument("title")
@click.option("--role", default="backend", show_default=True, help="Agent role for this task.")
@click.option("--description", "-d", default="", help="Task description.")
@click.option(
    "--priority",
    type=click.IntRange(1, 3),
    default=2,
    show_default=True,
    help="1=critical, 2=normal, 3=nice-to-have.",
)
@click.option(
    "--scope",
    type=click.Choice(["small", "medium", "large"]),
    default="medium",
    show_default=True,
)
@click.option(
    "--complexity",
    type=click.Choice(["low", "medium", "high"]),
    default="medium",
    show_default=True,
)
@click.option("--depends-on", multiple=True, metavar="TASK_ID", help="Task IDs this depends on.")
@click.pass_context
def add_task(
    ctx: click.Context,
    title: str,
    role: str,
    description: str,
    priority: int,
    scope: str,
    complexity: str,
    depends_on: tuple[str, ...],
) -> None:
    """Add a task to the running server.

    TITLE is the short task name.
    """
    payload: dict[str, Any] = {
        "title": title,
        "role": role,
        "description": description,
        "priority": priority,
        "scope": scope,
        "complexity": complexity,
        "depends_on": list(depends_on),
    }

    result = server_post("/tasks", payload)
    if result is None:
        from bernstein.cli.errors import server_unreachable

        server_unreachable().print()
        raise SystemExit(1)

    task_id = result.get("id", "?")
    if is_json():
        print_json(result)
    else:
        console.print(
            f"[green]Task added:[/green] [bold]{task_id}[/bold] — {title} ([dim]role={role}, priority={priority}[/dim])"
        )


@click.command("sync", hidden=True)
@click.option(
    "--port",
    default=8052,
    show_default=True,
    help="Task server port.",
)
@click.option(
    "--dir",
    "workdir",
    default=".",
    show_default=True,
    help="Project root directory (parent of .sdd/).",
)
def sync(port: int, workdir: str) -> None:
    """Sync .sdd/backlog/open/*.md files with the task server.

    \b
    Creates server tasks for new backlog files not yet on the server.
    Moves .md files to backlog/done/ when their task is completed.
    """
    from bernstein.core.sync import sync_backlog_to_server

    root = Path(workdir).resolve()
    result = sync_backlog_to_server(root, server_url=f"http://127.0.0.1:{port}")

    if result.created:
        console.print(f"[green]Created {len(result.created)} task(s):[/green] " + ", ".join(result.created))
    if result.skipped:
        console.print(f"[dim]Skipped {len(result.skipped)} file(s) already on server[/dim]")
    if result.moved:
        console.print(
            f"[green]Moved {len(result.moved)} completed file(s) to backlog/done/:[/green] " + ", ".join(result.moved)
        )
    for err in result.errors:
        console.print(f"[red]Error:[/red] {err}")

    if not result.created and not result.moved and not result.errors:
        if result.skipped:
            console.print("[dim]All backlog files already synced.[/dim]")
        else:
            console.print("[dim]Nothing to sync — backlog/open/ is empty.[/dim]")


@click.command()
@click.argument("task_id")
@click.option("--reason", "-r", default="Cancelled by user", help="Cancellation reason")
def cancel(task_id: str, reason: str) -> None:
    """Cancel a running or queued task."""
    data = server_post(f"/tasks/{task_id}/cancel", {"reason": reason})
    if data is None:
        from bernstein.cli.errors import server_unreachable

        server_unreachable().print()
        raise SystemExit(1)
    console.print(f"[green]Cancelled:[/green] {data['title']}")
    console.print(f"[dim]Status: {data['status']}[/dim]")


@click.command("review")
@click.option("--workdir", default=".", help="Project root directory.", type=click.Path())
def review_cmd(workdir: str) -> None:
    """Trigger an immediate manager queue review.

    Writes a flag file that the running orchestrator picks up on its next
    tick, prompting the manager agent to inspect the task queue and issue
    corrections (reassign mis-routed tasks, cancel stalled tasks, etc.).

    \b
    Example:
      bernstein review
    """
    flag = Path(workdir) / ".sdd" / "runtime" / "review_requested"
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text("1")
    console.print(
        "[green]Review queued.[/green] The manager will inspect the task queue on the next orchestrator tick."
    )


@click.command("approve")
@click.argument("task_id")
@click.option("--workdir", default=".", help="Project root directory.", type=click.Path())
def approve(task_id: str, workdir: str) -> None:
    """Approve a pending task review so Bernstein merges the work.

    When running with ``--approval review``, Bernstein pauses after each
    verified task and writes a pending approval file.  Run this command
    to signal approval so the orchestrator continues with the merge.

    \b
    Example:
      bernstein approve T-abc123
    """
    approvals_dir = Path(workdir) / ".sdd" / "runtime" / "approvals"
    approvals_dir.mkdir(parents=True, exist_ok=True)
    decision_file = approvals_dir / f"{task_id}.approved"
    decision_file.write_text("approved")
    console.print(f"[green]Approved:[/green] task [bold]{task_id}[/bold] — Bernstein will merge the work.")


@click.command("reject")
@click.argument("task_id")
@click.option("--workdir", default=".", help="Project root directory.", type=click.Path())
def reject(task_id: str, workdir: str) -> None:
    """Reject a pending task review so Bernstein discards the work.

    When running with ``--approval review``, Bernstein pauses after each
    verified task and writes a pending approval file.  Run this command
    to reject the work -- the worktree will be cleaned up without merging.

    \b
    Example:
      bernstein reject T-abc123
    """
    approvals_dir = Path(workdir) / ".sdd" / "runtime" / "approvals"
    approvals_dir.mkdir(parents=True, exist_ok=True)
    decision_file = approvals_dir / f"{task_id}.rejected"
    decision_file.write_text("rejected")
    console.print(f"[red]Rejected:[/red] task [bold]{task_id}[/bold] — work will be discarded.")


@click.command("pending")
@click.option("--workdir", default=".", help="Project root directory.", type=click.Path())
@click.pass_context
def pending(ctx: click.Context, workdir: str) -> None:
    """List tasks waiting for approval review.

    Shows all tasks that have been verified and are waiting for a human
    decision (``bernstein approve <id>`` or ``bernstein reject <id>``).
    """
    from rich.table import Table

    pending_dir = Path(workdir) / ".sdd" / "runtime" / "pending_approvals"
    if not pending_dir.exists() or not any(pending_dir.glob("*.json")):
        if ctx.obj.get("JSON"):
            console.print_json(data=[])
        else:
            console.print("[dim]No tasks pending approval.[/dim]")
        return

    results: list[dict[str, Any]] = []
    for f in sorted(pending_dir.glob("*.json")):
        try:
            import json as _json

            data = _json.loads(f.read_text())
            results.append(data)
        except Exception:
            results.append({"task_id": f.stem, "error": "unreadable"})

    if ctx.obj.get("JSON"):
        console.print_json(data=results)
        return

    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Task ID", style="cyan")
    table.add_column("Title")
    table.add_column("Tests")

    for res in results:
        table.add_row(
            res.get("task_id", "?"),
            res.get("task_title", ""),
            res.get("test_summary", ""),
        )

    console.print(table)
    console.print("\n[dim]Approve with:[/dim] bernstein approve <task_id>")
    console.print("[dim]Reject with:[/dim]  bernstein reject <task_id>")


def _render_graph() -> None:
    """Render ASCII task dependency graph using rich.tree.Tree."""
    from rich.text import Text
    from rich.tree import Tree

    raw = server_get("/tasks/graph")
    if raw is None:
        from bernstein.cli.errors import server_unreachable

        server_unreachable().print()
        raise SystemExit(1)

    data: dict[str, Any] = raw if isinstance(raw, dict) else {}  # type: ignore[assignment]
    nodes: list[dict[str, Any]] = data.get("nodes", [])
    edges: list[dict[str, Any]] = data.get("edges", [])
    critical_path: list[str] = data.get("critical_path", [])

    if not nodes:
        console.print("[dim]No tasks found.[/dim]")
        return

    # Build maps
    task_map: dict[str, dict[str, Any]] = {n["id"]: n for n in nodes}
    # forward adjacency: source -> [targets]
    forward: dict[str, list[str]] = {n["id"]: [] for n in nodes}
    for e in edges:
        src: str = e["from"]
        tgt: str = e["to"]
        if src in forward:
            forward[src].append(tgt)

    critical_set: set[str] = set(critical_path)

    def _node_text(tid: str) -> Text:
        t = task_map.get(tid, {})
        short_id = tid[:8]
        title = str(t.get("title", "?"))
        status = str(t.get("status", "?"))
        status_color = STATUS_COLORS.get(status, "white")
        text = Text()
        if tid in critical_set:
            text.append(f"[{short_id}] {title}", style="bold yellow")
            text.append(" ★", style="bold yellow")
        else:
            text.append(f"[{short_id}] {title}")
        text.append(f" ({status})", style=status_color)
        return text

    # Nodes that have an incoming edge
    has_incoming: set[str] = {e["to"] for e in edges}
    roots = [n["id"] for n in nodes if n["id"] not in has_incoming]

    tree = Tree("[bold cyan]Task Dependency Graph[/bold cyan]")
    visited: set[str] = set()

    def _add_branch(parent: Any, tid: str) -> None:
        visited.add(tid)
        branch = parent.add(_node_text(tid))
        for child in forward.get(tid, []):
            if child not in visited:
                _add_branch(branch, child)
            else:
                # Already shown elsewhere — add a reference stub
                branch.add(Text(f"  ↳ [{child[:8]}] (shown above)", style="dim italic"))

    for root_id in sorted(roots):
        _add_branch(tree, root_id)

    # Nodes with no edges at all (isolated)
    edge_nodes: set[str] = {e["from"] for e in edges} | {e["to"] for e in edges}
    for n in nodes:
        if n["id"] not in visited and n["id"] not in edge_nodes:
            tree.add(_node_text(n["id"]))

    console.print(tree)

    if critical_path:
        console.print()
        cp_ids = " → ".join(tid[:8] for tid in critical_path)
        console.print(f"[bold yellow]Critical path:[/bold yellow] {cp_ids}")
        minutes: int = int(data.get("critical_path_minutes", 0))
        if minutes:
            console.print(f"[dim]Estimated duration: {minutes} min[/dim]")

    bottlenecks: list[str] = data.get("bottlenecks", [])
    if bottlenecks:
        console.print()
        console.print("[bold red]Bottlenecks:[/bold red] " + ", ".join(b[:8] for b in bottlenecks))


@click.command("plan")
@click.option(
    "--export",
    "export_file",
    default=None,
    metavar="FILE",
    help="Write full task list as formatted JSON to FILE.",
)
@click.option(
    "--status",
    "status_filter",
    default=None,
    type=click.Choice(["open", "claimed", "in_progress", "done", "failed", "blocked", "cancelled"]),
    help="Filter tasks by status.",
)
@click.option("--graph", "show_graph", is_flag=True, default=False, help="Show ASCII dependency graph.")
def plan(export_file: str | None, status_filter: str | None, show_graph: bool) -> None:
    """Show task backlog as a table, or export to JSON.

    \b
      bernstein plan                          # show all tasks
      bernstein plan --status open            # show only open tasks
      bernstein plan --export plan.json       # export full backlog to JSON
      bernstein plan --graph                  # show ASCII dependency graph
    """
    if show_graph:
        _render_graph()
        return

    from rich.table import Table

    path = "/tasks"
    if status_filter:
        path = f"/tasks?status={status_filter}"

    raw = server_get(path)
    if raw is None:
        from bernstein.cli.errors import server_unreachable

        server_unreachable().print()
        raise SystemExit(1)

    tasks: list[dict[str, Any]] = cast("list[dict[str, Any]]", raw) if isinstance(raw, list) else []

    if export_file:
        out = Path(export_file)
        out.write_text(json.dumps(tasks, indent=2))
        console.print(f"Exported {len(tasks)} tasks to {export_file}")
        return

    if not tasks:
        console.print("[dim]No tasks found.[/dim]")
        return

    table = Table(title="Task Backlog", show_lines=False, header_style="bold cyan")
    table.add_column("ID", style="dim", min_width=10)
    table.add_column("Status", min_width=12)
    table.add_column("Role", min_width=10)
    table.add_column("Title", min_width=30)
    table.add_column("Depends On", min_width=12)
    table.add_column("Model", min_width=8)
    table.add_column("Effort", min_width=8)

    for t in tasks:
        raw_status: str = t.get("status", "open")
        color = STATUS_COLORS.get(raw_status, "white")
        depends = ", ".join(d[:8] for d in cast("list[str]", t.get("depends_on", []))) or "—"
        table.add_row(
            str(t.get("id", "—"))[:8],
            f"[{color}]{raw_status}[/{color}]",
            str(t.get("role", "—")),
            str(t.get("title", "—")),
            depends,
            str(t.get("model") or "—"),
            str(t.get("effort") or "—"),
        )

    console.print(table)


def _find_agent_logs(runtime_dir: Path, agent_id: str | None) -> list[Path]:
    """Return agent log files from runtime_dir sorted by mtime, optionally filtered by agent_id."""
    if not runtime_dir.exists():
        return []
    log_list = [p for p in runtime_dir.glob("*.log") if p.name != "watchdog.log"]
    if agent_id:
        log_list = [p for p in log_list if agent_id in p.stem]
    return sorted(log_list, key=lambda p: p.stat().st_mtime)


@click.command("logs")
@click.option("--follow", "-f", is_flag=True, default=False, help="Stream log output in real-time (like tail -f).")
@click.option("--agent", "-a", default=None, help="Filter by agent session ID (partial match).")
@click.option("--lines", "-n", default=50, show_default=True, help="Number of lines to show without --follow.")
@click.option(
    "--runtime-dir",
    default=".sdd/runtime",
    show_default=True,
    hidden=True,
    help="Directory containing agent log files.",
)
def logs_cmd(follow: bool, agent: str | None, lines: int, runtime_dir: str) -> None:
    """Tail agent log output.

    Without --follow, prints the last N lines of the most recent agent log.
    With --follow (-f), streams new output in real-time until Ctrl+C.
    """
    rdir = Path(runtime_dir)
    log_files = _find_agent_logs(rdir, agent)

    if not log_files:
        suffix = f" matching '{agent}'" if agent else ""
        console.print(f"[yellow]No agent logs found{suffix} in {rdir}[/yellow]")
        raise SystemExit(1)

    log_path = log_files[-1]  # most recent
    console.print(f"[dim]Watching:[/dim] [bold]{log_path.name}[/bold]")

    if not follow:
        text = log_path.read_text(errors="replace")
        tail_lines = text.splitlines()[-lines:]
        console.print("\n".join(tail_lines) or "[dim](empty)[/dim]")
        return

    # --follow: print last N lines as context then stream new bytes
    try:
        existing = log_path.read_text(errors="replace")
        context = existing.splitlines()[-lines:]
        if context:
            console.print("\n".join(context))
        offset = log_path.stat().st_size
    except FileNotFoundError:
        offset = 0

    console.print("[dim]--- following (Ctrl+C to stop) ---[/dim]")
    try:
        while True:
            try:
                size = log_path.stat().st_size
            except FileNotFoundError:
                time.sleep(0.2)
                continue

            if size > offset:
                with log_path.open("rb") as fh:
                    fh.seek(offset)
                    new_bytes = fh.read(size - offset)
                offset = size
                console.print(new_bytes.decode(errors="replace"), end="")

            time.sleep(0.2)
    except KeyboardInterrupt:
        console.print("\n[dim]stopped.[/dim]")


@click.command("notes", hidden=True)
@click.option("--lines", "-n", default=40, show_default=True, help="Number of tail lines to display.")
@click.option("--component", type=click.Choice(["server", "spawner"]), default="server", show_default=True)
def _notes_legacy(lines: int, component: str) -> None:  # type: ignore[reportUnusedFunction]
    """Tail server or spawner logs (legacy alias)."""
    log_path = Path(f".sdd/runtime/{component}.log")
    if not log_path.exists():
        console.print(f"[red]Log file not found:[/red] {log_path}")
        raise SystemExit(1)

    from rich.panel import Panel

    all_lines = log_path.read_text(errors="replace").splitlines()
    tail = all_lines[-lines:]
    console.print(
        Panel(
            "\n".join(tail) or "[dim](empty)[/dim]",
            title=f"[bold]{component}.log[/bold] (last {lines} lines)",
            border_style="dim",
        )
    )


@click.command("parts", hidden=True)
@click.option(
    "--status-filter",
    "status_filter",
    default=None,
    type=click.Choice(["open", "claimed", "in_progress", "done", "failed", "blocked"]),
    help="Filter by task status.",
)
@click.option("--role", default=None, help="Filter by role.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output raw JSON.")
def list_tasks(status_filter: str | None, role: str | None, as_json: bool) -> None:
    """List tasks with optional filters."""
    data = server_get("/status")
    if data is None:
        from bernstein.cli.errors import server_unreachable

        server_unreachable().print()
        raise SystemExit(1)

    tasks: list[dict[str, Any]] = data.get("tasks", [])

    if status_filter:
        tasks = [t for t in tasks if t.get("status") == status_filter]
    if role:
        tasks = [t for t in tasks if t.get("role") == role]

    if as_json:
        console.print_json(json.dumps(tasks))
        return

    if not tasks:
        console.print("[dim]No tasks matching filters.[/dim]")
        return

    from rich.table import Table

    table = Table(show_lines=False, header_style="bold cyan")
    table.add_column("ID", style="dim", min_width=10)
    table.add_column("Title", min_width=30)
    table.add_column("Role", min_width=10)
    table.add_column("Status", min_width=14)
    table.add_column("Priority", justify="right")

    for t in tasks:
        raw_status = t.get("status", "open")
        color = STATUS_COLORS.get(raw_status, "white")
        table.add_row(
            t.get("id", "—"),
            t.get("title", "—"),
            t.get("role", "—"),
            f"[{color}]{raw_status}[/{color}]",
            str(t.get("priority", 2)),
        )
    console.print(table)
