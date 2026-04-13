"""Verify CLI — WAL integrity, execution determinism, memory provenance, and formal verification.

Commands:
  bernstein verify --wal-integrity <run-id>   Verify WAL hash chain
  bernstein verify --determinism <run-id>     Compute execution fingerprint
  bernstein verify --memory-audit             Audit lesson memory provenance chain
  bernstein verify --formal <task-id>         Run Z3/Lean4 formal property checks for a task
"""

from __future__ import annotations

from pathlib import Path

import click
from rich.panel import Panel
from rich.table import Table

from bernstein.cli.helpers import console

_GREEN_ZERO = "[green]0[/green]"

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
@click.option(
    "--formal",
    "formal_task_id",
    default=None,
    metavar="TASK_ID",
    help="Run Z3/Lean4 formal property checks for a completed task.",
)
def verify_cmd(
    wal_run_id: str | None,
    determinism_run_id: str | None,
    memory_audit: bool,
    formal_task_id: str | None,
) -> None:
    """Verify WAL integrity, execution determinism, memory provenance, and formal properties.

    \b
      bernstein verify --wal-integrity <run-id>   Validate hash chain
      bernstein verify --determinism  <run-id>    Show execution fingerprint
      bernstein verify --memory-audit             Audit lesson memory provenance
      bernstein verify --formal <task-id>         Run Z3/Lean4 property checks
    """
    if wal_run_id is None and determinism_run_id is None and not memory_audit and formal_task_id is None:
        console.print(
            "[dim]Use --wal-integrity <run-id>, --determinism <run-id>, --memory-audit, or --formal <task-id>.[/dim]"
        )
        console.print("[dim]WAL files are stored in .sdd/runtime/wal/<run-id>.wal.jsonl[/dim]")
        return

    exit_code = 0

    if wal_run_id is not None:
        exit_code |= _verify_wal_integrity(wal_run_id)

    if determinism_run_id is not None:
        exit_code |= _verify_determinism(determinism_run_id)

    if memory_audit:
        exit_code |= _verify_memory_provenance()

    if formal_task_id is not None:
        exit_code |= _verify_formal(formal_task_id)

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
            f"[red]{len(tampered)}[/red]" if tampered else _GREEN_ZERO,
        )
        table2.add_row(
            "Chain-mispositioned",
            f"[red]{len(mispositioned)}[/red]" if mispositioned else _GREEN_ZERO,
        )
        console.print(table2)

    console.print()
    return 0 if chain_result.valid else 1


def _verify_formal(task_id: str) -> int:
    """Run Z3/Lean4 formal property checks for *task_id*. Returns 0 on pass, 1 on failure."""
    import httpx

    from bernstein.cli.helpers import SERVER_URL
    from bernstein.core.formal_verification import load_formal_verification_config, run_formal_verification
    from bernstein.core.models import Task

    workdir = Path.cwd()
    console.print()

    # Load formal_verification config from bernstein.yaml
    fv_config = load_formal_verification_config(workdir)
    if fv_config is None:
        console.print(
            Panel(
                "[dim]No formal_verification section in bernstein.yaml — nothing to verify.[/dim]",
                border_style="dim",
                expand=False,
            )
        )
        console.print()
        return 0

    if not fv_config.enabled:
        console.print(
            Panel("[dim]Formal verification is disabled (enabled: false).[/dim]", border_style="dim", expand=False)
        )
        console.print()
        return 0

    if not fv_config.properties:
        console.print(
            Panel("[dim]No properties defined in formal_verification section.[/dim]", border_style="dim", expand=False)
        )
        console.print()
        return 0

    # Fetch task from server
    task: Task | None = None
    try:
        with httpx.Client(timeout=5.0) as client:
            resp = client.get(f"{SERVER_URL}/tasks/{task_id}")
            resp.raise_for_status()
            task = Task.from_dict(resp.json())
    except Exception as exc:
        console.print(
            Panel(
                f"[bold red]Could not fetch task {task_id!r}:[/bold red] {exc}",
                border_style="red",
                expand=False,
            )
        )
        console.print(f"[dim]Is the Bernstein server running? ({SERVER_URL})[/dim]")
        console.print()
        return 1

    # Run formal verification
    fv_result = run_formal_verification(task, workdir, fv_config)

    if fv_result.passed:
        console.print(
            Panel(
                "[bold green]Formal Verification: PASSED[/bold green]",
                border_style="green",
                expand=False,
            )
        )
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("Key", style="dim", no_wrap=True, min_width=22)
        table.add_column("Value")
        table.add_row("Task ID", task_id)
        table.add_row("Task", task.title[:60])
        table.add_row("Properties checked", str(fv_result.properties_checked))
        table.add_row("Violations", _GREEN_ZERO)
        console.print(table)
    else:
        console.print(
            Panel(
                "[bold red]Formal Verification: FAILED[/bold red]",
                border_style="red",
                expand=False,
            )
        )
        table = Table(show_header=False, box=None, padding=(0, 2))
        table.add_column("Key", style="dim", no_wrap=True, min_width=22)
        table.add_column("Value")
        table.add_row("Task ID", task_id)
        table.add_row("Task", task.title[:60])
        table.add_row("Properties checked", str(fv_result.properties_checked))
        table.add_row("Violations", f"[red]{len(fv_result.violations)}[/red]")
        console.print(table)
        console.print()
        for violation in fv_result.violations:
            console.print(f"  [red]✗[/red] [bold]{violation.property_name}[/bold] ({violation.checker})")
            console.print(f"    [dim]{violation.detail}[/dim]")
            if violation.counterexample and violation.counterexample != "(timeout)":
                console.print(f"    [yellow]Counterexample:[/yellow] {violation.counterexample[:200]}")

    console.print()
    return 0 if fv_result.passed else 1
