"""CLI surface for the A2A server node (#2609).

Two operator commands, matching the two independent claims the node makes:

* ``bernstein a2a publish`` projects the node's signed capability card into
  agent-registry manifests, so peers discover it by verifiable capability
  rather than by an opaque URL.
* ``bernstein a2a verify --receipt`` checks an inbound A2A response offline.
  A caller that received an answer from this node proves the answer matches
  what the node recorded, without contacting the node and without trusting
  it to summarise its own behaviour.

``bernstein interop a2a`` remains the surface for cross-organisation *peer*
cards (issuing our card for a partner, verifying theirs). This group is
about this node as a callable endpoint.
"""

from __future__ import annotations

import json
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
    SignedCapabilityCard,
    issue_capability_card,
    resolve_advertised_card_policies,
)
from bernstein.core.protocols.a2a.publish import (
    PUBLISH_SURFACES,
    build_publication,
)
from bernstein.core.protocols.a2a.receipt import (
    A2ATaskReceipt,
    verify_task_receipt,
)

#: Default on-disk location for the node's published capability card. Kept
#: stable so republishing does not mint a new identity.
_DEFAULT_CARD_PATH = Path(".sdd") / "a2a" / "published-card.json"


def _fail(message: str) -> None:
    """Print an error in the active output mode and exit non-zero."""
    if is_json():
        print_json({"ok": False, "error": message})
    else:
        print_error(message, soft_wrap=True)
    sys.exit(1)


@click.group("a2a")
def a2a_group() -> None:
    """A2A node surface: publish this node, verify its receipts.

    \b
    Examples:
      bernstein a2a publish --endpoint https://node.example/a2a
      bernstein a2a verify --receipt receipt.json --response response.json
    """


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


@a2a_group.command("verify")
@click.option(
    "--receipt",
    "receipt_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Path to the lineage receipt JSON returned with the A2A response.",
)
@click.option(
    "--response",
    "response_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    required=True,
    help="Path to the A2A response JSON the receipt attests to.",
)
@click.option(
    "--trusted-jwk",
    "trusted_jwk_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="Pin the signing key: the receipt's embedded JWK must match this one.",
)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def verify(
    receipt_path: Path,
    response_path: Path,
    trusted_jwk_path: Path | None,
    as_json: bool,
) -> None:
    """Verify an A2A response against its lineage receipt, offline.

    Exits non-zero when the response does not match the receipt, when the
    receipt's own signature does not verify, or when the receipt carries no
    signature at all - an unattested answer is treated as unverified, not as
    trusted.

    Provenance is only established when ``--trusted-jwk`` pins the signing
    key. Without it the receipt's embedded key is trusted on first use, which
    authenticates the bytes against a key the issuer supplied - not against
    the operator's pinned key - and the command says so on success.
    """
    emit_json = as_json or is_json()

    try:
        receipt = A2ATaskReceipt.from_json(receipt_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        _fail(f"could not parse receipt: {exc}")
        return

    try:
        response = json.loads(response_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        _fail(f"could not parse response: {exc}")
        return

    # A receipt cannot attest to its own bytes, so the attested payload is the
    # response with the receipt field removed. Dropping it here means an
    # operator can pass the raw response body straight off the wire.
    if isinstance(response, dict):
        response = {k: v for k, v in response.items() if k != "receipt"}

    trusted_jwk = None
    if trusted_jwk_path is not None:
        try:
            trusted_jwk = json.loads(trusted_jwk_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            _fail(f"could not parse trusted JWK: {exc}")
            return

    result = verify_task_receipt(receipt, response=response, trusted_public_key_jwk=trusted_jwk)

    if not result.ok:
        if emit_json:
            print_json({"ok": False, "entry_hash": receipt.entry_hash, "failures": result.errors})
        else:
            print_error(f"A2A receipt {receipt_path} is NOT valid:", soft_wrap=True)
            for reason in result.errors:
                console.print(f"  - {reason}")
        sys.exit(1)

    key_pinned = trusted_jwk is not None
    unpinned_warning = (
        "signature verified against the receipt's own embedded key "
        "(trust-on-first-use); the key is not pinned. Pass --trusted-jwk to "
        "verify provenance against the operator's published key."
    )

    if emit_json:
        payload: dict[str, object] = {
            "ok": True,
            "task_id": receipt.task_id,
            "entry_hash": receipt.entry_hash,
            "content_hash": receipt.content_hash,
            "kid": receipt.kid,
            "verified_key_id": result.verified_key_id,
            "key_pinned": key_pinned,
        }
        if not key_pinned:
            payload["warning"] = unpinned_warning
        print_json(payload)
        return
    print_success(f"A2A receipt {receipt_path} is valid", soft_wrap=True)
    console.print(f"  task: [bold]{receipt.task_id}[/bold]  kid: {receipt.kid}")
    console.print(f"  entry: {receipt.entry_hash}")
    if not key_pinned:
        console.print(f"  [yellow]warning:[/yellow] {unpinned_warning}")


# ---------------------------------------------------------------------------
# publish
# ---------------------------------------------------------------------------


@a2a_group.command("publish")
@click.option("--endpoint", required=True, help="Public base URL peers send A2A traffic to.")
@click.option(
    "--output-dir",
    "output_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path(".sdd") / "a2a" / "publish",
    show_default=True,
    help="Directory the registry records are written to.",
)
@click.option(
    "--card",
    "card_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=_DEFAULT_CARD_PATH,
    show_default=True,
    help="Signed capability card to publish. Minted on first use and reused after.",
)
@click.option(
    "--surface",
    "surfaces",
    multiple=True,
    type=click.Choice(PUBLISH_SURFACES),
    help="Registry surface to emit (repeatable). Defaults to every supported surface.",
)
@click.option("--version", "version", default=None, help="Version to publish. Defaults to the installed version.")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def publish(
    endpoint: str,
    output_dir: Path,
    card_path: Path,
    surfaces: tuple[str, ...],
    version: str | None,
    as_json: bool,
) -> None:
    """Emit registry records advertising this node's signed capability card.

    Each record embeds the full signed card and a publisher fingerprint, so a
    consumer verifies the claim against the node's own key and treats the
    registry as a transport rather than an authority. Records are
    deterministic: republishing an unchanged node rewrites identical bytes.
    """
    emit_json = as_json or is_json()

    if version is None:
        from bernstein import __version__ as installed_version

        version = installed_version

    try:
        card = _load_or_issue_card(card_path, endpoint=endpoint)
    except (OSError, ValueError) as exc:
        _fail(f"could not resolve the capability card: {exc}")
        return

    try:
        publication = build_publication(
            card,
            endpoint=endpoint,
            version=version,
            surfaces=surfaces or PUBLISH_SURFACES,
        )
    except ValueError as exc:
        _fail(str(exc))
        return

    written: dict[str, str] = {}
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
        for surface, record in publication.items():
            path = output_dir / f"{surface}.json"
            path.write_text(json.dumps(record, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            written[surface] = str(path)
    except OSError as exc:
        _fail(f"could not write registry records: {exc}")
        return

    if emit_json:
        print_json({"ok": True, "endpoint": endpoint, "version": version, "records": written})
        return
    print_success(f"Published {len(written)} A2A registry record(s)", soft_wrap=True)
    for surface, path in sorted(written.items()):
        console.print(f"  {surface}: {path}")


def _load_or_issue_card(card_path: Path, *, endpoint: str) -> SignedCapabilityCard:
    """Return the node's capability card, minting and persisting it once.

    Reusing a persisted card keeps one stable identity across republications;
    minting a fresh card each time would hand peers a new key to trust on
    every publish. The private key is written beside the card at ``0600``.
    """
    if card_path.exists():
        return SignedCapabilityCard.from_json(card_path.read_text(encoding="utf-8"))

    key_path = card_path.with_suffix(card_path.suffix + ".key.pem")
    private_key_pem = key_path.read_bytes() if key_path.exists() else None

    signed, private_key_pem = issue_capability_card(
        issuer="bernstein",
        name="bernstein",
        description=f"Bernstein orchestrator node at {endpoint}",
        advertised_tools=["task_orchestration", "agent_spawning", "code_review", "a2a_message"],
        policies=resolve_advertised_card_policies(),
        private_key_pem=private_key_pem,
    )

    card_path.parent.mkdir(parents=True, exist_ok=True)
    card_path.write_text(signed.to_json(), encoding="utf-8")
    if not key_path.exists():
        key_path.write_bytes(private_key_pem)
        key_path.chmod(0o600)
    return signed


__all__ = ["a2a_group", "publish", "verify"]
