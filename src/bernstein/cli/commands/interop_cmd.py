"""CLI surface for cross-organisation A2A interop.

Exposes the capability-card primitives as operator commands:

* ``bernstein interop a2a card --output card.json`` issues a signed
  capability card for the local orchestrator (identity, advertised tools,
  supported policies, public key, expiry).
* ``bernstein interop a2a verify --card card.json`` confirms a peer card is
  cryptographically valid and (optionally) meets the operator's required
  policies.

The signing key is generated fresh on ``card`` unless ``--private-key`` is
supplied; the private key is written next to the card (``<output>.key.pem``)
with ``0600`` permissions so the operator can re-issue without minting a new
identity. The card itself carries only the public key.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import click

from bernstein.cli.helpers import (
    console,
    is_json,
    print_error,
    print_json,
    print_success,
)
from bernstein.core.interop.a2a_card import (
    CardPolicies,
    SignedCapabilityCard,
    card_public_key_fingerprint,
    issue_capability_card,
    verify_capability_card,
)
from bernstein.core.interop.a2a_conformance import check_card_conformance
from bernstein.core.interop.a2a_consume import (
    PolicyRequirements,
    policies_meet_requirements,
)


@click.group("interop")
def interop_group() -> None:
    """Cross-organisation agent interoperability surfaces."""


@interop_group.group("a2a")
def a2a_group() -> None:
    """A2A capability cards: issue and verify signed manifests.

    \b
    Examples:
      bernstein interop a2a card --issuer acme --output card.json
      bernstein interop a2a verify --card card.json
    """


@a2a_group.command("card")
@click.option("--issuer", required=True, help="Stable issuer id (organisation / orchestrator).")
@click.option("--name", default="bernstein-orchestrator", show_default=True, help="Human-readable issuer name.")
@click.option(
    "--description",
    default="Bernstein multi-agent orchestration system",
    show_default=True,
    help="What the issuer does.",
)
@click.option(
    "--tool",
    "tools",
    multiple=True,
    help="Advertised tool name (repeatable). Defaults to a minimal set when omitted.",
)
@click.option("--cost-cap-usd", type=float, default=10.0, show_default=True, help="Advertised cost cap (USD).")
@click.option("--redaction-tier", default="standard", show_default=True, help="Advertised redaction tier.")
@click.option("--sandbox-profile", default="container", show_default=True, help="Advertised sandbox profile.")
@click.option(
    "--ttl-seconds",
    type=int,
    default=86400,
    show_default=True,
    help="Card validity window in seconds (0 disables expiry).",
)
@click.option(
    "--private-key",
    "private_key_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Existing PKCS#8 Ed25519 PEM private key to sign with. Generated when omitted.",
)
@click.option(
    "--output",
    "output_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=Path("card.json"),
    show_default=True,
    help="Where to write the signed capability card JSON.",
)
def card(
    issuer: str,
    name: str,
    description: str,
    tools: tuple[str, ...],
    cost_cap_usd: float,
    redaction_tier: str,
    sandbox_profile: str,
    ttl_seconds: int,
    private_key_path: Path | None,
    output_path: Path,
) -> None:
    """Issue a signed A2A capability card for the local orchestrator."""
    advertised_tools = list(tools) or ["task_orchestration", "code_review"]
    policies = CardPolicies(
        cost_cap_usd=cost_cap_usd,
        redaction_tier=redaction_tier,
        sandbox_profile=sandbox_profile,
    )

    private_key_pem = private_key_path.read_bytes() if private_key_path is not None else None

    signed, used_private_key = issue_capability_card(
        issuer=issuer,
        name=name,
        description=description,
        advertised_tools=advertised_tools,
        policies=policies,
        private_key_pem=private_key_pem,
        ttl_seconds=ttl_seconds,
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(signed.to_json())

    fingerprint = card_public_key_fingerprint(signed.card.public_key_pem)

    # Persist the freshly generated private key so the operator can re-issue.
    key_path: Path | None = None
    if private_key_path is None:
        key_path = output_path.with_suffix(output_path.suffix + ".key.pem")
        key_path.write_bytes(used_private_key)
        os.chmod(key_path, 0o600)

    if is_json():
        print_json(
            {
                "output": str(output_path),
                "issuer": issuer,
                "kid": signed.card.kid,
                "fingerprint": fingerprint,
                "expires_at": signed.card.expires_at,
                "private_key": str(key_path) if key_path else None,
            }
        )
        return

    print_success(f"Capability card written to {output_path}")
    console.print(f"  issuer: [bold]{issuer}[/bold]  kid: {signed.card.kid}")
    console.print(f"  fingerprint: {fingerprint}")
    if key_path is not None:
        console.print(f"  private key (keep safe, 0600): {key_path}")


@a2a_group.command("verify")
@click.option(
    "--card",
    "card_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Path to the signed capability card JSON to verify.",
)
@click.option(
    "--trusted-fingerprint",
    "trusted_fingerprints",
    multiple=True,
    help="Trusted issuer key fingerprint (repeatable). When supplied the card key must match one.",
)
@click.option("--require-cost-cap-usd", type=float, default=None, help="Reject cards advertising a higher cost cap.")
@click.option("--require-redaction-tier", default=None, help="Reject cards with a weaker redaction tier.")
@click.option("--require-sandbox-profile", default=None, help="Reject cards with a weaker sandbox profile.")
def verify(
    card_path: Path,
    trusted_fingerprints: tuple[str, ...],
    require_cost_cap_usd: float | None,
    require_redaction_tier: str | None,
    require_sandbox_profile: str | None,
) -> None:
    """Verify a peer capability card; exit non-zero when it is not valid."""
    try:
        signed = SignedCapabilityCard.from_json(card_path.read_text())
    except (ValueError, json.JSONDecodeError) as exc:
        _fail(f"could not parse capability card: {exc}")
        return

    failures: list[str] = []

    if not verify_capability_card(signed, check_expiry=True):
        failures.append("signature is invalid or the card has expired")

    fingerprint = card_public_key_fingerprint(signed.card.public_key_pem)
    if trusted_fingerprints and fingerprint not in set(trusted_fingerprints):
        failures.append(f"key fingerprint {fingerprint} is not in the trusted-issuer set")

    if require_cost_cap_usd is not None or require_redaction_tier is not None or require_sandbox_profile is not None:
        requirements = PolicyRequirements(
            max_cost_cap_usd=require_cost_cap_usd if require_cost_cap_usd is not None else float("inf"),
            min_redaction_tier=require_redaction_tier or "none",
            min_sandbox_profile=require_sandbox_profile or "none",
        )
        verdict = policies_meet_requirements(signed.card.policies, requirements)
        failures.extend(verdict.failures)

    if failures:
        if is_json():
            print_json({"ok": False, "fingerprint": fingerprint, "failures": failures})
        else:
            print_error(f"Capability card {card_path} is NOT valid:", soft_wrap=True)
            for reason in failures:
                console.print(f"  - {reason}")
        sys.exit(1)

    if is_json():
        print_json(
            {
                "ok": True,
                "issuer": signed.card.issuer,
                "kid": signed.card.kid,
                "fingerprint": fingerprint,
                "expires_at": signed.card.expires_at,
            }
        )
        return
    print_success(f"Capability card {card_path} is valid", soft_wrap=True)
    console.print(f"  issuer: [bold]{signed.card.issuer}[/bold]  kid: {signed.card.kid}")
    console.print(f"  fingerprint: {fingerprint}")


@a2a_group.command("verify-thread")
@click.option(
    "--from-thread",
    "from_thread",
    required=True,
    help="The A2A task uuid (thread ref) whose message receipts to verify.",
)
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path("."),
    show_default=True,
    help="Project root containing .sdd/.",
)
def verify_thread_cmd(from_thread: str, workdir: Path) -> None:
    """Prove a cross-agent thread equals the executed actions offline (#2304).

    \b
    For the given task uuid, recompute every A2A message receipt binding,
    re-check each Ed25519 signature offline, verify the message-receipt spine,
    re-anchor each receipt against it, and confirm every message hash is
    referenced by the seeded per-task journal. A tampered receipt, spine, or
    journal fails the check. Exit codes: 0 = verified, 1 = no thread / mismatch.
    """
    from bernstein.core.interop.a2a_lineage import verify_thread
    from bernstein.core.security.audit import load_or_create_audit_key

    root = workdir.resolve()
    result = verify_thread(
        workdir=root,
        lineage_root=root / ".sdd" / "lineage",
        hmac_key=load_or_create_audit_key(),
        task_uuid=from_thread,
    )

    if is_json():
        print_json(
            {
                "ok": result.ok,
                "task_uuid": result.task_uuid,
                "message_count": result.message_count,
                "reason": result.reason,
            }
        )
        if not result.ok:
            sys.exit(1)
        return

    if result.ok:
        print_success(
            f"A2A thread {from_thread} verifies: {result.message_count} message(s) equal the executed actions",
            soft_wrap=True,
        )
        return
    print_error(f"A2A thread {from_thread} is NOT verified: {result.reason}", soft_wrap=True)
    sys.exit(1)


@a2a_group.command("conformance")
@click.option(
    "--card",
    "card_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Path to the signed capability card JSON to self-check.",
)
def conformance(card_path: Path) -> None:
    """Self-check that a card round-trips and verifies offline.

    \b
    Runs, in order: required-field validation, RFC 8785 JCS
    canonicalisation, detached Ed25519 JWS signature verification,
    expiry, and issuer. Prints a per-check pass/fail report and exits
    non-zero if any check fails. This proves the card the operator emits
    verifies the same way a peer will check it, with no network access.
    """
    try:
        signed = SignedCapabilityCard.from_json(card_path.read_text())
    except (ValueError, json.JSONDecodeError) as exc:
        _fail(f"could not parse capability card: {exc}")
        return

    report = check_card_conformance(signed)

    if is_json():
        print_json({"card": str(card_path), **report.to_dict()})
        if not report.ok:
            sys.exit(1)
        return

    if report.ok:
        print_success(f"Capability card {card_path} passes all conformance checks", soft_wrap=True)
    else:
        print_error(f"Capability card {card_path} FAILED conformance:", soft_wrap=True)
    console.print(f"  issuer: [bold]{report.issuer}[/bold]  kid: {report.kid}")
    if report.fingerprint:
        console.print(f"  fingerprint: {report.fingerprint}")
    for check in report.checks:
        marker = "[green]PASS[/green]" if check.passed else "[red]FAIL[/red]"
        console.print(f"  {marker} {check.name}: {check.detail}")

    if not report.ok:
        sys.exit(1)


def _fail(message: str) -> None:
    """Print an error (JSON-aware) and exit non-zero."""
    if is_json():
        print_json({"ok": False, "error": message})
    else:
        print_error(message)
    sys.exit(1)


__all__ = ["a2a_group", "card", "conformance", "interop_group", "verify", "verify_thread_cmd"]
