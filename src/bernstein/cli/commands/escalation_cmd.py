"""``bernstein escalation``: journal-anchored stall escalation receipts.

Issue #2299. Surfaces the signed, spine-anchored escalation receipt a stalled
worker leaves behind. The receipt fixes the exact failure window by binding the
last N run-journal entries by their Merkle hash, references an f03 fork point
for resume, and recommends a deterministic action:

    bernstein escalation show   <receipt-id>
    bernstein escalation verify <receipt-id>

``show`` prints the operator projection (stall reason, recommended action,
resume fork point, spine anchor) -- never the signature or the raw window
hashes. ``verify`` reconstructs the trailing window from the run journal,
walks the journal's Merkle chain, and confirms every bound entry hash matches;
a tampered journal entry inside the window fails the check.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from bernstein.cli.helpers import console


def _load_hmac_key() -> bytes:
    from bernstein.core.security.audit import load_or_create_audit_key

    return load_or_create_audit_key()


def _sdd_dir(workdir: Path) -> Path:
    return workdir / ".sdd"


def _lineage_root(workdir: Path) -> Path:
    return workdir / ".sdd" / "lineage"


@click.group("escalation")
def escalation_group() -> None:
    """Show and verify journal-anchored stall escalation receipts.

    \b
      bernstein escalation show   <receipt-id>
      bernstein escalation verify <receipt-id>
    """


@escalation_group.command("show")
@click.argument("receipt_id")
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    show_default=True,
    help="Project root containing .sdd/.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit machine-readable JSON.")
def escalation_show_cmd(receipt_id: str, workdir: str, as_json: bool) -> None:
    """Print the operator projection of an escalation receipt.

    Exit codes: 0 = found, 1 = no receipt.
    """
    from bernstein.core.orchestration.escalation import (
        project_escalation_receipt,
        read_escalation_receipt,
    )

    root = Path(workdir).resolve()
    receipt = read_escalation_receipt(_sdd_dir(root), receipt_id)
    if receipt is None:
        console.print(f"[yellow]NO RECEIPT[/yellow] -- no escalation receipt for id {receipt_id}")
        raise SystemExit(1)

    view = project_escalation_receipt(receipt)
    if as_json:
        console.print_json(json.dumps(view))
        return

    console.print()
    console.print("[bold]Escalation receipt[/bold]")
    console.print(f"  receipt_id          {view['receipt_id']}")
    console.print(f"  run_id              {view['run_id']}")
    console.print(f"  worker_id           {view['worker_id']}")
    console.print(f"  stall_reason        {view['stall_reason']}")
    console.print(f"  recommended_action  {view['recommended_action']}")
    console.print(f"  window              from step {view['from_step']}, size {view['window_size']}")
    console.print(f"  journal_head        {view['journal_head_at_stall']}")
    if view["fork_snapshot_sha"]:
        console.print(f"  resume fork         step {view['fork_step']} -> {view['fork_snapshot_sha']}")
    else:
        console.print("  resume fork         [dim]none pinned[/dim]")
    console.print(f"  journal_entry_hash  {view['journal_entry_hash']}")


@escalation_group.command("verify")
@click.argument("receipt_id")
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    show_default=True,
    help="Project root containing .sdd/.",
)
def escalation_verify_cmd(receipt_id: str, workdir: str) -> None:
    """Reconstruct the failure window from the journal and confirm the receipt.

    Recomputes the trailing window from the run journal, walks the journal's
    Merkle chain, and checks the Ed25519 signature and spine anchor. Exit codes:
    0 = verified, 1 = no receipt, 2 = mismatch (tamper).
    """
    from bernstein.core.orchestration.escalation import verify_escalation_receipt

    root = Path(workdir).resolve()
    result = verify_escalation_receipt(
        sdd_dir=_sdd_dir(root),
        lineage_root=_lineage_root(root),
        hmac_key=_load_hmac_key(),
        receipt_id=receipt_id,
    )
    console.print()
    console.print(f"[bold]Escalation receipt verify[/bold] id={receipt_id}")
    if result.ok:
        if result.reason:
            # A degraded receipt (missing/empty journal at kill time) verifies
            # its signature and spine anchor but has no window to reconstruct.
            console.print(f"[yellow]OK (degraded)[/yellow] -- {result.reason}.")
        else:
            console.print("[green]OK[/green] -- failure window reconstructs from the journal and matches the receipt.")
        raise SystemExit(0)
    if result.receipt is None:
        console.print(f"[yellow]NO RECEIPT[/yellow] -- {result.reason}")
        raise SystemExit(1)
    console.print(f"[red]MISMATCH[/red] -- {result.reason}")
    raise SystemExit(2)


__all__ = ["escalation_group"]
