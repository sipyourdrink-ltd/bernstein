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
import time
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
from bernstein.core.interop.a2a_conformance import (
    check_agent_card_v1_conformance,
    check_card_conformance,
)
from bernstein.core.interop.a2a_conformance import (
    report_hash as _v1_report_hash,
)
from bernstein.core.interop.a2a_consume import (
    PolicyRequirements,
    policies_meet_requirements,
    verify_inbound_agent_card_v1,
)

#: Canonical on-disk location for the install's A2A message/verdict signing
#: identity, so verdict receipts and message receipts share one signer.
_A2A_IDENTITY_SUBPATH = (".sdd", "a2a-identity")


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
    default="Bernstein, a deterministic orchestrator for CLI coding agents",
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
    default=Path(),
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
    default=None,
    help="Path to a signed capability card JSON to self-check (a2a-capability+jws profile).",
)
@click.option(
    "--agent-card",
    "agent_card_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Path to an A2A v1.0 agent-card JSON to run the v1.0 conformance profile over.",
)
@click.option(
    "--agent-card-url",
    "agent_card_url",
    default=None,
    help="Fetch the A2A v1.0 agent card over HTTP (e.g. https://host/.well-known/agent.json).",
)
@click.option(
    "--jwks",
    "jwks_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="JWKS JSON for the v1.0 agent card (required with --agent-card).",
)
@click.option(
    "--jwks-url",
    "jwks_url",
    default=None,
    help="Fetch the JWKS over HTTP (defaults to <agent-card-url>/keys).",
)
@click.option("--now", type=float, default=None, help="Deterministic evaluation timestamp (Unix seconds).")
@click.option(
    "--trusted-fingerprint",
    "trusted_fingerprints",
    multiple=True,
    help="Trusted issuer key fingerprint (repeatable). When supplied the inbound trust gate runs too.",
)
@click.option("--anchor", is_flag=True, default=False, help="Anchor the verdict as a signed receipt in the chain.")
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path(),
    show_default=True,
    help="Project root containing .sdd/ (used with --anchor).",
)
@click.option("--task-ref", "task_ref", default="conformance", show_default=True, help="Thread ref for the receipt.")
def conformance(
    card_path: Path | None,
    agent_card_path: Path | None,
    agent_card_url: str | None,
    jwks_path: Path | None,
    jwks_url: str | None,
    now: float | None,
    trusted_fingerprints: tuple[str, ...],
    anchor: bool,
    workdir: Path,
    task_ref: str,
) -> None:
    """Run a card conformance suite and (optionally) anchor the verdict.

    \b
    Two profiles:
      * --card <file>            capability-card self-check (a2a-capability+jws).
      * --agent-card <file> --jwks <file>   A2A v1.0 agent-card profile, or
        --agent-card-url <url>   to fetch our own or a remote card over HTTP.

    The v1.0 profile checks, in order: required v1.0 fields, signatures[]
    shape, RFC 8785 JCS canonicalisation, JWS header (typ/alg/kid), kid
    resolution against the JWKS (incl. rotation grace), detached JWS
    signature, and expiry. The report is deterministic in (card, jwks, now);
    with --anchor it is bound into a signed receipt that recomputes offline.
    Exits non-zero when any check fails (or the trust gate rejects the card).
    """
    v1_selected = agent_card_path is not None or agent_card_url is not None
    if card_path is not None and v1_selected:
        _fail("pass either --card (capability) or --agent-card/--agent-card-url (v1.0), not both")
        return
    if v1_selected:
        _run_v1_conformance(
            agent_card_path=agent_card_path,
            agent_card_url=agent_card_url,
            jwks_path=jwks_path,
            jwks_url=jwks_url,
            now=now,
            trusted_fingerprints=trusted_fingerprints,
            anchor=anchor,
            workdir=workdir,
            task_ref=task_ref,
        )
        return
    if card_path is None:
        _fail("provide --card, --agent-card, or --agent-card-url")
        return

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


def _load_json_source(path: Path | None, url: str | None, *, what: str) -> dict:
    """Return parsed JSON from a file path or an HTTP URL."""
    if path is not None:
        return json.loads(path.read_text())
    if url is not None:
        import httpx

        resp = httpx.get(url, timeout=10.0, follow_redirects=True)
        resp.raise_for_status()
        return resp.json()
    raise click.UsageError(f"no source supplied for {what}")


def _run_v1_conformance(
    *,
    agent_card_path: Path | None,
    agent_card_url: str | None,
    jwks_path: Path | None,
    jwks_url: str | None,
    now: float | None,
    trusted_fingerprints: tuple[str, ...],
    anchor: bool,
    workdir: Path,
    task_ref: str,
) -> None:
    """Run the A2A v1.0 agent-card profile and optionally anchor the verdict."""
    if jwks_url is None and agent_card_url is not None:
        jwks_url = agent_card_url.rstrip("/") + "/keys"
    try:
        payload = _load_json_source(agent_card_path, agent_card_url, what="agent card")
        jwks = _load_json_source(jwks_path, jwks_url, what="JWKS")
    except Exception as exc:  # surface any load / parse / transport error as a clean failure
        _fail(f"could not load agent card or JWKS: {exc}")
        return

    report = check_agent_card_v1_conformance(payload, jwks=jwks, now=now)
    r_hash = _v1_report_hash(report)

    # Decide accept/reject. When trusted fingerprints are supplied, run the full
    # inbound trust gate; otherwise the verdict is the conformance verdict.
    if trusted_fingerprints:
        verdict = verify_inbound_agent_card_v1(
            payload, jwks=jwks, trusted_issuer_fingerprints=list(trusted_fingerprints), now=now
        )
        accepted = verdict.accepted
        decision = "accept" if accepted else "reject"
        reason_code = verdict.reason_code
    else:
        accepted = report.ok
        decision = "accept" if accepted else "reject"
        reason_code = ""
        if not accepted:
            reason_code = next((c.name for c in report.checks if not c.passed), "malformed")

    anchored_receipt: dict | None = None
    if anchor:
        anchored_receipt = _anchor_v1_verdict(
            workdir=workdir,
            task_ref=task_ref,
            decision=decision,
            issuer=report.issuer,
            fingerprint=report.fingerprint,
            reason_code=reason_code,
            report_hash=r_hash,
            now=now,
        )

    result = {
        "profile": "a2a-agent-card-v1",
        "accepted": accepted,
        "decision": decision,
        "reason_code": reason_code,
        "report_hash": r_hash,
        **report.to_dict(),
    }
    if anchored_receipt is not None:
        result["receipt"] = anchored_receipt

    if is_json():
        print_json(result)
        if not accepted:
            sys.exit(1)
        return

    if accepted:
        print_success("A2A v1.0 agent card passes conformance" + (" and trust gate" if trusted_fingerprints else ""))
    else:
        print_error(f"A2A v1.0 agent card REJECTED ({reason_code}):", soft_wrap=True)
    console.print(f"  issuer: [bold]{report.issuer}[/bold]  kid: {report.kid}")
    if report.fingerprint:
        console.print(f"  fingerprint: {report.fingerprint}")
    console.print(f"  report_hash: {r_hash}")
    for check in report.checks:
        marker = "[green]PASS[/green]" if check.passed else "[red]FAIL[/red]"
        console.print(f"  {marker} {check.name}: {check.detail}")
    if anchored_receipt is not None:
        console.print(f"  anchored verdict receipt: {anchored_receipt['journal_entry_hash']}")
    if not accepted:
        sys.exit(1)


def _anchor_v1_verdict(
    *,
    workdir: Path,
    task_ref: str,
    decision: str,
    issuer: str,
    fingerprint: str,
    reason_code: str,
    report_hash: str,
    now: float | None,
) -> dict:
    """Anchor a conformance verdict as a signed receipt; return its dict view."""
    from bernstein.core.interop.a2a_lineage import record_card_verdict
    from bernstein.core.security.audit import load_or_create_audit_key

    root = workdir.resolve()
    return record_card_verdict(
        workdir=root,
        lineage_root=root / ".sdd" / "lineage",
        hmac_key=load_or_create_audit_key(),
        identity_dir=root.joinpath(*_A2A_IDENTITY_SUBPATH),
        task_ref=task_ref,
        decision=decision,
        issuer=issuer,
        peer_card_fingerprint=fingerprint,
        reason_code=reason_code,
        report_hash=report_hash,
        timestamp=int(now) if now is not None else int(time.time()),
    ).to_dict()


@a2a_group.command("verify-verdict")
@click.option("--task-ref", "task_ref", required=True, help="The verdict thread ref to verify offline.")
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path(),
    show_default=True,
    help="Project root containing .sdd/.",
)
def verify_verdict_cmd(task_ref: str, workdir: Path) -> None:
    """Prove an anchored card-verdict thread verifies offline (#2525).

    \b
    Re-checks every verdict receipt's Ed25519 signature, verifies the verdict
    spine, and re-anchors each receipt against it. Exit codes: 0 = verified,
    1 = no thread / mismatch.
    """
    from bernstein.core.interop.a2a_lineage import verify_card_verdict
    from bernstein.core.security.audit import load_or_create_audit_key

    root = workdir.resolve()
    result = verify_card_verdict(
        workdir=root,
        lineage_root=root / ".sdd" / "lineage",
        hmac_key=load_or_create_audit_key(),
        task_ref=task_ref,
    )
    if is_json():
        print_json(
            {
                "ok": result.ok,
                "task_ref": result.task_ref,
                "verdict_count": result.verdict_count,
                "reason": result.reason,
            }
        )
        if not result.ok:
            sys.exit(1)
        return
    if result.ok:
        print_success(
            f"Card verdict thread {task_ref} verifies: {result.verdict_count} verdict(s) anchored and signed",
            soft_wrap=True,
        )
        return
    print_error(f"Card verdict thread {task_ref} is NOT verified: {result.reason}", soft_wrap=True)
    sys.exit(1)


def _fail(message: str) -> None:
    """Print an error (JSON-aware) and exit non-zero."""
    if is_json():
        print_json({"ok": False, "error": message})
    else:
        print_error(message)
    sys.exit(1)


__all__ = [
    "a2a_group",
    "card",
    "conformance",
    "interop_group",
    "verify",
    "verify_thread_cmd",
    "verify_verdict_cmd",
]
