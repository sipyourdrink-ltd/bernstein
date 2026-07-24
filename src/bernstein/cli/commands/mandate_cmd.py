"""``bernstein mandate``: verifiable spending mandates as consent receipts.

Issue #2306. Emits, verifies, and revokes AP2-style spending mandates whose
consent receipts are anchored in the lineage spine:

    bernstein mandate emit    --intent <file> --cart <file> --settlement <file>
    bernstein mandate verify  <mandate_hash> --intent <file> --cart <file>
    bernstein mandate revoke  <mandate_hash> --reason "..."

The intent, cart, and settlement inputs are canonical JSON files. Emit binds
the mandate, the actions it authorized, and the settlement reference into one
journal-anchored consent receipt; verify recomputes that binding offline; and
revoke appends a signed revocation so subsequent actions under the mandate are
refused.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import click

from bernstein.cli.helpers import console


def _load_hmac_key() -> bytes:
    from bernstein.core.security.audit import load_or_create_audit_key

    return load_or_create_audit_key()


def _lineage_root(workdir: Path) -> Path:
    return workdir / ".sdd" / "lineage"


def _audit_dir(workdir: Path) -> Path:
    return workdir / ".sdd" / "audit"


def _settlement_ref_hash(settlement: object) -> str:
    import hashlib

    from bernstein.core.protocols.payments.mandates import SettlementRef

    assert isinstance(settlement, SettlementRef)
    canonical = json.dumps(settlement.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode(
        "utf-8"
    )
    return "sha256:" + hashlib.sha256(canonical).hexdigest()


def _read_json(path: str) -> dict[str, object]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


@click.group("mandate")
def mandate_group() -> None:
    """Emit, verify, and revoke verifiable spending mandates.

    \b
      bernstein mandate emit --intent i.json --cart c.json --settlement s.json
      bernstein mandate verify <hash> --intent i.json --cart c.json
      bernstein mandate revoke <hash> --reason "budget change"
    """


@mandate_group.command("emit")
@click.option("--intent", "intent_file", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--cart", "cart_file", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--settlement", "settlement_file", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    show_default=True,
    help="Project root containing .sdd/.",
)
@click.option("--sign/--no-sign", default=True, help="Sign the mandates with the audit-chain key before binding.")
def mandate_emit_cmd(intent_file: str, cart_file: str, settlement_file: str, workdir: str, sign: bool) -> None:
    """Bind a mandate + authorized actions + settlement into a consent receipt.

    Exit codes: 0 = receipt anchored, 1 = refused (cap breach, revocation,
    signature, or intent-binding gate).
    """
    from bernstein.core.protocols.payments.mandates import (
        CartMandate,
        IntentMandate,
        MandateRefused,
        SettlementRef,
        emit_consent_receipt,
    )
    from bernstein.core.security.audit_chain import AuditChainStore, record_mandate_consent_receipt

    root = Path(workdir).resolve()
    key = _load_hmac_key()

    intent = IntentMandate.from_dict(_read_json(intent_file))
    cart = CartMandate.from_dict(_read_json(cart_file))
    settlement = SettlementRef.from_dict(_read_json(settlement_file))
    if sign:
        intent = intent.sign(key)
        cart = CartMandate(
            intent_hash=intent.mandate_hash(),
            tool_calls=cart.tool_calls,
            amount_usd=cart.amount_usd,
        ).sign(key)

    now = int(time.time())
    try:
        receipt = emit_consent_receipt(
            workdir=root,
            lineage_root=_lineage_root(root),
            hmac_key=key,
            intent=intent,
            cart=cart,
            settlement_ref=settlement,
            now=now,
        )
    except MandateRefused as exc:
        console.print(f"[red]REFUSED[/red] -- {exc}")
        raise SystemExit(1) from exc

    # The receipt is already anchored in the lineage spine above. The audit-
    # chain mirror is a best-effort second write; a failure here must not raise
    # post-anchor and orphan the operator with a traceback over a committed
    # receipt. Warn distinctly (naming the mandate hash) and continue.
    chain = AuditChainStore(_audit_dir(root), key=key)
    try:
        record_mandate_consent_receipt(
            chain=chain,
            mandate_hash=receipt.mandate_hash,
            intent_hash=receipt.intent_hash,
            authorized_tool_calls_hash=receipt.authorized_tool_calls_hash,
            settlement_ref_hash=_settlement_ref_hash(settlement),
            journal_entry_hash=receipt.journal_entry_hash,
            task_id=receipt.task_id,
        )
    except Exception as exc:  # best-effort mirror: never mask the anchored receipt
        console.print(
            f"[yellow]WARNING[/yellow] -- consent receipt anchored but audit-chain mirror "
            f"failed for {receipt.mandate_hash}: {exc}",
            soft_wrap=True,
        )

    console.print()
    console.print("[bold]Mandate emit[/bold]")
    console.print(f"  mandate_hash        {receipt.mandate_hash}")
    console.print(f"  intent_hash         {receipt.intent_hash}")
    console.print(f"  authorized_calls    {receipt.authorized_tool_calls_hash}")
    console.print(f"  journal_entry_hash  {receipt.journal_entry_hash}")
    console.print("[green]OK[/green] -- consent receipt anchored in the mandate spine.")


@mandate_group.command("verify")
@click.argument("mandate_hash", required=True)
@click.option("--intent", "intent_file", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option("--cart", "cart_file", required=True, type=click.Path(exists=True, dir_okay=False))
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    show_default=True,
    help="Project root containing .sdd/.",
)
def mandate_verify_cmd(mandate_hash: str, intent_file: str, cart_file: str, workdir: str) -> None:
    """Prove offline that *mandate_hash*'s action was authorized by the intent.

    Exit codes: 0 = verified, 1 = no receipt / bad input, 2 = mismatch.
    """
    from bernstein.core.protocols.payments.mandates import (
        CartMandate,
        IntentMandate,
        verify_consent_receipt,
    )

    root = Path(workdir).resolve()
    intent = IntentMandate.from_dict(_read_json(intent_file))
    cart = CartMandate.from_dict(_read_json(cart_file))

    result = verify_consent_receipt(
        workdir=root,
        lineage_root=_lineage_root(root),
        hmac_key=_load_hmac_key(),
        mandate_hash=mandate_hash,
        intent=intent,
        cart=cart,
    )
    console.print()
    console.print(f"[bold]Mandate verify[/bold] hash={mandate_hash[:24]}")
    if result.ok:
        console.print(f"  authorized_calls {list(result.authorized_tool_calls)}")
        console.print("[green]OK[/green] -- action was authorized by the recorded intent.")
        raise SystemExit(0)
    if result.receipt is None:
        console.print(f"[yellow]NO RECEIPT[/yellow] -- {result.reason}")
        raise SystemExit(1)
    console.print(f"[red]MISMATCH[/red] -- {result.reason}")
    raise SystemExit(2)


@mandate_group.command("revoke")
@click.argument("mandate_hash", required=True)
@click.option("--reason", default="", help="Human-readable revocation reason.")
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    show_default=True,
    help="Project root containing .sdd/.",
)
def mandate_revoke_cmd(mandate_hash: str, reason: str, workdir: str) -> None:
    """Append a signed revocation for *mandate_hash*.

    Subsequent actions under the revoked mandate are refused; the original
    mandate stays provable. Exit code 0 on success.
    """
    from bernstein.core.protocols.payments.mandates import revoke_mandate
    from bernstein.core.security.audit_chain import AuditChainStore, record_mandate_revocation

    root = Path(workdir).resolve()
    key = _load_hmac_key()
    entry = revoke_mandate(
        workdir=root,
        hmac_key=key,
        mandate_hash=mandate_hash,
        reason=reason,
        timestamp=int(time.time()),
    )
    # The revocation is already committed to the append-only ledger above. The
    # audit-chain mirror is a best-effort second write; a failure here must not
    # raise after the revocation is persisted. Warn distinctly (naming the
    # mandate hash) and continue.
    chain = AuditChainStore(_audit_dir(root), key=key)
    try:
        record_mandate_revocation(chain=chain, mandate_hash=entry.mandate_hash, reason=entry.reason)
    except Exception as exc:  # best-effort mirror: never mask the committed revocation
        console.print(
            f"[yellow]WARNING[/yellow] -- revocation committed but audit-chain mirror "
            f"failed for {entry.mandate_hash}: {exc}",
            soft_wrap=True,
        )

    console.print()
    console.print(f"[bold]Mandate revoke[/bold] hash={mandate_hash[:24]}")
    console.print("[green]OK[/green] -- signed revocation appended; further actions are refused.")
