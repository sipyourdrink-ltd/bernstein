"""Task lifecycle commands for Bernstein CLI.

This module contains all task-related commands:
  cancel, add_task, list_tasks, approve, reject, pending, review_cmd, sync, logs_cmd, plan

All commands are registered with the main CLI group in main.py.
"""

from __future__ import annotations

import json
import os
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


@click.group("backlog")
def backlog_group() -> None:
    """File-backed backlog primitives for external workers."""


@backlog_group.command("claim")
@click.option(
    "--backlog",
    "backlog_path",
    default=Path(".sdd/runtime/task-backlog.json"),
    show_default=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Path to the shared JSON backlog.",
)
@click.option("--role", default=None, help="Only claim tasks for this role.")
@click.option("--capability", default=None, help="Only claim tasks requiring this capability.")
@click.option("--project", default=None, help="Only claim tasks for this project namespace.")
@click.option("--done", "completed_ids", multiple=True, metavar="TASK_ID", help="Dependency already completed.")
@click.option("--max-attempts", default=None, type=int, help="Skip rows at or above this attempt count.")
@click.option(
    "--agent-id",
    default=None,
    envvar="BERNSTEIN_AGENT_ID",
    help="Claiming worker identity. Defaults to a process-scoped CLI id.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Print the claimed row as JSON.")
def claim_backlog(
    backlog_path: Path,
    role: str | None,
    capability: str | None,
    project: str | None,
    completed_ids: tuple[str, ...],
    max_attempts: int | None,
    agent_id: str | None,
    as_json: bool,
) -> None:
    """Atomically claim the next eligible row from a shared JSON backlog."""
    from bernstein.core.tasks.claim import ClaimFilter, claim_next_entry

    claimer_id = agent_id or f"cli-{os.getpid()}"
    claimed = claim_next_entry(
        backlog_path,
        claimer_id=claimer_id,
        filter=ClaimFilter(
            project=project,
            role=role,
            capability=capability,
            completed_ids=set(completed_ids),
            max_attempts=max_attempts,
        ),
    )

    if as_json or is_json():
        print_json(claimed.to_dict() if claimed is not None else None)
        return
    if claimed is None:
        console.print("[dim]No eligible task.[/dim]")
        return
    console.print(claimed.id)


@backlog_group.command("verify-claim")
@click.option(
    "--receipt",
    "receipt_path",
    default="-",
    metavar="PATH",
    help="Claim receipt JSON to verify ('-' reads stdin).",
)
@click.option(
    "--backlog",
    "backlog_path",
    default=Path(".sdd/runtime/task-backlog.json"),
    show_default=True,
    type=click.Path(dir_okay=False, path_type=Path),
    help="Path to the shared JSON backlog to reproject from.",
)
@click.option(
    "--audit-dir",
    "audit_dir",
    default=Path(".sdd/audit"),
    show_default=True,
    type=click.Path(file_okay=False, path_type=Path),
    help="Audit chain directory the claim event was mirrored into.",
)
def verify_claim(receipt_path: str, backlog_path: Path, audit_dir: Path) -> None:
    """Verify a claim receipt offline against the backlog and audit chain.

    Reprojects the backlog head from the on-disk backlog, checks the receipt
    signature, verifies the audit chain, and confirms a matching
    ``task.claim_receipt`` event sits at the receipt's embedded chain head -
    the same chain ``bernstein audit verify`` walks. Exits non-zero when the
    receipt does not verify.
    """
    import sys

    from bernstein.core.protocols.mcp.claim_receipt import ClaimReceipt, verify_claim_receipt
    from bernstein.core.security.audit_chain import AuditChainStore
    from bernstein.core.server.dashboard_tokens import resolve_dashboard_hmac_key
    from bernstein.core.tasks.claim import Backlog

    raw = sys.stdin.read() if receipt_path == "-" else Path(receipt_path).read_text(encoding="utf-8")
    receipt = ClaimReceipt.from_wire(json.loads(raw))
    rows = [entry.to_dict() for entry in Backlog.load(backlog_path).entries]
    chain = AuditChainStore(audit_dir, key=resolve_dashboard_hmac_key(audit_dir.parent))

    ok, reason = verify_claim_receipt(receipt, rows, chain)
    if is_json():
        print_json({"verified": ok, "reason": reason, "task_id": receipt.task_id, "granted": receipt.granted})
    elif ok:
        console.print(f"[green]OK[/green] claim receipt verifies (task_id={receipt.task_id or '<refused>'})")
    else:
        console.print(f"[red]FAIL[/red] {reason}")
    if not ok:
        raise SystemExit(1)


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
@click.option(
    "--criterion-profile",
    "criterion_profile",
    default=None,
    metavar="PRESET",
    help=(
        "Per-task criterion profile (issue #1346): a named preset such as "
        "'safety-first', 'speed-first', 'balanced', 'cost-first'.  Biases "
        "model selection and the per-task blast radius."
    ),
)
@click.option(
    "--mode",
    "mode",
    default=None,
    type=click.Choice(["verbose", "balanced", "terse", "fast", "smart", "deep"]),
    help=(
        "Response-style profile for the worker handling this task. Accepts a "
        "style name (verbose/balanced/terse) or a mode-profile name "
        "(fast/smart/deep); stamped as metadata['mode'], the top-priority "
        "input of the spawn-time style resolution."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Print the JSON payload that would be sent without actually calling the API.",
)
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
    criterion_profile: str | None,
    mode: str | None,
    dry_run: bool,
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
    if criterion_profile is not None:
        # Validate early so operator typos surface before the payload
        # leaves the CLI.  The server simply forwards metadata blindly.
        from bernstein.core.routing.criterion_profile import (
            CriterionProfileError,
            resolve,
        )

        try:
            resolve(criterion_profile)
        except CriterionProfileError as exc:
            raise click.UsageError(f"--criterion-profile {criterion_profile!r}: {exc}") from None
        payload.setdefault("metadata", {})["criterion_profile"] = criterion_profile

    if mode is not None:
        payload.setdefault("metadata", {})["mode"] = mode

    if dry_run:
        if is_json():
            print_json(payload)
        else:
            console.print("[yellow]Dry run - payload that would be sent:[/yellow]")
            console.print_json(json.dumps(payload, indent=2))
        return

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
            f"[green]Task added:[/green] [bold]{task_id}[/bold]: {title} ([dim]role={role}, priority={priority}[/dim])"
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
            console.print("[dim]Nothing to sync: backlog/open/ is empty.[/dim]")


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
@click.option(
    "--pipeline",
    "pipeline_path",
    default=None,
    type=click.Path(),
    help="Path to a review.yaml pipeline definition.",
)
@click.option(
    "--pr",
    "pr_number",
    default=None,
    type=int,
    help="GitHub PR number to review (requires --pipeline).",
)
@click.option(
    "--validate-only",
    is_flag=True,
    default=False,
    help="Validate --pipeline YAML and exit. No agents run.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print the resolved pipeline without spawning agents or hitting any LLM.",
)
def review_cmd(
    workdir: str,
    pipeline_path: str | None,
    pr_number: int | None,
    validate_only: bool,
    dry_run: bool,
) -> None:
    """Trigger a manager queue review or run a YAML review pipeline.

    Without ``--pipeline``: writes a flag file that the running orchestrator
    picks up on its next tick, prompting the manager agent to inspect the
    task queue and issue corrections.

    With ``--pipeline``: parses the pipeline YAML and (when ``--pr`` is
    provided) runs the multi-phase review against the PR, printing a
    verdict table.  ``--validate-only`` exits after schema validation;
    ``--dry-run`` prints the resolved pipeline without spawning any agent.

    \b
    Examples:
      bernstein review
      bernstein review --pipeline review.yaml --validate-only
      bernstein review --pipeline review.yaml --pr 42 --dry-run
      bernstein review --pipeline templates/review/default-3-phase.yaml --pr 42
    """
    if pipeline_path is not None or validate_only or dry_run or pr_number is not None:
        # Lazy import - keep the legacy fast path zero-cost.
        from bernstein.cli.commands.review_pipeline_cmd import run_review_pipeline_cli

        exit_code = run_review_pipeline_cli(
            pipeline_path=pipeline_path,
            pr_number=pr_number,
            validate_only=validate_only,
            dry_run=dry_run,
            workdir=workdir,
        )
        raise SystemExit(exit_code)

    flag = Path(workdir) / ".sdd" / "runtime" / "review_requested"
    flag.parent.mkdir(parents=True, exist_ok=True)
    flag.write_text("1")
    console.print(
        "[green]Review queued.[/green] The manager will inspect the task queue on the next orchestrator tick."
    )


# NOTE: ``bernstein approve``, ``bernstein reject`` and ``bernstein pending``
# moved to dedicated modules (``approve_cmd.py``, ``reject_cmd.py``,
# ``pending_cmd.py``) as part of #1110. They are re-exported below so any
# legacy ``from bernstein.cli.commands.task_cmd import approve`` import keeps
# working without forcing every caller to update.
from bernstein.cli.commands.approve_cmd import approve as approve  # noqa: E402
from bernstein.cli.commands.pending_cmd import pending as pending  # noqa: E402
from bernstein.cli.commands.reject_cmd import reject as reject  # noqa: E402


def _build_graph_maps(
    data: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, list[str]], list[dict[str, Any]]]:
    """Parse raw graph data into task_map, forward-adjacency, and edges."""
    nodes: list[dict[str, Any]] = data.get("nodes", [])
    edges: list[dict[str, Any]] = data.get("edges", [])
    task_map: dict[str, dict[str, Any]] = {n["id"]: n for n in nodes}
    forward: dict[str, list[str]] = {n["id"]: [] for n in nodes}
    for e in edges:
        src: str = e["from"]
        tgt: str = e["to"]
        if src in forward:
            forward[src].append(tgt)
    return task_map, forward, edges


def _print_critical_path(data: dict[str, Any]) -> None:
    """Print the critical path and bottleneck summary lines."""
    critical_path: list[str] = data.get("critical_path", [])
    if critical_path:
        console.print()
        cp_ids = " \u2192 ".join(tid[:8] for tid in critical_path)
        console.print(f"[bold yellow]Critical path:[/bold yellow] {cp_ids}")
        minutes: int = int(data.get("critical_path_minutes", 0))
        if minutes:
            console.print(f"[dim]Estimated duration: {minutes} min[/dim]")

    bottlenecks: list[str] = data.get("bottlenecks", [])
    if bottlenecks:
        console.print()
        console.print("[bold red]Bottlenecks:[/bold red] " + ", ".join(b[:8] for b in bottlenecks))


def _make_node_text(
    tid: str,
    task_map: dict[str, dict[str, Any]],
    critical_set: set[str],
) -> Any:
    """Build a Rich Text label for a graph node."""
    from rich.text import Text

    t = task_map.get(tid, {})
    short_id = tid[:8]
    title = str(t.get("title", "?"))
    status = str(t.get("status", "?"))
    status_color = STATUS_COLORS.get(status, "white")
    text = Text()
    style = "bold yellow" if tid in critical_set else ""
    text.append(f"[{short_id}] {title}", style=style)
    if tid in critical_set:
        text.append(" \u2605", style="bold yellow")
    text.append(f" ({status})", style=status_color)
    return text


def _build_tree_recursive(
    parent: Any,
    tid: str,
    forward: dict[str, list[str]],
    task_map: dict[str, dict[str, Any]],
    critical_set: set[str],
    visited: set[str],
) -> None:
    """Recursively add branches to the tree."""
    from rich.text import Text

    visited.add(tid)
    branch = parent.add(_make_node_text(tid, task_map, critical_set))
    for child in forward.get(tid, []):
        if child not in visited:
            _build_tree_recursive(branch, child, forward, task_map, critical_set, visited)
        else:
            branch.add(Text(f"  \u21b3 [{child[:8]}] (shown above)", style="dim italic"))


def _render_graph() -> None:
    """Render ASCII task dependency graph using rich.tree.Tree."""
    from rich.tree import Tree

    raw = server_get("/tasks/graph")
    if raw is None:
        from bernstein.cli.errors import server_unreachable

        server_unreachable().print()
        raise SystemExit(1)

    data: dict[str, Any] = raw if isinstance(raw, dict) else {}  # type: ignore[assignment]
    nodes: list[dict[str, Any]] = data.get("nodes", [])
    if not nodes:
        console.print("[dim]No tasks found.[/dim]")
        return

    task_map, forward, edges = _build_graph_maps(data)
    critical_set: set[str] = set(data.get("critical_path", []))

    has_incoming: set[str] = {e["to"] for e in edges}
    roots = [n["id"] for n in nodes if n["id"] not in has_incoming]

    tree = Tree("[bold cyan]Task Dependency Graph[/bold cyan]")
    visited: set[str] = set()

    for root_id in sorted(roots):
        _build_tree_recursive(tree, root_id, forward, task_map, critical_set, visited)

    edge_nodes: set[str] = {e["from"] for e in edges} | {e["to"] for e in edges}
    for n in nodes:
        if n["id"] not in visited and n["id"] not in edge_nodes:
            tree.add(_make_node_text(n["id"], task_map, critical_set))

    console.print(tree)
    _print_critical_path(data)


@click.group("plan", invoke_without_command=True)
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
@click.pass_context
def plan(ctx: click.Context, export_file: str | None, status_filter: str | None, show_graph: bool) -> None:
    """Show task backlog, or run plan subcommands (generate, validate...).

    \b
      bernstein plan                          # show all tasks
      bernstein plan --status open            # show only open tasks
      bernstein plan --export plan.json       # export full backlog to JSON
      bernstein plan --graph                  # show ASCII dependency graph
      bernstein plan generate "description"   # generate a YAML plan from description
    """
    if ctx.invoked_subcommand is not None:
        return

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
        depends = ", ".join(d[:8] for d in cast("list[str]", t.get("depends_on", []))) or "-"
        table.add_row(
            str(t.get("id", "-"))[:8],
            f"[{color}]{raw_status}[/{color}]",
            str(t.get("role", "-")),
            str(t.get("title", "-")),
            depends,
            str(t.get("model") or "-"),
            str(t.get("effort") or "-"),
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
            t.get("id", "-"),
            t.get("title", "-"),
            t.get("role", "-"),
            f"[{color}]{raw_status}[/{color}]",
            str(t.get("priority", 2)),
        )
    console.print(table)


# ---------------------------------------------------------------------------
# Durable suspend / resume (#2552)
# ---------------------------------------------------------------------------


@click.group("task")
def task_group() -> None:
    """Durable task lifecycle: suspend and resume long-running sessions.

    A task that must wait on a human (a mid-flight approval, an external
    review, a credential rotation, a dependency landing) can PARK: the park
    emits an attested receipt that releases the seat, sandbox, and budget
    envelope, and ``bernstein task resume`` restores from that receipt
    deterministically.
    """


def _suspension_context(workdir: str, task_id: str) -> tuple[Path, Any, Any]:
    """Return ``(sdd_dir, audit_chain, work_ledger)`` for a task park/resume."""
    from bernstein.core.persistence.work_ledger import WorkLedger, run_ledger_dir
    from bernstein.core.security.audit_chain import AuditChainStore
    from bernstein.core.tasks.checkpoint_retry import task_run_id

    sdd_dir = Path(workdir) / ".sdd"
    chain = AuditChainStore(sdd_dir / "audit")
    ledger = WorkLedger.open(run_ledger_dir(sdd_dir, task_run_id(task_id)))
    return sdd_dir, chain, ledger


@task_group.command("suspend")
@click.argument("task_id")
@click.option("--workdir", default=".", help="Project root directory (parent of .sdd/).", type=click.Path())
@click.option("--adapter", default=None, help="Adapter owning the session (defaults to the latest checkpoint).")
@click.option("--session-id", default=None, help="Native session id (defaults to the latest checkpoint).")
@click.option("--worktree", default=None, help="Worktree to hash (defaults to the checkpoint worktree or cwd).")
@click.option("--envelope", default="subscription", show_default=True, help="Quota envelope whose headroom is freed.")
@click.option("--reserved-usd", default=0.0, type=float, help="Envelope headroom reserved for the task.")
@click.option("--spent-usd", default=0.0, type=float, help="Spend recorded against the reservation at park time.")
@click.option(
    "--until",
    "until",
    type=click.Choice(["approval"]),
    default=None,
    help="Compose with the approval sentinel: resume only when 'bernstein approve <task-id>' lands.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Print the park result as JSON.")
def task_suspend(
    task_id: str,
    workdir: str,
    adapter: str | None,
    session_id: str | None,
    worktree: str | None,
    envelope: str,
    reserved_usd: float,
    spent_usd: float,
    until: str | None,
    as_json: bool,
) -> None:
    """Durably park a running task, releasing its seat, sandbox, and budget.

    The park appends a suspend row to the task journal, binds its hash into the
    HMAC audit chain *before* any release, then returns the seat, tears down the
    sandbox, reaps the process, and releases unspent envelope headroom -- each
    referencing the suspend receipt hash. The SUSPENDED state is persisted to
    the work ledger so the park survives an orchestrator restart.

    \b
    Example:
      bernstein task suspend T-abc123 --reserved-usd 10 --spent-usd 2.5
      bernstein task suspend T-abc123 --until approval
    """
    from bernstein.core.orchestration.approval_gate import write_pending_sentinel
    from bernstein.core.tasks.checkpoint_retry import latest_checkpoint
    from bernstein.core.tasks.models import ApprovalSpec
    from bernstein.core.tasks.suspension import (
        WAKE_APPROVAL,
        UnsafeTaskIdError,
        park_task,
        validate_task_id,
    )

    # The park derives filesystem names (the journal run dir, and the approval
    # sentinel under --until approval) from the task id, so it is checked as an
    # identifier before anything is written.
    try:
        validate_task_id(task_id)
    except UnsafeTaskIdError as exc:
        console.print(f"[red]Refusing to park:[/red] {exc}")
        raise SystemExit(1) from exc

    sdd_dir, chain, ledger = _suspension_context(workdir, task_id)

    checkpoint = latest_checkpoint(sdd_dir, task_id)
    if adapter is None:
        adapter = checkpoint.adapter if checkpoint else ""
    if session_id is None:
        session_id = checkpoint.session_id if checkpoint else ""
    if worktree is None:
        worktree = checkpoint.worktree_path if checkpoint and checkpoint.worktree_path else workdir

    wake_condition = WAKE_APPROVAL if until == "approval" else ""

    result = park_task(
        sdd_dir=sdd_dir,
        task_id=task_id,
        adapter=adapter,
        session_id=session_id,
        worktree_path=Path(worktree),
        envelope=envelope,
        reserved_usd=reserved_usd,
        spent_usd=spent_usd,
        chain=chain,
        ledger=ledger,
        wake_condition=wake_condition,
    )

    if wake_condition == WAKE_APPROVAL:
        write_pending_sentinel(
            Path(workdir),
            task_id,
            ApprovalSpec(prompt=f"Resume parked task {task_id}?"),
        )

    released = {resource: detail for resource, detail in result.release.rows}
    payload: dict[str, Any] = {
        "task_id": task_id,
        "status": "suspended",
        "suspend_event_hash": result.suspend_row.event_hash,
        "suspend_receipt_hash": result.suspend_receipt_hash,
        "workspace_hash": result.suspend_row.workspace_hash,
        "released_usd": result.release.released_usd,
        "released_resources": sorted(released),
        "wake_condition": wake_condition,
        "ledger_entry_hash": result.ledger_entry_hash,
    }
    if as_json:
        print_json(payload)
        return
    console.print(f"[green]Suspended:[/green] task [bold]{task_id}[/bold]")
    console.print(f"[dim]parked-at:[/dim] {result.suspend_row.event_hash[:16]}")
    console.print(f"[dim]receipt:[/dim] {result.suspend_receipt_hash[:16]}")
    console.print(f"[dim]released:[/dim] {', '.join(sorted(released))} (${result.release.released_usd:.4f} headroom)")
    if wake_condition == WAKE_APPROVAL:
        console.print(f"[cyan]Wake gate:[/cyan] run 'bernstein approve {task_id}' to resume.")


@task_group.command("resume")
@click.argument("task_id")
@click.option("--workdir", default=".", help="Project root directory (parent of .sdd/).", type=click.Path())
@click.option("--worktree", default=None, help="Re-materialized worktree to hash (defaults to the parked path).")
@click.option(
    "--mode",
    type=click.Choice(["warm", "fork", "cold"]),
    default="warm",
    show_default=True,
    help="Requested continuation mode; downgraded (never upgraded) on drift.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Print the resume result as JSON.")
def task_resume(task_id: str, workdir: str, worktree: str | None, mode: str, as_json: bool) -> None:
    """Durably resume a parked task from its attested suspend receipt.

    Reads the latest verified suspend row, re-derives the continuation mode
    deterministically (warm when the workspace hash still matches, otherwise a
    recorded fork/cold downgrade), and records the resume receipt binding the
    suspend receipt it continued from. When the park was '--until approval' the
    resume refuses until 'bernstein approve <task-id>' has landed its decision.

    \b
    Example:
      bernstein task resume T-abc123
    """
    from bernstein.core.tasks.suspension import (
        WAKE_APPROVAL,
        ReleaseWithoutReceiptError,
        ResumeApprovalRequiredError,
        SuspendReceiptMismatchError,
        SuspensionAlreadySettledError,
        UnsafeTaskIdError,
        approval_decision_ref,
        find_suspension_receipt,
        latest_suspension,
        resume_task,
        validate_task_id,
        write_resume_marker,
    )

    try:
        validate_task_id(task_id)
    except UnsafeTaskIdError as exc:
        console.print(f"[red]Refusing to resume:[/red] {exc}")
        raise SystemExit(1) from exc

    sdd_dir, chain, ledger = _suspension_context(workdir, task_id)

    suspend_row = latest_suspension(sdd_dir, task_id)
    if suspend_row is None:
        console.print(f"[red]No verified suspension found for task[/red] [bold]{task_id}[/bold].")
        raise SystemExit(1)

    approval_ref = ""
    if suspend_row.wake_condition == WAKE_APPROVAL:
        approval_ref = approval_decision_ref(Path(workdir), task_id)
        if not approval_ref:
            console.print(f"[yellow]Parked until approval:[/yellow] run 'bernstein approve {task_id}' before resuming.")
            raise SystemExit(1)

    # The suspend receipt is selected by identity, not by recency: it must be
    # the receipt that binds *this* suspend row's hash and journal index. A task
    # parked more than once therefore never resumes against another park's
    # receipt, and a substituted receipt has nothing to match.
    receipt = find_suspension_receipt(chain=chain, task_id=task_id, suspend_row=suspend_row)
    if receipt is None:
        console.print(
            f"[red]No suspend receipt binds the parked row for task[/red] [bold]{task_id}[/bold] "
            f"([dim]parked-at {suspend_row.event_hash[:16]}[/dim])."
        )
        raise SystemExit(1)

    new_worktree = Path(worktree) if worktree else Path(suspend_row.worktree_path or workdir)
    try:
        result = resume_task(
            sdd_dir=sdd_dir,
            suspend_row=suspend_row,
            new_worktree_path=new_worktree,
            chain=chain,
            suspend_receipt_hash=receipt.hmac,
            requested_mode=mode,
            ledger=ledger,
            approval_ref=approval_ref,
        )
    except (
        ReleaseWithoutReceiptError,
        ResumeApprovalRequiredError,
        SuspendReceiptMismatchError,
        SuspensionAlreadySettledError,
        UnsafeTaskIdError,
    ) as exc:
        console.print(f"[red]Refusing to resume:[/red] {exc}")
        raise SystemExit(1) from exc
    if approval_ref:
        write_resume_marker(Path(workdir), task_id, result.resume_receipt_hash)

    payload: dict[str, Any] = {
        "task_id": task_id,
        "status": "resumed",
        "effective_mode": str(result.decision.effective_mode),
        "requested_mode": str(result.decision.requested_mode),
        "workspace_match": result.decision.workspace_match,
        "downgrade_reason": result.decision.downgrade_reason,
        "decision_hash": result.decision.decision_hash,
        "continued_from": suspend_row.event_hash,
        "resume_receipt_hash": result.resume_receipt_hash,
        "approval_ref": approval_ref,
    }
    if as_json:
        print_json(payload)
        return
    mode_str = str(result.decision.effective_mode)
    console.print(f"[green]Resumed:[/green] task [bold]{task_id}[/bold] ([bold]{mode_str}[/bold])")
    if result.decision.downgrade_reason:
        console.print(f"[yellow]downgrade:[/yellow] {result.decision.downgrade_reason}")
    console.print(f"[dim]continued-from:[/dim] {suspend_row.event_hash[:16]}")
    console.print(f"[dim]decision:[/dim] {result.decision.decision_hash[:16]}")


@task_group.command("list-suspended")
@click.option("--workdir", default=".", help="Project root directory (parent of .sdd/).", type=click.Path())
@click.option("--json", "as_json", is_flag=True, default=False, help="Print the parked tasks as JSON.")
def task_list_suspended(workdir: str, as_json: bool) -> None:
    """List durably-parked tasks with their parked-at hash and freed resources.

    Reads every per-task work ledger under ``.sdd/runtime/ledger`` and surfaces
    the tasks whose latest transition is ``task.suspended``, joined with the
    released headroom and the parked-at journal hash from the suspend row.
    """
    from bernstein.core.persistence.work_ledger import (
        LedgerReader,
        default_ledger_root,
        replay_state,
    )
    from bernstein.core.tasks.suspension import latest_suspension

    sdd_dir = Path(workdir) / ".sdd"
    ledger_root = default_ledger_root(sdd_dir)

    parked: list[dict[str, Any]] = []
    for run_dir in sorted(p for p in ledger_root.iterdir() if p.is_dir()) if ledger_root.exists() else []:
        reader = LedgerReader(run_dir)
        if not reader.exists():
            continue
        state = replay_state(reader.entries())
        for task_id in state.suspended_tasks:
            row = latest_suspension(sdd_dir, task_id)
            parked.append(
                {
                    "task_id": task_id,
                    "parked_at": row.event_hash if row else "",
                    "workspace_hash": row.workspace_hash if row else "",
                    "released_usd": row.released_usd if row else 0.0,
                    "wake_condition": row.wake_condition if row else "",
                }
            )

    if as_json:
        print_json({"suspended": parked})
        return
    if not parked:
        console.print("[dim]No suspended tasks.[/dim]")
        return

    from rich.table import Table

    table = Table(show_lines=False, header_style="bold cyan")
    table.add_column("Task", style="dim", min_width=10)
    table.add_column("Parked-at", min_width=18)
    table.add_column("Released $", justify="right")
    table.add_column("Wake", min_width=8)
    for entry in parked:
        wake = str(entry["wake_condition"]) or "operator"
        table.add_row(
            str(entry["task_id"]),
            str(entry["parked_at"])[:16],
            f"{float(entry['released_usd']):.4f}",
            wake,
        )
    console.print(table)
