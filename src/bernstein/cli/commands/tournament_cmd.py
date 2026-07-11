"""``bernstein tournament``: parallel-attempt selection receipts (issue #2353).

A tournament fans out N sibling attempts of one task and selects the winner by
scripted evaluators -- no model call in the decision path. The signed selection
receipt *is* the proof of why one attempt won: it names every attempt hash,
every evaluator output, every score, the winner, and the lineage edges (one
``chosen``, the rest ``sibling``).

    bernstein tournament show <task>     Render a selection receipt.
    bernstein tournament verify <task>   Recompute + re-verify it offline.

``bernstein audit verify`` runs the same integrity check across every receipt,
so a tampered score or a hand-picked winner is detected exactly like a tampered
chain entry.
"""

from __future__ import annotations

from pathlib import Path

import click

from bernstein.cli.helpers import console


def _load_hmac_key() -> bytes:
    from bernstein.core.security.audit import load_or_create_audit_key

    return load_or_create_audit_key()


@click.group("tournament")
def tournament_group() -> None:
    """Render and verify tournament selection receipts.

    \b
      bernstein tournament show <task>
      bernstein tournament verify <task>
    """


@tournament_group.command("show")
@click.argument("task")
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    show_default=True,
    help="Project root containing .sdd/.",
)
def tournament_show_cmd(task: str, workdir: str) -> None:
    """Render the tournament selection receipt for TASK.

    Exit code 0 when a receipt exists, 1 when there is none.
    """
    from rich.table import Table

    from bernstein.core.tournament.receipt import CHOSEN_RELATION, read_tournament_receipt

    root = Path(workdir).resolve()
    receipt = read_tournament_receipt(root, task)
    if receipt is None:
        console.print(f"[yellow]No tournament receipt found for task[/yellow] {task}")
        raise SystemExit(1)

    anchor = receipt.journal_entry_hash or ""
    anchor_short = anchor.split(":", 1)[-1][:16] if anchor else "unanchored"
    winner_short = receipt.winner_hash.split(":", 1)[-1][:16]

    console.print()
    console.print(f"[bold]Tournament selection[/bold] task={receipt.task_id}")
    console.print(f"  winner          {winner_short}")
    console.print(f"  attempts        {len(receipt.attempts)}")
    console.print(f"  evaluators      {', '.join(e.name for e in receipt.spec.evaluators)}")
    console.print(f"  tie_break       {receipt.spec.tie_break}")
    console.print(f"  anchor          {anchor_short}")

    table = Table(show_header=True, header_style="bold magenta")
    table.add_column("Rank", justify="right")
    table.add_column("Attempt", style="dim")
    table.add_column("Score", justify="right")
    table.add_column("Edge")
    edge_by_hash = {e.attempt_hash: e.relation for e in receipt.edges}
    for rank, row in enumerate(receipt.scores, start=1):
        relation = edge_by_hash.get(row.attempt_hash, "?")
        marker = "[green]chosen[/green]" if relation == CHOSEN_RELATION else relation
        table.add_row(
            str(rank),
            row.attempt_hash.split(":", 1)[-1][:16] + "…",
            f"{row.score:.4f}",
            marker,
        )
    console.print(table)
    console.print(f"\n[dim]Verify offline:[/dim] bernstein tournament verify {receipt.task_id}\n")


@tournament_group.command("verify")
@click.argument("task")
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    show_default=True,
    help="Project root containing .sdd/.",
)
def tournament_verify_cmd(task: str, workdir: str) -> None:
    """Recompute and re-verify TASK's tournament selection offline.

    Replays the deterministic scorer over the recorded evaluator outputs,
    checks exactly one chosen edge over the recorded attempts, verifies the
    Ed25519 signature, and re-anchors the receipt against the tournament spine.
    Exit codes: 0 = verified, 1 = no receipt, 2 = mismatch (a tampered score, a
    hand-picked winner, or a tampered receipt/spine).
    """
    from bernstein.core.tournament.receipt import verify_tournament_receipt

    root = Path(workdir).resolve()
    result = verify_tournament_receipt(
        workdir=root,
        lineage_root=root / ".sdd" / "lineage",
        hmac_key=_load_hmac_key(),
        task_id=task,
    )
    console.print()
    console.print(f"[bold]Tournament verify[/bold] task={task}")
    if result.ok:
        assert result.receipt is not None
        console.print(f"  winner {result.winner_hash.split(':', 1)[-1][:16]}")
        console.print("[green]OK[/green] -- the selection recomputes deterministically from the sealed receipt.")
        raise SystemExit(0)
    if result.receipt is None:
        console.print(f"[yellow]NO RECEIPT[/yellow] -- {result.reason}")
        raise SystemExit(1)
    console.print(f"[red]MISMATCH[/red] -- {result.reason}")
    raise SystemExit(2)
