"""Garbage-collection commands for durable stores."""

from __future__ import annotations

from pathlib import Path

import click

from bernstein.core.persistence.cas_gc import run_cas_gc_cli


@click.group("gc")
def gc_group() -> None:
    """Reclaim storage held by durable stores."""


@gc_group.command("cas")
@click.option(
    "--workdir",
    type=click.Path(path_type=Path, file_okay=False),
    default=Path(),
    show_default=True,
    help="Root directory containing .sdd/.",
)
@click.option(
    "--days",
    type=int,
    default=None,
    help=(
        "Delete unreferenced blobs older than N days (0 deletes immediately). "
        "Defaults to the configured retention window."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Report what would be deleted without modifying the store.",
)
@click.option(
    "--yes",
    is_flag=True,
    default=False,
    help="Skip the confirmation prompt.",
)
def gc_cas(workdir: Path, days: int | None, dry_run: bool, yes: bool) -> None:
    """Mark and sweep unreferenced blobs from the CAS store.

    Referenced digests are collected from the durable roots — the write-ahead
    log, snapshots, audit seals, lineage records and the backlog — so a blob
    still reachable from any of them is preserved regardless of its age.
    """
    if not run_cas_gc_cli(workdir.resolve(), days=days, dry_run=dry_run, yes=yes):
        raise SystemExit(1)
