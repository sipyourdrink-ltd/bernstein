"""``bernstein delegation`` - verify delegation chains offline.

Two distinct surfaces:

* ``delegation verify <run>`` reconstructs the per-hop HMAC *receipt* chain for
  a run (the ACT log: which principal authorized which sub-agent action, see
  :mod:`bernstein.core.identity.delegation`), exiting non-zero on any tamper,
  deleted hop, or missing chain (issue #2305 AC4). For hops that record their
  effective scope it also recomputes child-subset-of-parent narrowing,
  separation of duties, and decision-time charter binding, exiting non-zero
  when any of those relations is violated (issue #2554). Exit codes are
  0 pass, 1 fail, 3 unproven; 2 is left to click for usage errors. A chain
  that records no scope at all is unproven, not a pass, so a legacy chain
  that used to exit 0 now exits 3.
* ``delegation verify-token <token-file>`` verifies a signed *authority* token
  chain (see :mod:`bernstein.core.security.capability_tokens`): per-hop
  signature, structural linkage, identity/pubkey continuity, monotonic
  attenuation, and root trust-anchor membership - fully offline, no network and
  no registry - printing pass/fail per hop and exiting non-zero on any failing
  hop (issue #2611).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypedDict

import click

from bernstein.cli.helpers import console
from bernstein.core.identity import delegation
from bernstein.core.identity.delegation_scope import VERDICT_FAIL, VERDICT_UNPROVEN


class _ReceiptRecord(TypedDict):
    """One delegation receipt as emitted on the ``--json`` boundary."""

    hop_index: int
    issuer: str
    subject: str
    audience: str
    act: str
    parent_ref: str | None
    scope_ref: str | None
    binding: dict[str, object] | None


class _VerifyPayload(TypedDict):
    """The ``delegation verify --json`` response contract."""

    run: str
    valid: bool
    chain_ok: bool
    hops: int
    errors: list[str]
    authority: dict[str, object]
    verdict: dict[str, object]
    receipts: list[_ReceiptRecord]


@click.group(name="delegation")
def delegation_group() -> None:
    """Delegation-receipt tooling for the principal->orchestrator->sub-agent chain.

    \b
    Examples:
      bernstein delegation verify run-42
      bernstein delegation verify run-42 --json
      bernstein delegation verify-token chain.json --trust-anchor principal.pem
    """


def _verify_exit_code(result: delegation.ChainResult) -> int:
    """0 pass, 1 fail, 3 unproven. 2 belongs to click's usage errors.

    ``valid`` still decides the failing case on its own, so nothing that
    exited 1 before this graded surface existed exits 0 now. The one changed
    outcome is the reverse: a chain that recorded no scope to check used to
    exit 0 and now exits 3.
    """
    if not result.valid or result.verdict.verdict == VERDICT_FAIL:
        return 1
    if result.verdict.verdict == VERDICT_UNPROVEN:
        return 3
    return 0


@delegation_group.command("verify")
@click.argument("run")
@click.option(
    "--root",
    type=click.Path(file_okay=False, dir_okay=True, path_type=Path),
    default=None,
    help="Delegation-receipt root (default: .sdd/audit).",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit machine-readable JSON.")
def verify_cmd(run: str, root: Path | None, as_json: bool) -> None:
    """Reconstruct and verify the delegation chain for RUN.

    Checks tamper evidence (linkage plus HMAC) and, for hops that record their
    effective scope, recomputes three distinct authority relations: that each
    child scope is a subset of its parent's, that no principal exercised
    separated duties (spawn / approve / merge), and that gated decisions cite
    the charter version in force at decision time.

    Exits 0 when the chain is intact and narrowing was checked and held, 1
    when a check failed, and 3 when the receipts carry too little to decide.
    Unproven is not a pass: a chain that recorded no scope reaches 3, not 0.
    """
    result = delegation.verify_run(run, root=root)
    authority = result.authority

    if as_json:
        receipt_records: list[_ReceiptRecord] = [
            {
                "hop_index": r.hop_index,
                "issuer": r.issuer,
                "subject": r.subject,
                "audience": r.audience,
                "act": r.act,
                "parent_ref": r.parent_ref,
                "scope_ref": r.scope_ref,
                "binding": r.binding,
            }
            for r in result.receipts
        ]
        payload: _VerifyPayload = {
            "run": run,
            "valid": result.valid,
            "chain_ok": result.chain_ok,
            "hops": result.hops,
            "errors": result.errors,
            "authority": authority.to_dict(),
            "verdict": result.verdict.to_dict(),
            "receipts": receipt_records,
        }
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
        raise SystemExit(_verify_exit_code(result))

    if result.hops == 0:
        console.print(f"[red]No delegation receipts for run[/red] {run}")
        raise SystemExit(1)

    for r in result.receipts:
        console.print(f"  hop {r.hop_index}: {r.issuer} -> {r.audience}  [dim]({r.act})[/dim]")
        if r.scope_ref:
            console.print(f"[dim]      scope {r.scope_ref}[/dim]")
        if r.binding:
            bits = ", ".join(f"{k}={v}" for k, v in sorted(r.binding.items()))
            console.print(f"[dim]      binding {bits}[/dim]")

    console.print(f"[dim]scope coverage:[/dim] {authority.scope_coverage}")
    for row in result.verdict.hops:
        console.print(f"[dim]  {row}[/dim]")
    for violation in authority.violations:
        console.print(f"[red]  {violation}[/red]")
    for err in result.errors:
        console.print(f"[red]  {err}[/red]")

    code = _verify_exit_code(result)
    if code == 0:
        console.print(f"[green]delegation chain intact[/green] ({result.hops} hop(s))")
        raise SystemExit(0)
    if code == 3:
        detail = f", {', '.join(result.verdict.reasons)}" if result.verdict.reasons else ""
        console.print(
            f"[yellow]delegation chain unproven[/yellow]: "
            f"{result.verdict.unproven_hops} of {result.hops} hop(s) could not be checked"
            f"{detail}"
        )
        raise SystemExit(3)
    if not result.chain_ok:
        console.print("[red]delegation chain verification failed[/red]")
    else:
        failed = [
            name
            for name, ok in (
                ("narrowing", authority.narrowing_ok),
                ("separation-of-duties", authority.duties_ok),
                ("decision binding", authority.binding_ok),
            )
            if not ok
        ]
        if failed:
            console.print(f"[red]delegation authority verification failed:[/red] {', '.join(failed)}")
        else:
            console.print("[red]delegation chain verdict: fail[/red]")
    raise SystemExit(1)


@delegation_group.command("verify-token")
@click.argument(
    "token_file",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--trust-anchor",
    "trust_anchor_files",
    multiple=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Trusted root-issuer public-key PEM (repeatable). Defaults to the local agent-card keystore public key.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit machine-readable JSON.")
def verify_token_cmd(token_file: Path, trust_anchor_files: tuple[Path, ...], as_json: bool) -> None:
    """Verify a signed capability-token chain in TOKEN_FILE, fully offline.

    Prints per-hop pass/fail and the resolved principal -> ... -> leaf authority
    path. Exits 0 only when every hop verifies (signature, linkage, continuity,
    attenuation, and a trusted root); 1 on any failing hop, an empty chain, or an
    unreadable token file.
    """
    from bernstein.core.security import capability_tokens as ct

    try:
        chain = ct.CapabilityChain.from_json(token_file.read_text(encoding="utf-8"))
    except (ValueError, KeyError, json.JSONDecodeError) as exc:
        console.print(f"[red]unreadable token file:[/red] {exc}")
        raise SystemExit(1) from exc

    anchors: set[str] = {p.read_text(encoding="utf-8") for p in trust_anchor_files}
    if not anchors:
        anchors = _keystore_trust_anchors()

    result = ct.verify_chain(chain, trust_anchors=anchors)

    if as_json:
        payload = {
            "valid": result.valid,
            "principal_path": result.principal_path,
            "hops": [
                {
                    "hop_index": h.hop_index,
                    "issuer": h.issuer_identity_id,
                    "subject": h.subject_identity_id,
                    "ok": h.ok,
                    "errors": h.errors,
                }
                for h in result.hops
            ],
        }
        click.echo(json.dumps(payload, indent=2, sort_keys=True))
        raise SystemExit(0 if result.valid else 1)

    if not result.hops:
        console.print("[red]empty token chain[/red]")
        raise SystemExit(1)

    for hop in result.hops:
        marker = "[green]PASS[/green]" if hop.ok else "[red]FAIL[/red]"
        console.print(f"  hop {hop.hop_index}: {hop.issuer_identity_id} -> {hop.subject_identity_id}  {marker}")
        for err in hop.errors:
            console.print(f"[red]      {err}[/red]")

    console.print(f"[dim]authority path:[/dim] {' -> '.join(result.principal_path)}")
    if result.valid:
        console.print(f"[green]capability chain verified[/green] ({len(result.hops)} hop(s))")
        raise SystemExit(0)
    console.print("[red]capability chain verification failed[/red]")
    raise SystemExit(1)


def _keystore_trust_anchors() -> set[str]:
    """Return the local agent-card keystore public key as the default anchor set.

    The root issuer of a locally-minted chain is the install identity, so the
    keystore's current public key is the natural default trust anchor. Returns an
    empty set when no keystore is available - the root-anchor check then fails
    loudly rather than trusting an unknown root.
    """
    try:
        from bernstein.core.security.agent_card_keystore import AgentCardKeystore

        _priv, public_pem = AgentCardKeystore().load_or_generate()
    except Exception:
        # A missing / unreadable keystore is non-fatal: fall back to an empty
        # anchor set so the root-anchor check fails loudly rather than trusting
        # an unknown root.
        return set()
    return {public_pem.decode("ascii")}
