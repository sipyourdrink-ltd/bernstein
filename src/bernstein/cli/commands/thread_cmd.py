"""``bernstein thread`` - verify the live event stream against the journal.

Issue #2297. The TUI and web UI render the run as a live SSE stream that
is a hash-anchored projection of the canonical run journal. ``thread
verify --run <id>`` proves that projection equals the executed journal:
it recomputes the journal's Merkle chain and confirms every projected
event carries the byte-identical entry hash, reporting a clean pass or the
first divergent index (AC3).
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from bernstein.cli.helpers import console
from bernstein.core.replay.journal import JOURNAL_FILENAME
from bernstein.core.replay.thread_projection import verify_thread_against_journal


def thread_verify(*, run_id: str, sdd_dir: Path, as_json: bool) -> int:
    """Verify the streamed thread for *run_id* equals its journal.

    Args:
        run_id: The run whose journal to verify (``.sdd/runs/<id>/``).
        sdd_dir: Path to the ``.sdd`` directory.
        as_json: Emit a machine-readable result instead of a table.

    Returns:
        ``0`` when the projected thread equals the journal, ``1`` on a
        divergence, ``2`` when the run journal is missing.
    """
    journal_path = sdd_dir / "runs" / run_id / JOURNAL_FILENAME
    if not journal_path.exists():
        if as_json:
            console.print_json(json.dumps({"ok": False, "run_id": run_id, "error": "run journal not found"}))
        else:
            console.print(f"[red]No run journal for[/red] {run_id} (looked at {journal_path})")
        return 2

    result = verify_thread_against_journal(journal_path)

    if as_json:
        console.print_json(
            json.dumps(
                {
                    "ok": result.ok,
                    "run_id": run_id,
                    "count": result.count,
                    "divergent_index": result.divergent_index,
                    "errors": result.errors,
                }
            )
        )
    elif result.ok:
        console.print(
            f"[green]thread verified[/green] for [cyan]{run_id}[/cyan]: {result.count} steps match the journal"
        )
    else:
        console.print(f"[red]thread divergence[/red] for [cyan]{run_id}[/cyan] at step {result.divergent_index}")
        for err in result.errors:
            console.print(f"  - {err}")

    return 0 if result.ok else 1


@click.group("thread")
def thread_cmd() -> None:
    """Inspect and verify the live run event stream (the audit-chain thread)."""


@thread_cmd.command("verify")
@click.option("--run", "run_id", required=True, help="Run id to verify (.sdd/runs/<id>/).")
@click.option(
    "--sdd-dir",
    default=".sdd",
    show_default=True,
    help="Path to the .sdd directory.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit a machine-readable result.")
def thread_verify_cmd(run_id: str, sdd_dir: str, as_json: bool) -> None:
    """Prove the streamed thread for a run equals its executed journal.

    \b
      bernstein thread verify --run 20240315-143022
      bernstein thread verify --run latest --json
    """
    rc = thread_verify(run_id=run_id, sdd_dir=Path(sdd_dir), as_json=as_json)
    if rc != 0:
        raise SystemExit(rc)


__all__ = ["thread_cmd", "thread_verify"]
