"""CLI command: ``bernstein bundle`` -- per-ticket transcript bundle.

Wraps :class:`bernstein.core.observability.ticket_bundle.TicketBundle` in
a Click group so operators and auditors can produce, sign, and verify
ticket bundles without writing Python.

Subcommands:

- ``bernstein bundle ticket <tracker> <ticket_id> --out <path>`` -- assemble
  the archive, optionally sign it when keys are available.
- ``bernstein bundle verify <archive> <signature> --card <agent_card.json>``
  -- verify a previously signed bundle on the auditor host.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from bernstein.cli.helpers import console
from bernstein.core.lineage.identity import AgentCard
from bernstein.core.observability.ticket_bundle import TicketBundle


@click.group("bundle")
def bundle_group() -> None:
    """Per-ticket transcript bundle commands."""


@bundle_group.command("ticket")
@click.argument("tracker", type=str)
@click.argument("ticket_id", type=str)
@click.option(
    "--out",
    "-o",
    "out",
    type=click.Path(dir_okay=False, path_type=Path),
    required=True,
    help="Destination path for the bundle archive (tar.gz).",
)
@click.option(
    "--workdir",
    type=click.Path(file_okay=False, exists=True, path_type=Path),
    default=Path(),
    help="Project root directory containing .sdd/ (default: current).",
)
@click.option(
    "--sign-key",
    type=click.Path(dir_okay=False, exists=True, path_type=Path),
    default=None,
    help="PEM-encoded Ed25519 private key to detached-sign the manifest.",
)
@click.option(
    "--sign-kid",
    type=str,
    default=None,
    help="Key id (kid) to embed in the JWS header; required with --sign-key.",
)
def ticket_cmd(
    tracker: str,
    ticket_id: str,
    out: Path,
    workdir: Path,
    sign_key: Path | None,
    sign_kid: str | None,
) -> None:
    """Assemble a per-ticket transcript bundle for an auditor.

    \b
      bernstein bundle ticket github ENG-42 --out ENG-42.tar.gz
      bernstein bundle ticket jira PROJ-9 --out PROJ-9.tar.gz \\
        --sign-key keys/lineage.pem --sign-kid lineage-2026
    """
    # Validate signing inputs together before mutating any state on disk.
    # --sign-key and --sign-kid must be supplied as a pair: a kid without a
    # key cannot sign, and a key without a kid leaves the JWS header empty.
    if bool(sign_key) != bool(sign_kid):
        raise click.UsageError(
            "--sign-key and --sign-kid must be provided together",
        )

    priv_pem: str | None = None
    if sign_key is not None:
        try:
            priv_pem = sign_key.read_text(encoding="utf-8")
        except OSError as exc:
            raise click.ClickException(f"could not read --sign-key: {exc}") from exc

    bundle = TicketBundle(workdir=workdir, tracker=tracker, ticket_id=ticket_id)
    manifest = bundle.assemble(out=out)

    if priv_pem is not None and sign_kid is not None:
        jws_path = bundle.sign(private_key_pem=priv_pem, kid=sign_kid)
        sig_note = f" Signed -> [bold]{jws_path}[/bold]."
    else:
        sig_note = ""

    size_bytes = out.stat().st_size
    size_kib = size_bytes / 1024
    console.print(
        f"Ticket bundle written to [bold]{out}[/bold] "
        f"({size_kib:.1f} KiB, {len(manifest.files)} files, "
        f"tracker={tracker} ticket={ticket_id}).{sig_note}",
    )


@bundle_group.command("verify")
@click.argument(
    "archive",
    type=click.Path(dir_okay=False, exists=True, path_type=Path),
)
@click.argument(
    "signature",
    type=click.Path(dir_okay=False, exists=True, path_type=Path),
)
@click.option(
    "--card",
    "card_path",
    type=click.Path(dir_okay=False, exists=True, path_type=Path),
    required=True,
    help="JSON file with the verifier's Agent Card (agent_id, kid, public_key_pem).",
)
def verify_cmd(archive: Path, signature: Path, card_path: Path) -> None:
    """Verify a ticket bundle against an Agent Card.

    Click exits with status 0 when the bundle is verified.

    Raises:
        click.ClickException: If reading the Agent Card fails, decoding
            raises ValueError, a required key is missing, or verification
            returns false.
    """
    try:
        card_payload = json.loads(card_path.read_text(encoding="utf-8"))
        card = AgentCard(
            agent_id=str(card_payload["agent_id"]),
            kid=str(card_payload["kid"]),
            public_key_pem=str(card_payload["public_key_pem"]),
            protocol_version=str(card_payload.get("protocol_version", "a2a/1.0")),
        )
    except (OSError, ValueError, KeyError) as exc:
        raise click.ClickException(f"could not load Agent Card: {exc}") from exc

    ok = TicketBundle.verify(archive, signature, card)
    if ok:
        console.print(f"[green]Bundle verified:[/green] {archive}")
        return
    raise click.ClickException(f"bundle verification failed for {archive}")


__all__ = ["bundle_group", "ticket_cmd", "verify_cmd"]
