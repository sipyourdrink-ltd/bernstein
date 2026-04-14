"""Audit CLI — Merkle-tree integrity seal and verification.

Commands:
  bernstein audit show             Show recent audit log events.
  bernstein audit seal             Compute and store a Merkle root.
  bernstein audit seal --anchor-git  Also create a git tag.
  bernstein audit verify --merkle  Verify the Merkle tree against disk.
"""

from __future__ import annotations

import contextlib
from pathlib import Path

import click
from rich.panel import Panel
from rich.table import Table

from bernstein.cli.helpers import console

AUDIT_DIR = Path(".sdd/audit")
MERKLE_DIR = AUDIT_DIR / "merkle"


@click.group("audit")
def audit_group() -> None:
    """Audit log integrity tools."""


@audit_group.command("show")
@click.option("--limit", default=20, show_default=True, help="Maximum number of events to show.")
def show_cmd(limit: int) -> None:
    """Show recent audit log events from .sdd/audit/."""
    import json as _json

    if not AUDIT_DIR.is_dir():
        console.print(
            "[yellow]No audit log found.[/yellow]  Run [bold]bernstein run[/bold] first to generate audit events."
        )
        return

    log_files = sorted(AUDIT_DIR.glob("*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not log_files:
        console.print(
            "[yellow]Audit directory exists but contains no log files.[/yellow]  "
            "Run [bold]bernstein run[/bold] to generate audit events."
        )
        return

    events: list[dict] = []
    for lf in log_files:
        try:
            for line in lf.read_text().splitlines():
                line = line.strip()
                if line:
                    with contextlib.suppress(_json.JSONDecodeError):
                        events.append(_json.loads(line))
        except OSError:
            pass
        if len(events) >= limit:
            break

    events = events[:limit]

    table = Table(show_header=True, header_style="bold magenta", show_lines=False)
    table.add_column("Timestamp", style="dim", no_wrap=True)
    table.add_column("Event", style="bold")
    table.add_column("Actor")
    table.add_column("Resource")

    for ev in events:
        ts = str(ev.get("timestamp", "—"))[:19]
        event_type = str(ev.get("event_type", "—"))
        actor = str(ev.get("actor", ""))
        resource = f"{ev.get('resource_type', '')}/{ev.get('resource_id', '')}"
        table.add_row(ts, event_type, actor, resource)

    console.print()
    console.print(table)
    console.print(f"\n[dim]Showing {len(events)} event(s) from {AUDIT_DIR}[/dim]\n")


@audit_group.command("seal")
@click.option("--anchor-git", is_flag=True, default=False, help="Anchor root hash as a git tag.")
def seal_cmd(anchor_git: bool) -> None:
    """Compute a Merkle root across all audit log files and store the seal."""
    from bernstein.core.merkle import anchor_to_git, compute_seal, save_seal

    if not AUDIT_DIR.is_dir():
        console.print(f"[red]Audit directory not found:[/red] {AUDIT_DIR}")
        console.print("[dim]Ensure the audit log is active (bernstein must have written audit events).[/dim]")
        raise SystemExit(1)

    try:
        _tree, seal = compute_seal(AUDIT_DIR)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from None

    seal_path = save_seal(seal, MERKLE_DIR)

    # Display result
    console.print()
    console.print(
        Panel(
            "[bold]Merkle Audit Seal[/bold]",
            border_style="green",
            expand=False,
        )
    )

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Key", style="dim", no_wrap=True, min_width=14)
    table.add_column("Value")
    table.add_row("Root hash", str(seal["root_hash"]))
    table.add_row("Leaves", str(seal["leaf_count"]))
    table.add_row("Algorithm", str(seal["algorithm"]))
    table.add_row("Sealed at", str(seal["sealed_at_iso"]))
    table.add_row("Seal file", str(seal_path))
    console.print(table)

    if anchor_git:
        root_hash = str(seal["root_hash"])
        tag = anchor_to_git(root_hash, Path.cwd())
        if tag:
            console.print(f"\n  [green]Git tag created:[/green] {tag}")
        else:
            console.print("\n  [yellow]Git anchoring failed (not a git repo or tag exists).[/yellow]")

    console.print()


@audit_group.command("verify")
@click.option("--merkle-only", is_flag=True, default=False, help="Only verify Merkle tree (skip HMAC chain).")
@click.option("--hmac-only", is_flag=True, default=False, help="Only verify HMAC chain (skip Merkle tree).")
def verify_cmd(merkle_only: bool, hmac_only: bool) -> None:
    """Verify audit log integrity (HMAC chain + Merkle tree).

    \b
      bernstein audit verify              Verify both HMAC chain and Merkle tree
      bernstein audit verify --hmac-only  Verify HMAC chain only
      bernstein audit verify --merkle-only  Verify Merkle tree only
    """
    if not AUDIT_DIR.is_dir():
        console.print(f"[red]Audit directory not found:[/red] {AUDIT_DIR}")
        raise SystemExit(1)

    all_passed = True

    if not merkle_only:
        all_passed = _verify_hmac_chain() and all_passed

    if not hmac_only:
        all_passed = _verify_merkle_tree() and all_passed

    console.print()
    raise SystemExit(0 if all_passed else 1)


def _verify_hmac_chain() -> bool:
    """Verify HMAC chain and print results. Returns True if valid."""
    from bernstein.core.audit import AuditLog

    audit_log = AuditLog(AUDIT_DIR)
    hmac_valid, hmac_errors = audit_log.verify()

    console.print()
    if hmac_valid:
        console.print(
            Panel("[bold green]HMAC Chain Verification Passed[/bold green]", border_style="green", expand=False)
        )
        return True
    console.print(
        Panel("[bold red]HMAC Chain Verification FAILED[/bold red]", border_style="red", expand=False)
    )
    for err in hmac_errors:
        console.print(f"  [red]![/red] {err}")
    return False


def _verify_merkle_tree() -> bool:
    """Verify Merkle tree and print results. Returns True if valid."""
    from bernstein.core.merkle import verify_merkle

    result = verify_merkle(AUDIT_DIR, MERKLE_DIR)

    console.print()
    if result.valid:
        console.print(
            Panel("[bold green]Merkle Verification Passed[/bold green]", border_style="green", expand=False)
        )
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("Key", style="dim", no_wrap=True, min_width=14)
        table.add_column("Value")
        table.add_row("Root hash", result.root_hash)
        if result.seal_path:
            table.add_row("Seal file", str(result.seal_path))
        console.print(table)
        return True
    console.print(
        Panel("[bold red]Merkle Verification FAILED[/bold red]", border_style="red", expand=False)
    )
    for err in result.errors:
        console.print(f"  [red]![/red] {err}")
    return False


@audit_group.command("verify-hmac")
def verify_hmac_cmd() -> None:
    """Verify HMAC chain integrity across all audit log files."""
    from bernstein.core.audit import AuditLog

    if not AUDIT_DIR.is_dir():
        console.print(f"[red]Audit directory not found:[/red] {AUDIT_DIR}")
        raise SystemExit(1)

    audit_log = AuditLog(AUDIT_DIR)
    valid, errors = audit_log.verify()

    console.print()
    if valid:
        console.print(
            Panel(
                "[bold green]HMAC Chain Verification Passed[/bold green]",
                border_style="green",
                expand=False,
            )
        )
    else:
        console.print(
            Panel(
                "[bold red]HMAC Chain Verification FAILED[/bold red]",
                border_style="red",
                expand=False,
            )
        )
        for err in errors:
            console.print(f"  [red]![/red] {err}")

    console.print()
    raise SystemExit(0 if valid else 1)


@audit_group.command("export")
@click.option("--period", required=True, help="Time period to export (e.g. Q1-2026, 2026-03, 2026).")
@click.option(
    "--format",
    "fmt",
    default="zip",
    type=click.Choice(["zip", "dir"]),
    show_default=True,
    help="Output format.",
)
@click.option("--output", "-o", default=None, help="Output directory (defaults to .sdd/evidence/).")
@click.option("--dir", "workdir", default=".", show_default=True, help="Project root directory.")
def export_cmd(period: str, fmt: str, output: str | None, workdir: str) -> None:
    """Export a SOC 2 evidence package for auditors.

    \b
    Collects audit logs, HMAC verification, Merkle seals, compliance config,
    WAL entries, and SBOM into a single package.

    \b
    Examples:
      bernstein audit export --period Q1-2026
      bernstein audit export --period Q1-2026 --format dir
      bernstein audit export --period 2026-03 -o /tmp/evidence
    """
    from bernstein.core.compliance import export_soc2_package, parse_period

    sdd_dir = Path(workdir).resolve() / ".sdd"
    if not sdd_dir.is_dir():
        console.print(f"[red]State directory not found:[/red] {sdd_dir}")
        console.print("[dim]Run [bold]bernstein run[/bold] first to generate audit data.[/dim]")
        raise SystemExit(1)

    # Validate period before doing work
    try:
        start, end = parse_period(period)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from None

    output_path = Path(output).resolve() if output else None

    try:
        result = export_soc2_package(sdd_dir, period, output_path=output_path, fmt=fmt)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from None

    # Display summary
    console.print()
    console.print(
        Panel(
            "[bold]SOC 2 Evidence Package[/bold]",
            border_style="green",
            expand=False,
        )
    )

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Key", style="dim", no_wrap=True, min_width=14)
    table.add_column("Value")
    table.add_row("Period", f"{period}  ({start} to {end})")
    table.add_row("Format", fmt)
    table.add_row("Output", str(result))
    console.print(table)
    console.print()


@audit_group.command("query")
@click.option("--event-type", default=None, help="Filter by event type.")
@click.option("--actor", default=None, help="Filter by actor.")
@click.option("--since", default=None, help="ISO 8601 lower bound (inclusive).")
@click.option("--limit", default=50, show_default=True, help="Maximum number of events to return.")
def query_cmd(event_type: str | None, actor: str | None, since: str | None, limit: int) -> None:
    """Query audit log events with filters."""
    from bernstein.core.audit import AuditLog

    if not AUDIT_DIR.is_dir():
        console.print(f"[red]Audit directory not found:[/red] {AUDIT_DIR}")
        raise SystemExit(1)

    audit_log = AuditLog(AUDIT_DIR)
    events = audit_log.query(event_type=event_type, actor=actor, since=since)
    events = events[:limit]

    if not events:
        console.print("[yellow]No matching audit events found.[/yellow]")
        return

    table = Table(show_header=True, header_style="bold magenta", show_lines=False)
    table.add_column("Timestamp", style="dim", no_wrap=True)
    table.add_column("Event Type", style="bold")
    table.add_column("Actor")
    table.add_column("Resource")
    table.add_column("HMAC", style="dim", no_wrap=True)

    for ev in events:
        table.add_row(
            ev.timestamp[:19],
            ev.event_type,
            ev.actor,
            f"{ev.resource_type}/{ev.resource_id}",
            ev.hmac[:12] + "…",
        )

    console.print()
    console.print(table)
    console.print(f"\n[dim]Showing {len(events)} event(s)[/dim]\n")
