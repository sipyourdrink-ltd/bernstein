"""``bernstein run-service`` - detached run supervisor + thin client (#2352).

The detached run service lets an operator submit a goal, drop the terminal,
and reattach from another shell later. The daemon owns execution; this command
group is the thin client over it. Every subcommand is a projection of, or an
append to, the durable work ledger and the HMAC audit chain -- the client
never rebuilds scheduler state, it replays the ledger.

* ``submit``  - open a run and (by default) spawn a detached supervisor.
* ``attach``  - prove ledger continuity across the detach boundary, then
  render the live projection.
* ``status``  - supervisor liveness plus the ledger projection.
* ``stop``    - stop the supervisor and record a detach boundary.
* ``verify``  - re-verify the audit chain, ledger chain, and every continuity
  boundary offline.

Off-host execution (the ``ssh`` sandbox backend and hosted sandbox backends)
is a documented follow-on; see docs/reference/KNOWN_LIMITATIONS.md.
"""

from __future__ import annotations

import json
from pathlib import Path

import click
from rich.panel import Panel
from rich.table import Table

from bernstein.cli.helpers import console
from bernstein.core.run_service import (
    RunService,
    RunServiceError,
    serve_run,
    spawn_detached,
    stop_supervisor,
    supervisor_status,
    verify_run,
)
from bernstein.core.run_service.paths import list_run_ids

# Exit codes shared across the group so operators (and the dashboard) can
# branch on the specific failure mode.
EXIT_OK = 0
EXIT_NO_RUN = 1
EXIT_VERIFY_FAILED = 2
EXIT_CONTINUITY_BROKEN = 3

_WORKDIR_OPTION = click.option(
    "--workdir",
    default=None,
    type=click.Path(file_okay=False, path_type=Path),
    help="Project root (defaults to current directory).",
)
_JSON_OPTION = click.option(
    "--json",
    "output_json",
    is_flag=True,
    default=False,
    help="Emit machine-readable JSON instead of the Rich summary.",
)


def _root(workdir: Path | None) -> Path:
    return (workdir or Path.cwd()).resolve()


@click.group("run-service")
def run_service_group() -> None:
    """Detached run service: submit, attach, status, stop, verify."""


# ---------------------------------------------------------------------------
# submit
# ---------------------------------------------------------------------------


@run_service_group.command("submit")
@click.argument("goal")
@click.option("--task", "tasks", multiple=True, help="Task id to schedule (repeatable).")
@_WORKDIR_OPTION
@_JSON_OPTION
@click.option(
    "--foreground",
    is_flag=True,
    default=False,
    help="Advance the run in this process instead of spawning a detached supervisor.",
)
@click.option(
    "--per-task-delay",
    type=float,
    default=0.0,
    show_default=True,
    help="Seconds to dwell per task (makes off-terminal progress observable).",
)
def submit_cmd(
    goal: str,
    tasks: tuple[str, ...],
    workdir: Path | None,
    output_json: bool,
    foreground: bool,
    per_task_delay: float,
) -> None:
    """Open a run for GOAL and start advancing it.

    Provide the decomposed task graph with one or more ``--task`` ids. Without
    ``--foreground`` a detached supervisor is spawned that survives this
    terminal; reattach later with ``bernstein run-service attach <run-id>``.
    """
    task_ids = list(tasks)
    if not task_ids:
        console.print("[red]Provide at least one --task id to schedule.[/red]")
        raise SystemExit(EXIT_NO_RUN)

    root = _root(workdir)
    svc = RunService(root)
    handle = svc.submit(goal, task_ids)
    run_id = handle.run_id

    pid: int | None = None
    if foreground:
        serve_run(root, run_id, per_task_delay=per_task_delay)
    else:
        pid = spawn_detached(root, run_id, per_task_delay=per_task_delay)

    if output_json:
        console.print_json(
            json.dumps(
                {
                    "run_id": run_id,
                    "ledger_head": handle.ledger_head,
                    "task_count": len(task_ids),
                    "detached": not foreground,
                    "pid": pid,
                }
            )
        )
        return

    console.print(
        Panel(
            f"[bold]Run submitted[/bold] [cyan]{run_id}[/cyan] ({len(task_ids)} task(s))",
            border_style="green",
            expand=False,
        )
    )
    if foreground:
        console.print("[green]Ran in the foreground to completion.[/green]")
    else:
        console.print(f"[green]Detached supervisor started[/green] (pid {pid}).")
        console.print(f"[dim]Reattach from any shell: bernstein run-service attach {run_id}[/dim]")


# ---------------------------------------------------------------------------
# attach
# ---------------------------------------------------------------------------


@run_service_group.command("attach")
@click.argument("run_id")
@_WORKDIR_OPTION
@_JSON_OPTION
def attach_cmd(run_id: str, workdir: Path | None, output_json: bool) -> None:
    """Reattach to a run and prove continuity across the detach boundary.

    \b
    Exit codes:
        0  reattached; the current ledger extends the head last seen
        1  no such run on disk
        3  continuity broken (ledger diverged or failed to verify)
    """
    root = _root(workdir)
    svc = RunService(root)
    try:
        result = svc.attach(run_id)
    except RunServiceError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(EXIT_NO_RUN) from None

    proof = result.proof
    if output_json:
        console.print_json(
            json.dumps(
                {
                    **result.state.to_dict(),
                    "run_id": run_id,
                    "current_head": result.current_head,
                    "continuity": proof.to_dict(),
                }
            )
        )
    else:
        _render_attach(run_id, result)

    if not proof.ok:
        raise SystemExit(EXIT_CONTINUITY_BROKEN)


def _render_attach(run_id: str, result: object) -> None:
    proof = result.proof  # type: ignore[attr-defined]
    state = result.state  # type: ignore[attr-defined]
    colour = "green" if proof.ok else "red"
    verdict = "continuity verified" if proof.ok else f"CONTINUITY BROKEN: {proof.reason}"
    console.print()
    console.print(
        Panel(f"[bold]Attached to[/bold] [cyan]{run_id}[/cyan] -- {verdict}", border_style=colour, expand=False)
    )
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Key", style="dim", no_wrap=True, min_width=22)
    table.add_column("Value")
    table.add_row("Chain head", f"{proof.current_head[:32]}...")
    table.add_row("Entries since detach", str(proof.entries_added))
    table.add_row("Completed tasks", ", ".join(state.completed_tasks) or "[dim]<none>[/dim]")
    table.add_row("In-flight tasks", ", ".join(state.in_flight_tasks) or "[dim]<none>[/dim]")
    table.add_row("Scheduled tasks", ", ".join(state.scheduled_tasks) or "[dim]<none>[/dim]")
    console.print(table)
    console.print()


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@run_service_group.command("status")
@click.argument("run_id", required=False)
@_WORKDIR_OPTION
@_JSON_OPTION
def status_cmd(run_id: str | None, workdir: Path | None, output_json: bool) -> None:
    """Show supervisor liveness and the ledger projection.

    With no RUN_ID, list every run known to this project.
    """
    root = _root(workdir)
    svc = RunService(root)

    if run_id is None:
        rows = []
        for rid in list_run_ids(root / ".sdd"):
            state = svc.project(rid)
            sup = supervisor_status(root, rid)
            rows.append(
                {
                    "run_id": rid,
                    "running": sup.running,
                    "pid": sup.pid,
                    "completed": len(state.completed_tasks),
                    "in_flight": len(state.in_flight_tasks),
                    "scheduled": len(state.scheduled_tasks),
                    "run_closed": state.run_closed,
                }
            )
        if output_json:
            console.print_json(json.dumps({"runs": rows}))
            return
        if not rows:
            console.print("[dim]No detached runs in this project.[/dim]")
            return
        table = Table(title="Detached runs")
        for col in ("run_id", "running", "completed", "in_flight", "scheduled", "closed"):
            table.add_column(col)
        for row in rows:
            table.add_row(
                row["run_id"],
                "yes" if row["running"] else "no",
                str(row["completed"]),
                str(row["in_flight"]),
                str(row["scheduled"]),
                "yes" if row["run_closed"] else "no",
            )
        console.print(table)
        return

    try:
        state = svc.project(run_id)
    except RunServiceError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(EXIT_NO_RUN) from None
    sup = supervisor_status(root, run_id)
    if output_json:
        console.print_json(json.dumps({**state.to_dict(), "run_id": run_id, "running": sup.running, "pid": sup.pid}))
        return
    console.print(f"[bold]{run_id}[/bold]: supervisor {'running' if sup.running else 'stopped'} (pid {sup.pid})")
    console.print(
        f"  completed={state.completed_tasks} in_flight={state.in_flight_tasks} scheduled={state.scheduled_tasks}"
    )
    console.print(f"  closed={state.run_closed} head={state.head_hash[:16]}...")


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------


@run_service_group.command("stop")
@click.argument("run_id")
@_WORKDIR_OPTION
@_JSON_OPTION
def stop_cmd(run_id: str, workdir: Path | None, output_json: bool) -> None:
    """Stop the run's supervisor and record a detach boundary.

    \b
    Exit codes:
        0  stopped (or already stopped)
        1  no such run on disk
    """
    root = _root(workdir)
    svc = RunService(root)
    try:
        svc.detach(run_id)
    except RunServiceError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(EXIT_NO_RUN) from None
    stopped = stop_supervisor(root, run_id)
    if output_json:
        console.print_json(json.dumps({"run_id": run_id, "stopped": stopped}))
        return
    if stopped:
        console.print(f"[green]Supervisor stopped[/green] for run {run_id}; detach boundary recorded.")
    else:
        console.print(f"[yellow]No running supervisor[/yellow] for run {run_id}; detach boundary recorded.")


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


@run_service_group.command("verify")
@click.argument("run_id")
@_WORKDIR_OPTION
@_JSON_OPTION
def verify_cmd(run_id: str, workdir: Path | None, output_json: bool) -> None:
    """Re-verify the audit chain, ledger chain, and continuity boundaries.

    \b
    Exit codes:
        0  everything verifies offline
        2  a check failed (the exact reason is listed)
    """
    root = _root(workdir)
    report = verify_run(root, run_id)
    if output_json:
        console.print_json(json.dumps(report.to_dict()))
    else:
        colour = "green" if report.ok else "red"
        console.print(
            Panel(
                f"[bold]Run {run_id}[/bold] -- "
                f"audit={report.audit_ok} ledger={report.ledger_ok} continuity={report.continuity_ok}",
                border_style=colour,
                expand=False,
            )
        )
        for error in report.errors:
            console.print(f"  [red]-[/red] {error}")
    if not report.ok:
        raise SystemExit(EXIT_VERIFY_FAILED)


# ---------------------------------------------------------------------------
# _serve (hidden supervisor entrypoint)
# ---------------------------------------------------------------------------


@run_service_group.command("_serve", hidden=True)
@click.argument("run_id")
@_WORKDIR_OPTION
@click.option("--per-task-delay", type=float, default=0.0)
def serve_cmd(run_id: str, workdir: Path | None, per_task_delay: float) -> None:
    """Internal: the detached supervisor loop (spawned by ``submit``)."""
    root = _root(workdir)
    serve_run(root, run_id, per_task_delay=per_task_delay)


__all__ = [
    "EXIT_CONTINUITY_BROKEN",
    "EXIT_NO_RUN",
    "EXIT_OK",
    "EXIT_VERIFY_FAILED",
    "run_service_group",
]
