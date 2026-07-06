"""``bernstein webhook verify``: audited webhook-node receipts (#2310).

The webhook node turns an otherwise-opaque no-code flow step into a verifiable
one: an inbound webhook triggers a run and both the inbound event and the
outbound result carry signed receipts anchored to the run journal and the
webhook-node lineage spine.

    bernstein webhook verify <event_id>

``verify`` recomputes the inbound event hash and confirms the outbound result
hash against the journal, re-checks both Ed25519 signatures offline, and
re-anchors both receipts against the spine, so a tampered receipt, spine, or
journal is detected.
"""

from __future__ import annotations

from pathlib import Path

import click

from bernstein.cli.helpers import console


def _load_hmac_key() -> bytes:
    from bernstein.core.security.audit import load_or_create_audit_key

    return load_or_create_audit_key()


def _lineage_root(workdir: Path) -> Path:
    return workdir / ".sdd" / "lineage"


@click.group("webhook")
def webhook_group() -> None:
    """Audited webhook-node receipts (signed inbound + outbound).

    \b
      bernstein webhook verify <event_id>
    """


@webhook_group.command("verify")
@click.argument("event_id")
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    show_default=True,
    help="Project root containing .sdd/.",
)
def webhook_verify_cmd(event_id: str, workdir: str) -> None:
    """Recompute the inbound event hash and confirm the outbound result hash.

    Exit codes: 0 = verified, 1 = no receipt / incomplete, 2 = mismatch
    (tamper).
    """
    from bernstein.core.trigger_sources.webhook_node import verify_webhook_event

    root = Path(workdir).resolve()
    result = verify_webhook_event(
        workdir=root,
        lineage_root=_lineage_root(root),
        hmac_key=_load_hmac_key(),
        event_id=event_id,
    )

    console.print()
    console.print(f"[bold]Webhook node verify[/bold] event={event_id}")
    if result.ok:
        console.print(f"  inbound  {'ok' if result.inbound_ok else 'FAIL'}")
        console.print(f"  outbound {'ok' if result.outbound_ok else 'FAIL'}")
        console.print("[green]OK[/green] -- inbound event and outbound result verify against the journal.")
        raise SystemExit(0)
    if result.receipt is None:
        console.print(f"[yellow]NO RECEIPT[/yellow] -- {result.reason}")
        raise SystemExit(1)
    if result.inbound_ok and result.outbound is None:
        console.print(f"[yellow]INCOMPLETE[/yellow] -- {result.reason}")
        raise SystemExit(1)
    console.print(f"[red]MISMATCH[/red] -- {result.reason}")
    raise SystemExit(2)
