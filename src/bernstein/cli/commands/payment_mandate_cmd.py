"""``bernstein payment-mandate``: signed spend mandates + chain-anchored receipts.

Issue #2612. Bernstein never moves money; this command group issues an operator
authorization (a signed :class:`~bernstein.core.payments.mandate.SpendMandate`),
records every transaction attempt under it as a chain-anchored
:class:`~bernstein.core.payments.receipt.TransactionReceipt`, and verifies a
receipt entirely offline against the lineage signature and the audit chain:

    bernstein payment-mandate issue  --presence-mode delegated --max-amount 100.00 \\
        --currency USD --recipient vendor:acme --not-after <unix>
    bernstein payment-mandate show   <mandate_hash>
    bernstein payment-mandate spend  --mandate <hash> --amount 20.00 --to vendor:acme
    bernstein payment-mandate verify --receipt <hash>

The metered-gateway settlement path (#2528) is a concrete adapter over this
scheme-agnostic surface; no external payment product is bundled here.
"""

from __future__ import annotations

import secrets
import time
from pathlib import Path

import click

from bernstein.cli.helpers import console


def _audit_key() -> bytes:
    from bernstein.core.security.audit import load_or_create_audit_key

    return load_or_create_audit_key()


def _keystore_dir(workdir: Path) -> Path:
    return workdir / ".bernstein" / "keys"


@click.group("payment-mandate")
def payment_mandate_group() -> None:
    """Issue, inspect, spend under, and verify signed payment mandates.

    \b
      bernstein payment-mandate issue  --presence-mode delegated --max-amount 100.00 \\
          --currency USD --recipient vendor:acme --not-after 2000000000
      bernstein payment-mandate show   <mandate_hash>
      bernstein payment-mandate spend  --mandate <hash> --amount 20.00 --to vendor:acme
      bernstein payment-mandate verify --receipt <hash>
    """


_WORKDIR_OPTION = click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False),
    default=".",
    show_default=True,
    help="Project root holding .sdd/ and .bernstein/.",
)


@payment_mandate_group.command("issue")
@click.option(
    "--presence-mode",
    type=click.Choice(["human_present", "delegated"]),
    required=True,
    help="human_present binds a concrete amount+recipient; delegated binds a bounded envelope.",
)
@click.option("--max-amount", required=True, help="Cap (major units). For human_present, the exact amount.")
@click.option("--currency", required=True, help="ISO-4217-style uppercase code, e.g. USD.")
@click.option("--recipient", required=True, help="Opaque payee id (must be NFC).")
@click.option("--not-after", "not_after", required=True, type=int, help="Unix-seconds expiry.")
@click.option("--issued-at", "issued_at", type=int, default=None, help="Unix-seconds issuance (default: now).")
@click.option("--per-tx-cap", "per_tx_cap", default=None, help="Per-transaction cap (delegated only).")
@click.option("--allowed-category", "allowed_categories", multiple=True, help="Permitted category (repeatable).")
@click.option("--nonce", default=None, help="Uniqueness nonce (default: random).")
@_WORKDIR_OPTION
def issue_cmd(
    presence_mode: str,
    max_amount: str,
    currency: str,
    recipient: str,
    not_after: int,
    issued_at: int | None,
    per_tx_cap: str | None,
    allowed_categories: tuple[str, ...],
    nonce: str | None,
    workdir: str,
) -> None:
    """Issue an Ed25519-signed, content-addressed spend mandate."""
    from bernstein.core.payments._identity import load_operator_identity
    from bernstein.core.payments.enforce import save_mandate
    from bernstein.core.payments.mandate import SpendMandate

    root = Path(workdir).resolve()
    identity = load_operator_identity(_keystore_dir(root))
    try:
        mandate = SpendMandate.issue(
            private_key_pem=identity.private_pem,
            public_key_pem=identity.public_pem,
            kid=identity.kid,
            presence_mode=presence_mode,
            max_amount=max_amount,
            currency=currency,
            recipient=recipient,
            not_after=not_after,
            issued_at=issued_at if issued_at is not None else int(time.time()),
            nonce=nonce or secrets.token_hex(8),
            per_tx_cap=per_tx_cap,
            allowed_categories=tuple(allowed_categories) or None,
        )
    except ValueError as exc:
        console.print(f"[red]REFUSED[/red] -- {exc}")
        raise SystemExit(2) from exc

    save_mandate(root, mandate)
    verified = mandate.verify_signature()
    console.print()
    console.print("[bold]Payment mandate issued[/bold]")
    console.print(f"  mandate_hash   {mandate.mandate_hash()}")
    console.print(f"  presence_mode  {mandate.presence_mode}")
    console.print(f"  max_amount     {mandate.max_amount_nanos} nano-{mandate.currency}")
    console.print(f"  recipient      {mandate.recipient}")
    console.print(f"  not_after      {mandate.not_after}")
    if verified:
        console.print("[green]OK[/green] -- signature verifies offline.")
    else:  # pragma: no cover - defensive; a freshly signed mandate always verifies
        console.print("[red]FAIL[/red] -- signature did not verify.")
        raise SystemExit(1)


@payment_mandate_group.command("show")
@click.argument("mandate_hash", required=True)
@_WORKDIR_OPTION
def show_cmd(mandate_hash: str, workdir: str) -> None:
    """Show a stored mandate's scope and verify its signature offline.

    Exit codes: 0 = valid, 1 = signature invalid, 2 = not found.
    """
    from bernstein.core.payments.enforce import load_mandate

    root = Path(workdir).resolve()
    try:
        mandate = load_mandate(root, mandate_hash)
    except FileNotFoundError as exc:
        console.print(f"[yellow]NOT FOUND[/yellow] -- {exc}")
        raise SystemExit(2) from exc

    console.print()
    console.print(f"[bold]Payment mandate[/bold] hash={mandate_hash[:24]}")
    console.print(f"  presence_mode  {mandate.presence_mode}")
    console.print(f"  max_amount     {mandate.max_amount_nanos} nano-{mandate.currency}")
    if mandate.per_tx_cap_nanos is not None:
        console.print(f"  per_tx_cap     {mandate.per_tx_cap_nanos} nano-{mandate.currency}")
    console.print(f"  recipient      {mandate.recipient}")
    console.print(f"  not_after      {mandate.not_after}")
    if mandate.allowed_categories:
        console.print(f"  categories     {', '.join(mandate.allowed_categories)}")
    if mandate.verify_signature():
        console.print("[green]OK[/green] -- signature verifies offline.")
        raise SystemExit(0)
    console.print("[red]FAIL[/red] -- signature does not verify.")
    raise SystemExit(1)


@payment_mandate_group.command("spend")
@click.option("--mandate", "mandate_hash", required=True, help="Content hash of the mandate to spend under.")
@click.option("--amount", required=True, help="Transaction amount (major units).")
@click.option("--to", "recipient", required=True, help="Payee id (must match the mandate recipient).")
@click.option("--category", default="", help="Category label (must be NFC).")
@click.option(
    "--presence-mode",
    type=click.Choice(["human_present", "delegated"]),
    required=True,
    help="Mode the agent is transacting under; must match the mandate.",
)
@click.option("--currency", default=None, help="Currency (default: the mandate's).")
@click.option("--now", "now", type=int, default=None, help="Unix-seconds decision time (default: now).")
@click.option("--nonce", default=None, help="Per-receipt uniqueness nonce (default: random).")
@_WORKDIR_OPTION
def spend_cmd(
    mandate_hash: str,
    amount: str,
    recipient: str,
    category: str,
    presence_mode: str,
    currency: str | None,
    now: int | None,
    nonce: str | None,
    workdir: str,
) -> None:
    """Attempt a transaction under a mandate; emit an anchored receipt.

    Exit codes: 0 = authorized, 1 = refused (still recorded), 2 = mandate not
    found or malformed request.
    """
    from bernstein.core.payments._identity import load_operator_identity
    from bernstein.core.payments.enforce import (
        TransactionRequest,
        authorize,
        load_mandate,
    )
    from bernstein.core.payments.receipt import Decision
    from bernstein.core.security.audit_chain import AuditChainStore

    root = Path(workdir).resolve()
    key = _audit_key()
    try:
        mandate = load_mandate(root, mandate_hash)
    except FileNotFoundError as exc:
        console.print(f"[yellow]NOT FOUND[/yellow] -- {exc}")
        raise SystemExit(2) from exc

    identity = load_operator_identity(_keystore_dir(root))
    chain = AuditChainStore(root / ".sdd" / "audit", key=key)
    try:
        request = TransactionRequest.build(
            amount=amount,
            currency=currency or mandate.currency,
            recipient=recipient,
            category=category or "uncategorized",
            presence_mode=presence_mode,
            now=now if now is not None else int(time.time()),
        )
        receipt = authorize(
            request=request,
            mandate=mandate,
            workdir=root,
            hmac_key=key,
            identity=identity,
            chain=chain,
            nonce=nonce or secrets.token_hex(8),
        )
    except ValueError as exc:
        console.print(f"[red]REFUSED[/red] -- {exc}")
        raise SystemExit(2) from exc

    console.print()
    console.print("[bold]Transaction attempt[/bold]")
    console.print(f"  receipt_hash   {receipt.receipt_hash()}")
    console.print(f"  mandate_hash   {receipt.mandate_hash}")
    console.print(f"  amount         {receipt.amount_nanos} nano-{receipt.currency}")
    console.print(f"  presence_mode  {receipt.presence_mode}")
    if receipt.decision == Decision.AUTHORIZED.value:
        console.print("[green]AUTHORIZED[/green] -- receipt anchored in lineage + audit chain.")
        raise SystemExit(0)
    console.print(f"[red]REFUSED[/red] ({receipt.refusal_reason}) -- refusal receipt anchored.")
    raise SystemExit(1)


@payment_mandate_group.command("verify")
@click.option("--receipt", "receipt_hash", required=True, help="Content hash of the receipt to verify.")
@_WORKDIR_OPTION
def verify_cmd(receipt_hash: str, workdir: str) -> None:
    """Verify a receipt offline against the lineage signature and audit chain.

    Recomputes the lineage detached JWS and the audit-chain HMAC entirely
    offline, reports the bound scope, and fails if the receipt body, the mandate
    scope, the chain digest, or the signature substrate was altered or stripped.

    Exit codes: 0 = verified, 1 = verification failed, 2 = receipt/mandate not found.
    """
    from bernstein.core.payments.enforce import load_mandate
    from bernstein.core.payments.receipt import load_receipt, verify_receipt

    root = Path(workdir).resolve()
    key = _audit_key()
    try:
        receipt = load_receipt(root, receipt_hash)
        mandate = load_mandate(root, receipt.mandate_hash)
    except FileNotFoundError as exc:
        console.print(f"[yellow]NOT FOUND[/yellow] -- {exc}")
        raise SystemExit(2) from exc

    result = verify_receipt(workdir=root, hmac_key=key, receipt=receipt, mandate=mandate)

    console.print()
    console.print(f"[bold]Receipt verify[/bold] hash={receipt_hash[:24]}")
    console.print(f"  decision       {result.decision}")
    if result.refusal_reason:
        console.print(f"  refusal_reason {result.refusal_reason}")
    console.print("  checked scope:")
    console.print(f"    max_amount   {result.scope['max_amount_nanos']} nano-{result.scope['currency']}")
    console.print(f"    recipient    {result.scope['recipient']}")
    console.print(f"    not_after    {result.scope['not_after']}")
    console.print("  checks:")
    for name, passed in result.checks.items():
        mark = "[green]ok[/green]" if passed else "[red]FAIL[/red]"
        console.print(f"    {name:<18} {mark}")
    if result.ok:
        console.print("[green]OK[/green] -- receipt verifies offline against lineage + audit chain.")
        raise SystemExit(0)
    for err in result.errors:
        console.print(f"    [red]-[/red] {err}")
    console.print("[red]FAIL[/red] -- receipt verification failed.")
    raise SystemExit(1)
