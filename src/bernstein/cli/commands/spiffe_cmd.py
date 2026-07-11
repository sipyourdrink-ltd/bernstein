"""``bernstein spiffe`` - SPIFFE-compatible workload identity helpers (#2363).

Subcommands:

* ``bernstein spiffe id`` - derive the deterministic SPIFFE ID for an install
  and agent (``spiffe://<trust-domain>/bernstein/<install>/<agent>``). Offline,
  pure, no network.
* ``bernstein spiffe verify-binding`` - re-derive the SPIFFE ID from the install
  public key and confirm a card-to-SVID binding receipt, optionally checking it
  against the ``spiffe.svid_binding`` event in the HMAC-chained audit log so the
  mapping between the SVID and the card is proven from the chain alone.

The commands are read-only and never open a network connection. Fetching an
SVID from a SPIRE agent requires the optional ``spiffe`` extra and lives in
:mod:`bernstein.core.identity.spiffe.workload_api`, not here, so this CLI stays
importable in the default self-contained install.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from bernstein.core.identity.spiffe import (
    SpiffeIdError,
    SvidBinding,
    derive_spiffe_id_from_key,
    verify_binding,
    verify_binding_against_event,
)


@click.group(name="spiffe")
def spiffe_group() -> None:
    """SPIFFE-compatible workload identity helpers.

    \b
    Maps the Ed25519 install identity and agent cards onto SPIFFE IDs and
    verifies card-to-SVID bindings from the audit chain. The self-contained
    Ed25519 path stays the default; SPIRE is an optional integration profile
    (pip install 'bernstein[spiffe]').

    \b
    Examples:
      bernstein spiffe id --install-key .bernstein/keys/agent-card.ed25519.pub \\
          --agent backend-1 --trust-domain example.org
      bernstein spiffe verify-binding binding.json \\
          --install-key agent-card.ed25519.pub --trust-domain example.org \\
          --audit-dir .sdd/audit
    """


@spiffe_group.command("id")
@click.option(
    "--install-key",
    "install_key",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to the install public key PEM (SPKI Ed25519).",
)
@click.option("--agent", "agent_id", required=True, help="Agent card id.")
@click.option("--trust-domain", "trust_domain", required=True, help="SPIFFE trust domain.")
def id_cmd(install_key: Path, agent_id: str, trust_domain: str) -> None:
    """Derive and print the deterministic SPIFFE ID for an install and agent."""
    pub = install_key.read_bytes()
    try:
        sid = derive_spiffe_id_from_key(trust_domain=trust_domain, install_public_key_pem=pub, agent_id=agent_id)
    except SpiffeIdError as exc:
        click.echo(f"invalid SPIFFE ID components: {exc}", err=True)
        raise SystemExit(1) from exc
    click.echo(sid)


@spiffe_group.command("verify-binding")
@click.argument("binding_file", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@click.option(
    "--install-key",
    "install_key",
    required=True,
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    help="Path to the install public key PEM used to re-derive the SPIFFE ID.",
)
@click.option("--trust-domain", "trust_domain", required=True, help="SPIFFE trust domain.")
@click.option(
    "--audit-dir",
    "audit_dir",
    default=None,
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    help="Audit-log directory; when given, the binding is also checked against its chained receipt.",
)
def verify_binding_cmd(binding_file: Path, install_key: Path, trust_domain: str, audit_dir: Path | None) -> None:
    """Verify a card-to-SVID binding receipt.

    Exits 0 when the binding is internally consistent (and, when ``--audit-dir``
    is given, matches its chained receipt), 1 otherwise.
    """
    binding = SvidBinding.from_dict(json.loads(binding_file.read_text(encoding="utf-8")))
    pub = install_key.read_bytes()

    ok, reason = verify_binding(binding=binding, install_public_key_pem=pub, trust_domain=trust_domain)
    if not ok:
        click.echo(f"invalid: {reason}", err=True)
        raise SystemExit(1)

    if audit_dir is not None:
        matched = _verify_against_chain(binding, audit_dir)
        if not matched:
            click.echo("invalid: no matching chained spiffe.svid_binding receipt", err=True)
            raise SystemExit(1)
        click.echo("valid (chain-anchored)")
        return
    click.echo("valid")


def _verify_against_chain(binding: SvidBinding, audit_dir: Path) -> bool:
    """Return True when *binding* matches a chained ``spiffe.svid_binding`` event."""
    from bernstein.core.security.audit_chain import (
        EVENT_SPIFFE_SVID_BINDING,
        AuditChainStore,
    )

    chain = AuditChainStore(audit_dir)
    for event in chain.query(event_type=EVENT_SPIFFE_SVID_BINDING):
        ok, _reason = verify_binding_against_event(binding, event)
        if ok:
            return True
    return False
