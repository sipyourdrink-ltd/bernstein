"""Named sandbox pool commands: register, list, show, verify (#2547).

A pool is a first-class chained object. ``bernstein pool register`` appends a
``pool.registered`` (or ``pool.updated``) event to the HMAC audit chain and
writes the manifest body to a content-addressed store; the runtime registry is
a deterministic projection rebuilt by replaying those events, so there is no
side database to drift. ``bernstein pool verify`` re-derives the projection,
re-hashes every stored body, and re-checks every placement and enrolment
receipt, so the whole pool subsystem is offline-verifiable.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import click
from rich.table import Table

from bernstein.cli.helpers import console
from bernstein.core.sandbox.pool import PoolManifest, PoolManifestError
from bernstein.core.sandbox.pool_registry import (
    PoolStore,
    PoolStoreError,
    project_pool_registry,
)


def _audit_dir(workdir: Path) -> Path:
    return workdir / ".sdd" / "audit"


def _store(workdir: Path) -> PoolStore:
    return PoolStore(root=workdir / ".sdd" / "sandbox")


def _chain(workdir: Path) -> Any:
    from bernstein.core.security.audit_chain import AuditChainStore

    audit_dir = _audit_dir(workdir)
    audit_dir.mkdir(parents=True, exist_ok=True)
    return AuditChainStore(audit_dir)


def _pool_events(workdir: Path) -> list[Any]:
    audit_dir = _audit_dir(workdir)
    if not audit_dir.is_dir():
        return []
    from bernstein.core.security.audit_chain import AuditChainStore

    events = AuditChainStore(audit_dir).query()
    return [e for e in events if str(getattr(e, "event_type", "")).startswith("pool.")]


@click.group("pool")
def pool_group() -> None:
    """Define and govern named sandbox pools (chain-projected)."""


@pool_group.command("register")
@click.argument("spec_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--workdir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path(),
    show_default=True,
    help="Project root containing .sdd/.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the registered pool as JSON.")
def register_cmd(spec_file: Path, workdir: Path, as_json: bool) -> None:
    """Register (or update) a pool from a JSON SPEC_FILE.

    The spec is the pool manifest without a hash: name, backend_allowlist,
    template, exposed_fields, capability_ceiling, network_egress_class,
    credential_env_allowlist, max_concurrency. The canonical hash is computed
    and the pool is appended to the audit chain.
    """
    workdir = workdir.resolve()
    try:
        spec = json.loads(spec_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        console.print(f"[red]Invalid pool spec JSON:[/red] {exc}")
        raise SystemExit(1) from exc
    spec.pop("pool_hash", None)
    try:
        manifest = PoolManifest.from_dict(spec)
    except (PoolManifestError, KeyError, ValueError) as exc:
        console.print(f"[red]Invalid pool manifest:[/red] {exc}")
        raise SystemExit(1) from exc

    store = _store(workdir)
    store.put(manifest)

    prev_hash = project_pool_registry(_pool_events(workdir)).get(manifest.name)
    chain = _chain(workdir)
    from bernstein.core.security.audit_chain import (
        record_pool_registered,
        record_pool_updated,
    )

    if prev_hash and prev_hash != manifest.pool_hash:
        record_pool_updated(
            chain=chain,
            pool_name=manifest.name,
            pool_hash=manifest.pool_hash,
            prev_pool_hash=prev_hash,
        )
        action = "updated"
    elif prev_hash == manifest.pool_hash:
        action = "unchanged"
    else:
        record_pool_registered(chain=chain, pool_name=manifest.name, pool_hash=manifest.pool_hash)
        action = "registered"

    if as_json:
        click.echo(json.dumps({"action": action, **manifest.to_dict()}, indent=2, sort_keys=True))
        return
    console.print(f"[green]Pool {action}:[/green] [bold]{manifest.name}[/bold]  {manifest.pool_hash[:16]}...")


@pool_group.command("list")
@click.option(
    "--workdir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path(),
    show_default=True,
    help="Project root containing .sdd/.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the active pools as JSON.")
def list_cmd(workdir: Path, as_json: bool) -> None:
    """List active pools projected from the audit chain."""
    workdir = workdir.resolve()
    active = project_pool_registry(_pool_events(workdir))
    if as_json:
        click.echo(json.dumps({"pools": active}, indent=2, sort_keys=True))
        return
    if not active:
        console.print("[dim]No active pools. Register one with 'bernstein pool register'.[/dim]")
        return
    table = Table(title="Active Sandbox Pools", header_style="bold cyan")
    table.add_column("Name", style="bold")
    table.add_column("Pool hash", style="dim")
    for name in sorted(active):
        table.add_row(name, active[name][:24] + "...")
    console.print(table)


@pool_group.command("show")
@click.argument("name")
@click.option(
    "--workdir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path(),
    show_default=True,
    help="Project root containing .sdd/.",
)
def show_cmd(name: str, workdir: Path) -> None:
    """Show the canonical manifest and hash for an active pool NAME."""
    workdir = workdir.resolve()
    pool_hash = project_pool_registry(_pool_events(workdir)).get(name)
    if pool_hash is None:
        console.print(f"[yellow]No active pool named[/yellow] {name!r}.")
        raise SystemExit(1)
    try:
        manifest = _store(workdir).get(pool_hash)
    except PoolStoreError as exc:
        console.print(f"[red]Cannot load pool body:[/red] {exc}")
        raise SystemExit(1) from exc
    click.echo(json.dumps(manifest.to_dict(), indent=2, sort_keys=True))


@pool_group.command("verify")
@click.option(
    "--workdir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path(),
    show_default=True,
    help="Project root containing .sdd/.",
)
def verify_cmd(workdir: Path) -> None:
    """Verify pool bodies and placement/enrolment receipts offline.

    Exits non-zero if any active pool body fails its content-addressed hash
    check or any stored placement receipt fails to recompute.
    """
    workdir = workdir.resolve()
    ok, errors = verify_pools(workdir)
    if ok:
        console.print("[bold green]Pool verification passed.[/bold green]")
        raise SystemExit(0)
    console.print("[bold red]Pool verification FAILED[/bold red]")
    for err in errors:
        console.print(f"  [red]![/red] {err}")
    raise SystemExit(1)


def verify_pools(workdir: Path) -> tuple[bool, list[str]]:
    """Verify active pool bodies and stored placement receipts.

    Returns ``(ok, errors)``. Reused by ``bernstein audit verify`` so the pool
    subsystem is a first-class integrity pillar alongside the HMAC chain.
    """
    from bernstein.core.sandbox.pool_placement import verify_placement_receipt

    errors: list[str] = []
    store = _store(workdir)
    active = project_pool_registry(_pool_events(workdir))
    for name, pool_hash in sorted(active.items()):
        try:
            store.get(pool_hash)
        except PoolStoreError as exc:
            errors.append(f"pool {name!r}: {exc}")

    placements_dir = workdir / ".sdd" / "sandbox" / "placements"
    if placements_dir.is_dir():
        for path in sorted(placements_dir.glob("*.json")):
            placement_hash = path.stem
            result = verify_placement_receipt(workdir, placement_hash)
            if not result.ok:
                errors.append(f"placement {placement_hash[:16]}...: {result.reason}")

    return (not errors), errors


__all__ = ["pool_group", "verify_pools"]
