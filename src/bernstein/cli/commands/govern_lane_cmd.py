"""``bernstein govern lane``: bootstrap, list and show reconciliation lanes (#5120).

A lane is a first-class chained object, the same discipline
:mod:`bernstein.cli.commands.pool_cmd` already established for pools.
``bernstein govern lane bootstrap`` reads a declared lane-set document, diffs
it against the lanes already active in the HMAC audit chain, and appends a
``lane.registered`` or ``lane.updated`` event for every lane that changed --
running it twice against an unchanged set changes nothing and says so. The
runtime lane registry is a deterministic projection of those events (see
:mod:`bernstein.core.govern.lane_registry`), so there is no side database that
can drift out of agreement with the chain.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click
from rich.table import Table

from bernstein.cli.helpers import console
from bernstein.core.govern.lane_registry import LaneStore, LaneStoreError, project_lane_registry
from bernstein.core.govern.lanes import LaneError, load_lane_set, reconcile_lanes


def _audit_dir(workdir: Path) -> Path:
    return workdir / ".sdd" / "audit"


def _store(workdir: Path) -> LaneStore:
    return LaneStore(root=workdir / ".sdd" / "sandbox")


def _chain(workdir: Path) -> Any:
    from bernstein.core.security.audit_chain import AuditChainStore

    audit_dir = _audit_dir(workdir)
    audit_dir.mkdir(parents=True, exist_ok=True)
    return AuditChainStore(audit_dir)


def _lane_events(workdir: Path, *, key: bytes | None = None) -> list[Any]:
    audit_dir = _audit_dir(workdir)
    if not audit_dir.is_dir():
        return []
    from bernstein.core.security.audit_chain import AuditChainStore

    events = AuditChainStore(audit_dir, key=key).query()
    return [e for e in events if str(getattr(e, "event_type", "")).startswith("lane.")]


_WORKDIR_OPTION = click.option(
    "--workdir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path(),
    show_default=True,
    help="Project root containing .sdd/.",
)


@click.command("bootstrap")
@click.argument("spec_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@_WORKDIR_OPTION
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the reconcile report as JSON.")
def govern_lane_bootstrap_cmd(spec_file: Path, workdir: Path, as_json: bool) -> None:
    """Bootstrap the lane set declared in JSON SPEC_FILE against the chain.

    The spec is ``{"lanes": [...]}``, each lane without a hash: name, selector,
    schedule, log_destination, timeout_seconds, barrier. Every lane that is new
    or changed is appended to the audit chain as ``lane.registered`` /
    ``lane.updated``; a lane already exactly as declared is left alone. Running
    this against an unchanged set is a no-op, and the report says so.
    """
    workdir = workdir.resolve()
    try:
        spec = json.loads(spec_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        console.print(f"[red]Invalid lane spec JSON:[/red] {exc}")
        raise SystemExit(1) from exc
    try:
        declared = list(load_lane_set(spec))
    except LaneError as exc:
        console.print(f"[red]Invalid lane set:[/red] {exc}")
        raise SystemExit(1) from exc

    existing = project_lane_registry(_lane_events(workdir))
    try:
        result = reconcile_lanes(declared, existing)
    except LaneError as exc:
        console.print(f"[red]Invalid lane set:[/red] {exc}")
        raise SystemExit(1) from exc

    store = _store(workdir)
    chain = _chain(workdir)
    from bernstein.core.security.audit_chain import record_lane_registered, record_lane_updated

    for entry in result.changed:
        store.put(entry.lane)
        if entry.prev_hash:
            record_lane_updated(
                chain=chain,
                lane_name=entry.lane.name,
                lane_hash=entry.lane.lane_hash,
                prev_lane_hash=entry.prev_hash,
            )
        else:
            record_lane_registered(chain=chain, lane_name=entry.lane.name, lane_hash=entry.lane.lane_hash)

    if as_json:
        click.echo(json.dumps(result.to_dict(), indent=2, sort_keys=True))
        return
    if result.is_noop:
        console.print("[dim]Lane bootstrap is a no-op: every declared lane already matches the chain.[/dim]")
        return
    for entry in result.entries:
        console.print(
            f"[green]Lane {entry.action.value}:[/green] [bold]{entry.lane.name}[/bold]  {entry.lane.lane_hash[:16]}..."
        )


@click.command("list")
@_WORKDIR_OPTION
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the active lanes as JSON.")
def govern_lane_list_cmd(workdir: Path, as_json: bool) -> None:
    """List active reconciliation lanes projected from the audit chain."""
    workdir = workdir.resolve()
    active = project_lane_registry(_lane_events(workdir))
    if as_json:
        click.echo(json.dumps({"lanes": active}, indent=2, sort_keys=True))
        return
    if not active:
        console.print("[dim]No active lanes. Bootstrap one with 'bernstein govern lane bootstrap'.[/dim]")
        return
    table = Table(title="Active Reconciliation Lanes", header_style="bold cyan")
    table.add_column("Name", style="bold")
    table.add_column("Lane hash", style="dim")
    for name in sorted(active):
        table.add_row(name, active[name][:24] + "...")
    console.print(table)


@click.command("show")
@click.argument("name")
@_WORKDIR_OPTION
def govern_lane_show_cmd(name: str, workdir: Path) -> None:
    """Show the canonical manifest and hash for an active lane NAME."""
    workdir = workdir.resolve()
    lane_hash = project_lane_registry(_lane_events(workdir)).get(name)
    if lane_hash is None:
        console.print(f"[yellow]No active lane named[/yellow] {name!r}.")
        raise SystemExit(1)
    try:
        manifest = _store(workdir).get(lane_hash)
    except LaneStoreError as exc:
        console.print(f"[red]Cannot load lane body:[/red] {exc}")
        raise SystemExit(1) from exc
    click.echo(json.dumps(manifest.to_dict(), indent=2, sort_keys=True))


@click.group("lane")
def govern_lane_group() -> None:
    """Bootstrap and inspect reconciliation lanes (chain-projected)."""


govern_lane_group.add_command(govern_lane_bootstrap_cmd, "bootstrap")
govern_lane_group.add_command(govern_lane_list_cmd, "list")
govern_lane_group.add_command(govern_lane_show_cmd, "show")


__all__ = ["govern_lane_group"]
