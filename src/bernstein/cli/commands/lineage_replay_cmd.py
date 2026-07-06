"""``bernstein lineage replay <run_id>`` -- walk the spine chain in order.

Lists every entry in the run's lineage spine in append order, showing
the artifact path, actor, step, model, content hash, and per-entry hash.
The chain head is the run's artifact-provenance identity.

Exits 1 with a distinct ``no entries`` message when the run emitted no
lineage (issue #2292).
"""

from __future__ import annotations

from pathlib import Path

import click
from rich.table import Table

from bernstein.cli.helpers import console
from bernstein.core.lineage.spine import LineageSpine


def _load_hmac_key() -> bytes:
    from bernstein.core.security.audit import load_or_create_audit_key

    return load_or_create_audit_key()


@click.command(name="replay")
@click.argument("run_id", required=True)
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    show_default=True,
    help="Project root containing .sdd/.",
)
@click.option(
    "--limit",
    type=int,
    default=100,
    show_default=True,
    help="Maximum number of entries to display.",
)
def lineage_replay_cmd(run_id: str, workdir: str, limit: int) -> None:
    """Replay the lineage spine for *run_id* in append order."""
    lineage_root = Path(workdir).resolve() / ".sdd" / "lineage"
    spine = LineageSpine(lineage_root, run_id=run_id, hmac_key=_load_hmac_key())
    entries = list(spine.iter_entries())
    if not entries:
        console.print(f"[yellow]NO ENTRIES[/yellow] -- no lineage spine for run={run_id}.")
        raise SystemExit(1)

    console.print()
    console.print(f"[bold]Lineage spine[/bold] run={run_id} entries={len(entries)} head={spine.head_hash()[:16]}")
    console.print()

    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("#", justify="right", style="dim")
    table.add_column("Time", style="dim", no_wrap=True)
    table.add_column("Artifact", overflow="fold")
    table.add_column("Actor", no_wrap=True)
    table.add_column("Step", no_wrap=True)
    table.add_column("Model", no_wrap=True)
    table.add_column("Content", style="dim", no_wrap=True)
    table.add_column("Entry hash", style="dim", no_wrap=True)

    for idx, entry in enumerate(entries[:limit]):
        table.add_row(
            str(idx),
            str(entry.timestamp),
            entry.artifact_path,
            entry.actor,
            entry.step_id,
            entry.model or "-",
            entry.content_hash.removeprefix("sha256:")[:12],
            entry.entry_hash.removeprefix("sha256:")[:12],
        )

    console.print(table)
    console.print()
