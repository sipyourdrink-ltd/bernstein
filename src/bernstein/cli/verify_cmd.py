"""Verify CLI — WAL integrity, execution determinism, and memory provenance.

Commands:
  bernstein verify --wal-integrity <run-id>   Verify WAL hash chain
  bernstein verify --determinism <run-id>     Compute execution fingerprint
  bernstein verify --memory-audit             Audit lesson memory provenance chain
"""

from __future__ import annotations

from pathlib import Path

import click
from rich.panel import Panel
from rich.table import Table

from bernstein.cli.helpers import console

SDD_DIR = Path(".sdd")


@click.command("verify")
@click.option(
    "--wal-integrity",
    "wal_run_id",
    default=None,
    metavar="RUN_ID",
    help="Verify WAL hash chain integrity for a run.",
)
@click.option(
    "--determinism",
    "determinism_run_id",
    default=None,
    metavar="RUN_ID",
    help="Compute and display execution fingerprint for a run.",
)
@click.option(
    "--memory-audit",
    "memory_audit",
    is_flag=True,
    default=False,
    help="Audit lesson memory provenance chain (OWASP ASI06 2026).",
)
def verify_cmd(
    wal_run_id: str | None,
    determinism_run_id: str | None,
    memory_audit: bool,
) -> None:
    """Verify WAL integrity, execution determinism, and memory provenance.

    \b
      bernstein verify --wal-integrity <run-id>   Validate hash chain
      bernstein verify --determinism  <run-id>    Show execution fingerprint
      bernstein verify --memory-audit             Audit lesson memory provenance
    """
    if wal_run_id is None and determinism_run_id is None and not memory_audit:
        console.print("[dim]Use --wal-integrity <run-id>, --determinism <run-id>, or --memory-audit.[/dim]")
        console.print("[dim]WAL files are stored in .sdd/runtime/wal/<run-id>.wal.jsonl[/dim]")
        return

    exit_code = 0

    if wal_run_id is not None:
        exit_code |= _verify_wal_integrity(wal_run_id)

    if determinism_run_id is not None:
        exit_code |= _verify_determinism(determinism_run_id)

    if memory_audit:
        exit_code |= _verify_memory_provenance()

    raise SystemExit(exit_code)


def _verify_wal_integrity(run_id: str) -> int:
    """Verify the WAL hash chain for *run_id*. Returns 0 on success, 1 on failure."""
    from bernstein.core.wal import WALReader

    reader = WALReader(run_id=run_id, sdd_dir=SDD_DIR)

    console.print()
    try:
        is_valid, errors = reader.verify_chain()
    except FileNotFoundError:
        console.print(
            Panel(
                f"[bold red]WAL file not found for run:[/bold red] {run_id}",
                border_style="red",
                expand=False,
            )
        )
        console.print(f"[dim]Expected: {SDD_DIR}/runtime/wal/{run_id}.wal.jsonl[/dim]")
        console.print()
        return 1

    if is_valid:
        # Count entries for display
        entry_count = sum(1 for _ in reader.iter_entries())
        console.print(
            Panel(
                "[bold green]WAL Integrity: PASSED[/bold green]",
                border_style="green",
                expand=False,
            )
        )
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("Key", style="dim", no_wrap=True, min_width=14)
        table.add_column("Value")
        table.add_row("Run ID", run_id)
        table.add_row("Entries", str(entry_count))
        table.add_row("Chain", "intact")
        console.print(table)
    else:
        console.print(
            Panel(
                "[bold red]WAL Integrity: FAILED[/bold red]",
                border_style="red",
                expand=False,
            )
        )
        for err in errors:
            console.print(f"  [red]![/red] {err}")

    console.print()
    return 0 if is_valid else 1


def _verify_determinism(run_id: str) -> int:
    """Compute and display execution fingerprint for *run_id*. Returns 0 always."""
    from bernstein.core.wal import ExecutionFingerprint, WALReader

    reader = WALReader(run_id=run_id, sdd_dir=SDD_DIR)

    console.print()
    try:
        fp = ExecutionFingerprint.from_wal(reader)
    except FileNotFoundError:
        console.print(
            Panel(
                f"[bold red]WAL file not found for run:[/bold red] {run_id}",
                border_style="red",
                expand=False,
            )
        )
        console.print(f"[dim]Expected: {SDD_DIR}/runtime/wal/{run_id}.wal.jsonl[/dim]")
        console.print()
        return 1

    fingerprint = fp.compute()

    # Count entries
    entry_count = sum(1 for _ in WALReader(run_id=run_id, sdd_dir=SDD_DIR).iter_entries())

    console.print(
        Panel(
            "[bold]Execution Determinism Fingerprint[/bold]",
            border_style="blue",
            expand=False,
        )
    )
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column("Key", style="dim", no_wrap=True, min_width=14)
    table.add_column("Value")
    table.add_row("Run ID", run_id)
    table.add_row("Entries", str(entry_count))
    table.add_row("Fingerprint", fingerprint)
    console.print(table)
    console.print("\n  [dim]Two runs with the same fingerprint made identical decisions in identical order.[/dim]")
    console.print()
    return 0


def _verify_memory_provenance() -> int:
    """Audit the lesson memory provenance chain. Returns 0 on clean, 1 on failure."""
    from bernstein.core.memory_integrity import audit_provenance, verify_chain

    lessons_path = SDD_DIR / "memory" / "lessons.jsonl"
    console.print()

    if not lessons_path.exists():
        console.print(
            Panel(
                "[dim]No lesson memory found — nothing to audit.[/dim]",
                border_style="dim",
                expand=False,
            )
        )
        console.print()
        return 0

    chain_result = verify_chain(lessons_path)

    if chain_result.valid:
        console.print(
            Panel(
                "[bold green]Memory Provenance: CLEAN[/bold green]",
                border_style="green",
                expand=False,
            )
        )
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("Key", style="dim", no_wrap=True, min_width=20)
        table.add_column("Value")
        table.add_row("Entries verified", str(chain_result.entries_checked))
        table.add_row("Chain", "intact")
        table.add_row("Tampering", "none detected")
        console.print(table)
    else:
        console.print(
            Panel(
                "[bold red]Memory Provenance: VIOLATION DETECTED[/bold red]",
                border_style="red",
                expand=False,
            )
        )
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("Key", style="dim", no_wrap=True, min_width=20)
        table.add_column("Value")
        table.add_row("Entries checked", str(chain_result.entries_checked))
        table.add_row("First broken at", f"line {chain_result.broken_at}" if chain_result.broken_at > 0 else "N/A")
        console.print(table)
        console.print()
        for err in chain_result.errors:
            console.print(f"  [red]![/red] {err}")

    # Show provenance trail summary
    trail = audit_provenance(lessons_path)
    if trail:
        tampered = [e for e in trail if not e.hash_valid]
        mispositioned = [e for e in trail if not e.chain_position_valid]
        console.print()
        table2 = Table(show_header=False, box=None, padding=(0, 2))
        table2.add_column("Key", style="dim", no_wrap=True, min_width=20)
        table2.add_column("Value")
        table2.add_row("Total entries", str(len(trail)))
        table2.add_row(
            "Hash-tampered",
            f"[red]{len(tampered)}[/red]" if tampered else "[green]0[/green]",
        )
        table2.add_row(
            "Chain-mispositioned",
            f"[red]{len(mispositioned)}[/red]" if mispositioned else "[green]0[/green]",
        )
        console.print(table2)

    console.print()
    return 0 if chain_result.valid else 1
