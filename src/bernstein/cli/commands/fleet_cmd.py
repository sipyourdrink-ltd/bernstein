"""``bernstein fleet`` - supervisory dashboard across multiple projects.

This is the CLI entry point. The actual aggregator and rendering live in
:mod:`bernstein.core.fleet`; this module only wires Click subcommands to
those primitives.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from pathlib import Path
from typing import Any

import click
import httpx
from rich.console import Console
from rich.table import Table

from bernstein.core.fleet import (
    DirectoryRegistry,
    FleetAggregator,
    FleetConfig,
    bulk_cost_report,
    bulk_pause,
    bulk_resume,
    bulk_stop,
    default_fleet_root,
    default_projects_config_path,
    load_projects_config,
    select_projects,
)

logger = logging.getLogger(__name__)
_console = Console()


def _resolve_config(path: str | None) -> FleetConfig:
    target = Path(path).expanduser() if path else None
    return load_projects_config(target)


def _print_config_errors(config: FleetConfig) -> None:
    if not config.errors:
        return
    for err in config.errors:
        tag = "global" if err.index < 0 else f"project[{err.index}]"
        _console.print(f"[yellow]config {tag}:[/yellow] {err.message}")


@click.group("fleet", invoke_without_command=True)
@click.option(
    "--config",
    "config_path",
    default=None,
    help=f"Path to fleet config (default: {default_projects_config_path()}).",
)
@click.option(
    "--web",
    "web_bind",
    default=None,
    help="Run the web view instead of the TUI. Bind format: ``[host:]port``.",
)
@click.pass_context
def fleet_group(
    ctx: click.Context,
    config_path: str | None,
    web_bind: str | None,
) -> None:
    """Supervisory dashboard for multiple Bernstein projects."""
    ctx.ensure_object(dict)
    ctx.obj["config_path"] = config_path
    if ctx.invoked_subcommand is None:
        config = _resolve_config(config_path)
        if web_bind is not None:
            _run_web(config, web_bind)
            return
        _run_tui(config)


def _run_tui(config: FleetConfig) -> None:
    if not config.projects:
        _print_config_errors(config)
        _console.print(
            f"[red]No projects configured.[/red] Edit {default_projects_config_path()} and add [[project]] blocks."
        )
        sys.exit(2)

    async def _main() -> None:
        aggregator = FleetAggregator(config.projects)
        await aggregator.start()
        try:
            try:
                from bernstein.core.fleet.tui import build_textual_app
            except ImportError:
                _fallback_table_render(aggregator, config)
                return
            app = build_textual_app(aggregator, config)
            await app.run_async()
        finally:
            await aggregator.stop()

    asyncio.run(_main())


def _fallback_table_render(aggregator: FleetAggregator, config: FleetConfig) -> None:
    """Fallback Rich-based renderer when Textual is unavailable."""
    from bernstein.core.fleet.tui import build_rows, format_footer

    rows, total = build_rows(aggregator)
    table = Table(title="Bernstein fleet")
    for col in (
        "Project",
        "State",
        "Run",
        "Agents",
        "Approvals",
        "Last SHA",
        "Cost (7d)",
        "Sparkline",
        "Chain",
    ):
        table.add_column(col)
    for row in rows:
        table.add_row(
            row.name,
            row.state,
            row.run_state,
            str(row.agents),
            str(row.approvals),
            row.last_sha,
            f"${row.cost_usd:.2f}",
            row.sparkline,
            "ok" if row.chain_ok else "BROKEN",
        )
    _console.print(table)
    _console.print(format_footer(config, rows, total))

    supervisor_line = _fleet_supervisor_summary_line()
    if supervisor_line:
        _console.print(f"[dim]{supervisor_line}[/dim]")


def _fleet_supervisor_summary_line() -> str:
    """Return the stuck-count summary across the fleet's primary workspace.

    The fleet view aggregates many projects but a single operator sits
    inside one workspace, so we surface the supervisor snapshot for that
    workspace as the most actionable signal. Returns an empty string on
    any aggregator failure so the fleet command never errors here.
    Failures are logged so an operator-visible drop can be debugged from
    the orchestrator log without restarting the fleet view.
    """
    try:
        from pathlib import Path as _Path

        from bernstein.core.defaults import AGENT
        from bernstein.core.orchestration.supervisor_aggregator import (
            aggregator_snapshot,
            format_summary_line,
        )

        snapshot = aggregator_snapshot(_Path.cwd(), heartbeat_stale_s=AGENT.heartbeat_stale_s)
    except Exception:  # pragma: no cover - fleet renderer must never raise
        logger.exception("fleet supervisor-summary aggregation failed")
        return ""
    return format_summary_line(snapshot)


def _parse_bind(bind: str) -> tuple[str, int]:
    text = bind.strip()
    if text.startswith(":"):
        return "127.0.0.1", int(text[1:])
    if ":" in text:
        host, port = text.rsplit(":", 1)
        return host or "127.0.0.1", int(port)
    return "127.0.0.1", int(text)


def _run_web(config: FleetConfig, bind: str) -> None:
    if not config.projects:
        _print_config_errors(config)
        _console.print("[red]No projects configured.[/red] Add [[project]] blocks before launching --web.")
        sys.exit(2)
    host, port = _parse_bind(bind)

    try:
        import uvicorn
    except ImportError:
        _console.print("[red]uvicorn is required for fleet --web.[/red]")
        sys.exit(2)

    from bernstein.core.fleet.web import build_fleet_app

    async def _bootstrap() -> tuple[FleetAggregator, Any]:
        aggregator = FleetAggregator(config.projects)
        await aggregator.start()
        return aggregator, build_fleet_app(aggregator, config)

    aggregator, app = asyncio.run(_bootstrap())
    _console.print(f"[green]Bernstein fleet web[/green] listening on http://{host}:{port}")
    _print_config_errors(config)
    try:
        uvicorn.run(app, host=host, port=port, log_level="warning")
    finally:
        asyncio.run(aggregator.stop())


# ---------------------------------------------------------------------------
# Bulk subcommands
# ---------------------------------------------------------------------------


def _bulk_target(
    config: FleetConfig,
    names: tuple[str, ...] | None,
    filter_expression: str | None,
) -> list[Any]:
    """Resolve a target list using the *static* config snapshot.

    Unlike the TUI, the CLI bulk path doesn't need the live aggregator
    - projects are filtered by their on-disk cost history when a filter
    references ``cost``.
    """
    from bernstein.core.fleet.aggregator import ProjectSnapshot, ProjectState
    from bernstein.core.fleet.cost_rollup import rollup_costs

    rollup = rollup_costs({p.name: p.sdd_dir for p in config.projects}, window_days=7)
    snapshots = [
        ProjectSnapshot(
            name=p.name,
            state=ProjectState.ONLINE,
            cost_usd=float(rollup.per_project.get(p.name, {}).get("total_usd") or 0.0),
        )
        for p in config.projects
    ]
    return select_projects(
        config.projects,
        snapshots,
        names=list(names) if names else None,
        filter_expression=filter_expression,
    )


def _print_bulk_result(result: Any) -> None:
    payload: dict[str, Any] = {
        "action": result.action,
        "succeeded": list(result.succeeded),
        "failed": dict(result.failed),
    }
    _console.print_json(json.dumps(payload))


@fleet_group.command("bulk-stop")
@click.option("--names", multiple=True, help="Restrict to listed project names.")
@click.option("--filter", "filter_expression", default=None, help="Filter expression e.g. cost>5.")
@click.pass_context
def bulk_stop_cmd(
    ctx: click.Context,
    names: tuple[str, ...],
    filter_expression: str | None,
) -> None:
    """Stop every matching project via its CLI."""
    config = _resolve_config(ctx.obj.get("config_path"))
    targets = _bulk_target(config, names, filter_expression)
    result = asyncio.run(bulk_stop(targets))
    _print_bulk_result(result)


@fleet_group.command("bulk-pause")
@click.option("--names", multiple=True, help="Restrict to listed project names.")
@click.option("--filter", "filter_expression", default=None, help="Filter expression.")
@click.pass_context
def bulk_pause_cmd(
    ctx: click.Context,
    names: tuple[str, ...],
    filter_expression: str | None,
) -> None:
    """Pause every matching project (stops its daemon)."""
    config = _resolve_config(ctx.obj.get("config_path"))
    targets = _bulk_target(config, names, filter_expression)
    result = asyncio.run(bulk_pause(targets))
    _print_bulk_result(result)


@fleet_group.command("bulk-resume")
@click.option("--names", multiple=True, help="Restrict to listed project names.")
@click.option("--filter", "filter_expression", default=None, help="Filter expression.")
@click.pass_context
def bulk_resume_cmd(
    ctx: click.Context,
    names: tuple[str, ...],
    filter_expression: str | None,
) -> None:
    """Resume every matching project (restarts its daemon)."""
    config = _resolve_config(ctx.obj.get("config_path"))
    targets = _bulk_target(config, names, filter_expression)
    result = asyncio.run(bulk_resume(targets))
    _print_bulk_result(result)


@fleet_group.command("bulk-cost-report")
@click.option("--names", multiple=True, help="Restrict to listed project names.")
@click.option("--filter", "filter_expression", default=None, help="Filter expression.")
@click.pass_context
def bulk_cost_report_cmd(
    ctx: click.Context,
    names: tuple[str, ...],
    filter_expression: str | None,
) -> None:
    """Run ``bernstein cost report`` against every matching project."""
    config = _resolve_config(ctx.obj.get("config_path"))
    targets = _bulk_target(config, names, filter_expression)
    result = asyncio.run(bulk_cost_report(targets))
    _print_bulk_result(result)


@fleet_group.command("ls")
@click.pass_context
def ls_cmd(ctx: click.Context) -> None:
    """List configured projects without launching the dashboard."""
    config = _resolve_config(ctx.obj.get("config_path"))
    table = Table(title="Bernstein fleet - configured projects")
    table.add_column("Name")
    table.add_column("Path")
    table.add_column("Task server")
    for project in config.projects:
        table.add_row(project.name, str(project.path), project.task_server_url)
    _console.print(table)
    _print_config_errors(config)


# ---------------------------------------------------------------------------
# Receipt-backed steering of a single running worker (#2508)
# ---------------------------------------------------------------------------


def _post_steer(server_url: str, token: str | None, task_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST a steering command to a task server and return the receipt."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    resp = httpx.post(f"{server_url}/tasks/{task_id}/steer", json=payload, timeout=10.0, headers=headers)
    resp.raise_for_status()
    return resp.json()  # type: ignore[no-any-return]


@fleet_group.command("steer")
@click.argument("task_id")
@click.argument("kind", type=click.Choice(["pause", "resume", "guidance", "redirect", "abort"]))
@click.option("--guidance", default="", help="Guidance text (guidance kind).")
@click.option("--redirect-target", "redirect_target", default="", help="New objective (redirect kind).")
@click.option("--reason", default="", help="Human-readable reason (pause / abort).")
@click.option("--session-id", "session_id", default="", help="Target worker session (pause / abort).")
@click.option("--adapter", default="", help="Adapter owning the session (pause checkpoint).")
@click.option("--worktree", default="", help="Worktree path (pause checkpoint baseline).")
@click.option("--principal", default="", help="Declared operator seat (loopback attribution only).")
@click.option("--server", "server_url", default=None, help="Task server URL (default $BERNSTEIN_SERVER_URL).")
@click.option("--token", default=None, help="Operator scoped token (default $BERNSTEIN_AUTH_TOKEN).")
@click.option("--json", "as_json", is_flag=True, help="Emit the raw receipt JSON.")
def steer_cmd(
    task_id: str,
    kind: str,
    guidance: str,
    redirect_target: str,
    reason: str,
    session_id: str,
    adapter: str,
    worktree: str,
    principal: str,
    server_url: str | None,
    token: str | None,
    as_json: bool,
) -> None:
    """Steer one running worker: pause, resume, guidance, redirect, or abort.

    The action is a receipt first and an effect second. The confirmed
    payload hash is computed locally and sent to the server, which binds it
    into the audit chain before any effect runs and returns the receipt the
    delivered effect references. A mismatch between the locally shown payload
    and the executed command is rejected by the server.
    """
    from bernstein.cli.helpers import SERVER_URL
    from bernstein.core.orchestration.steering import InvalidSteeringCommand, SteeringCommand

    server = server_url or SERVER_URL
    auth = token or os.environ.get("BERNSTEIN_AUTH_TOKEN")
    command = SteeringCommand(
        kind=kind,
        task_id=task_id,
        principal=principal or "cli-operator",
        guidance=guidance,
        redirect_target=redirect_target,
        reason=reason,
        session_id=session_id,
        adapter=adapter,
        worktree=worktree,
    )
    try:
        command.validate()
    except InvalidSteeringCommand as exc:
        raise click.ClickException(str(exc)) from None

    payload = {
        "kind": kind,
        "principal": principal,
        "guidance": guidance,
        "redirect_target": redirect_target,
        "reason": reason,
        "session_id": session_id,
        "adapter": adapter,
        "worktree": worktree,
        "displayed_payload_hash": command.payload_hash(),
    }
    try:
        receipt = _post_steer(server, auth, task_id, payload)
    except httpx.HTTPStatusError as exc:
        raise click.ClickException(f"steer rejected ({exc.response.status_code}): {exc.response.text}") from None
    except httpx.HTTPError as exc:
        raise click.ClickException(f"task server unreachable: {exc}") from None

    if as_json:
        _console.print_json(json.dumps(receipt))
        return
    _console.print(
        f"[green]Steered[/green] task {task_id} ({kind}); "
        f"receipt {str(receipt.get('receipt_hash', ''))[:24]} at mailbox seq {receipt.get('mailbox_seq')}"
    )


# ---------------------------------------------------------------------------
# Directory registry subcommands
# ---------------------------------------------------------------------------


@fleet_group.command("list")
@click.option(
    "--root",
    "root_path",
    default=None,
    help=f"Override fleet root (default: {default_fleet_root()}).",
)
@click.pass_context
def list_cmd(ctx: click.Context, root_path: str | None) -> None:
    """List instances discovered under the fleet root directory.

    Scans ``$BERNSTEIN_FLEET_ROOT`` (or ``--root``) for subdirectories
    containing a ``bernstein.yaml`` manifest. Each enabled instance is
    rendered; disabled instances (with a ``.disabled`` flag file) are
    summarised at the end.
    """
    root = Path(root_path).expanduser() if root_path else None
    registry = DirectoryRegistry(root)
    scan = registry.scan()
    table = Table(title=f"Bernstein fleet - instances under {registry.root}")
    table.add_column("Name")
    table.add_column("Directory")
    table.add_column("Project path")
    table.add_column("Task server")
    for spec in scan.instances:
        table.add_row(
            spec.name,
            str(spec.directory),
            str(spec.project_path),
            spec.task_server_url,
        )
    _console.print(table)
    if scan.disabled:
        names = ", ".join(s.name for s in scan.disabled)
        _console.print(f"[yellow]disabled (.disabled flag):[/yellow] {names}")
    for err in scan.errors:
        tag = "root" if err.index < 0 else f"instance[{err.index}]"
        _console.print(f"[yellow]scan {tag}:[/yellow] {err.message}")


@fleet_group.command("reload")
@click.option(
    "--root",
    "root_path",
    default=None,
    help=f"Override fleet root (default: {default_fleet_root()}).",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    help="Emit a machine-readable JSON summary instead of a table.",
)
@click.pass_context
def reload_cmd(ctx: click.Context, root_path: str | None, as_json: bool) -> None:
    """Rescan the fleet root and report what would be picked up.

    The supervisor consumes the same :class:`DirectoryRegistry` API on
    its own reload signal; this subcommand simply exposes the result to
    operators so they can verify a new instance directory before the
    supervisor picks it up.
    """
    root = Path(root_path).expanduser() if root_path else None
    registry = DirectoryRegistry(root)
    scan = registry.scan()
    if as_json:
        payload = {
            "root": str(registry.root),
            "instances": [
                {
                    "name": s.name,
                    "directory": str(s.directory),
                    "project_path": str(s.project_path),
                    "task_server_url": s.task_server_url,
                }
                for s in scan.instances
            ],
            "disabled": [s.name for s in scan.disabled],
            "errors": [{"index": e.index, "message": e.message} for e in scan.errors],
        }
        _console.print_json(json.dumps(payload))
        return
    _console.print(
        f"[green]Rescanned[/green] {registry.root}: "
        f"{len(scan.instances)} active, {len(scan.disabled)} disabled, "
        f"{len(scan.errors)} error(s)."
    )
    for spec in scan.instances:
        _console.print(f"  - {spec.name} -> {spec.task_server_url}")
    for spec in scan.disabled:
        _console.print(f"  - {spec.name} (disabled)")
    for err in scan.errors:
        tag = "root" if err.index < 0 else f"instance[{err.index}]"
        _console.print(f"[yellow]scan {tag}:[/yellow] {err.message}")
