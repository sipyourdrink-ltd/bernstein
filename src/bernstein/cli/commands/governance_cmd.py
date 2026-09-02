"""``bernstein governance`` and ``bernstein govern`` -- verify, validate, and inventory.

Issue #2309. Recomputes every access and budget decision recorded for a run from
the signed lineage spine and confirms the recorded verdicts::

    bernstein governance verify <run> --bindings <file> [--ledger <file>]

Access decisions re-resolve the subject's role from the presented signed role
bindings and re-project the role's permissions onto the action. Budget decisions
recompute per-subject spend from the cost ledger (never a stored counter) and
re-derive the verdict. A tampered verdict, a widened permission binding, or a
diverged ledger fails the check.

Issue #4979. Governance playbook schema and validation::

    bernstein govern validate playbook.yaml

Validates playbook structure, referential integrity (surface_refs and
ceiling_refs), and absence of duplicates.

Issue #4973. Governance surface inventory::

    bernstein govern inventory --output inventory.json

Discovers and catalogs governable surfaces (MCP tools, API endpoints, file
paths) with a content hash for audit trail.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from bernstein.cli.helpers import console


def _load_hmac_key() -> bytes:
    from bernstein.core.security.audit import load_or_create_audit_key

    return load_or_create_audit_key()


def _lineage_root(workdir: Path) -> Path:
    return workdir / ".sdd" / "lineage"


@click.group("governance")
def governance_group() -> None:
    """Verify RBAC and budget decisions as projections over the audit chain.

    \\b
      bernstein governance verify <run> --bindings b.json --ledger ledger.jsonl
    """


@click.group("govern")
def govern_group() -> None:
    """Governance surface management commands.

    \\b
      bernstein govern inventory   discover and catalog governable surfaces
      bernstein govern validate    check a governance playbook against the schema
    """


@governance_group.command("verify")
@click.argument("run_id", required=True)
@click.option(
    "--bindings",
    "bindings_file",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Signed role-bindings JSON the access decisions project over.",
)
@click.option(
    "--ledger",
    "ledger_file",
    type=click.Path(exists=True, dir_okay=False),
    default=None,
    help="Spend ledger JSONL for recomputing budget decisions (required when the run has budget rows).",
)
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    show_default=True,
    help="Project root containing .sdd/.",
)
def governance_verify_cmd(run_id: str, bindings_file: str, ledger_file: str | None, workdir: str) -> None:
    """Recompute every access and budget verdict for *run_id* and match them.

    Exit codes: 0 = verified, 1 = no records / bad input, 2 = mismatch.
    """
    from bernstein.core.security.governance import RoleBindings, verify_governance

    root = Path(workdir).resolve()
    bindings = RoleBindings.from_dict(json.loads(Path(bindings_file).read_text(encoding="utf-8")))
    ledger_path = Path(ledger_file).resolve() if ledger_file else None

    result = verify_governance(
        run_id=run_id,
        lineage_root=_lineage_root(root),
        hmac_key=_load_hmac_key(),
        bindings=bindings,
        ledger_path=ledger_path,
    )

    console.print()
    console.print(f"[bold]Governance verify[/bold] run={run_id}")
    console.print(f"  decisions checked  {result.checked}")
    if result.ok:
        console.print("[green]OK[/green] -- every access and budget verdict recomputes from the chain.")
        raise SystemExit(0)
    if result.checked == 0:
        console.print(f"[yellow]NO RECORDS[/yellow] -- {result.reason}")
        raise SystemExit(1)
    console.print(f"[red]MISMATCH[/red] -- {result.reason}")
    raise SystemExit(2)


@govern_group.command("inventory")
@click.option(
    "--output",
    "-o",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Write inventory JSON to this path. Omit to print to stdout.",
)
@click.option(
    "--workspace",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    show_default=True,
    help="Workspace root to scan for surfaces.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit raw JSON to stdout (overrides --output).",
)
def govern_inventory_cmd(
    output: Path | None,
    workspace: str,
    as_json: bool,
) -> None:
    """Discover and catalog governable surfaces in the workspace.

    Scans for:
    - MCP tools from ``.mcp.json``
    - API endpoints from OpenAPI/Swagger specs
    - File paths from ``bernstein.yaml`` worktree config

    Writes a JSON inventory with ``inventory_hash`` (content digest) and
    ``timestamp`` for audit trail. The inventory is consumed by
    ``bernstein govern propose`` when drafting governance playbooks.

    Exit codes: 0 = success, 1 = error.
    """
    from bernstein.core.governance.inventory import discover_surfaces

    root = Path(workspace).resolve()

    try:
        inventory = discover_surfaces(root)
    except OSError as exc:
        console.print(f"[red]Discovery failed:[/red] {exc}")
        sys.exit(1)

    data = inventory.to_dict()

    if as_json:
        click.echo(json.dumps(data, indent=2, sort_keys=True))
        return

    if output is not None:
        try:
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_text(json.dumps(data, indent=2, sort_keys=True), encoding="utf-8")
        except OSError as exc:
            console.print(f"[red]Failed to write inventory:[/red] {exc}")
            sys.exit(1)
        console.print(f"[green]Wrote[/green] {output}")
        console.print(f"  surfaces       : {len(inventory.surfaces)}")
        console.print(f"  inventory_hash : {inventory.inventory_hash[:16]}...")
        console.print(f"  timestamp      : {inventory.timestamp}")
        return

    console.print(f"[bold]Governance Inventory[/bold] -- {root}")
    console.print(f"  surfaces       : {len(inventory.surfaces)}")
    console.print(f"  inventory_hash : {inventory.inventory_hash[:16]}...")
    console.print(f"  timestamp      : {inventory.timestamp}")
    console.print()

    if not inventory.surfaces:
        console.print("[dim]No surfaces discovered. Check .mcp.json, OpenAPI specs, or bernstein.yaml.[/dim]")
        return

    surface_by_kind: dict[str, list] = {}
    for s in inventory.surfaces:
        surface_by_kind.setdefault(s.kind, []).append(s)

    for kind, surfaces in sorted(surface_by_kind.items()):
        console.print(f"  [cyan]{kind}[/cyan] ({len(surfaces)})")
        for s in surfaces:
            console.print(f"    {s.identifier}")


@govern_group.command("validate")
@click.argument("playbook_file", type=click.Path(exists=True, dir_okay=False))
def govern_validate_cmd(playbook_file: str) -> None:
    """Validate a governance playbook YAML file.

    Checks playbook structure, referential integrity (surface_refs and
    ceiling_refs resolve), and absence of duplicate ids.

    Exit codes: 0 = valid, 1 = validation failed.

    \\b
    Example:
      bernstein govern validate playbook.yaml
    """
    from bernstein.core.governance.playbook import (
        PlaybookSchema,
        PlaybookValidationError,
        load_playbook,
    )

    path = Path(playbook_file).resolve()

    try:
        playbook = load_playbook(path)
        schema = PlaybookSchema()
        schema.validate(playbook)
    except PlaybookValidationError as exc:
        console.print(f"[red]VALIDATION FAILED[/red] -- {path}")
        console.print_json(data=exc.to_json())
        sys.exit(1)

    console.print(f"[green]OK[/green] -- {path} is a valid governance playbook")
    sys.exit(0)
