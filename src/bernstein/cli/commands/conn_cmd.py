"""``bernstein conn``: named connection documents (#2550).

A connection document is a typed, Ed25519-signed record that names a
broker-managed secret plus connector defaults. Task specs reference it by
name, so rotating one document re-points every consumer at the next mint.
The document carries no secret material.

    bernstein conn create <name> --secret <secret-name> [--scope S] [--default k=v]
    bernstein conn list
    bernstein conn rotate <name> [--secret <new-secret-name>] [--scope S]
    bernstein conn audit [<name>]
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from bernstein.cli.helpers import console


def _load_hmac_key() -> bytes:
    from bernstein.core.security.audit import load_or_create_audit_key

    return load_or_create_audit_key()


def _sdd(workdir: Path) -> Path:
    return Path(workdir) / ".sdd"


def _chain(workdir: Path):
    from bernstein.core.security.audit_chain import AuditChainStore

    return AuditChainStore(_sdd(workdir) / "audit", key=_load_hmac_key())


def _store(workdir: Path):
    from bernstein.core.fleet.connection import ConnectionDocumentStore

    return ConnectionDocumentStore(_sdd(workdir) / "fleet" / "connections")


def _identity_dir(workdir: Path) -> Path:
    return _sdd(workdir) / "identity"


def _parse_defaults(pairs: tuple[str, ...]) -> dict[str, object]:
    defaults: dict[str, object] = {}
    for pair in pairs:
        if "=" not in pair:
            raise click.BadParameter(f"expected key=value, got {pair!r}")
        key, raw = pair.split("=", 1)
        try:
            defaults[key] = json.loads(raw)
        except json.JSONDecodeError:
            defaults[key] = raw
    return defaults


_WORKDIR_OPTION = click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    help="Project directory holding .sdd (default: current directory).",
)


@click.group("conn")
def conn_group() -> None:
    """Named connection documents (create / list / rotate / audit).

    \b
      bernstein conn create <name> --secret <secret-name>
      bernstein conn list
      bernstein conn rotate <name> --secret <new-secret-name>
      bernstein conn audit [<name>]
    """


@conn_group.command("create")
@click.argument("name")
@click.option("--secret", "secret_name", required=True, help="Broker-managed secret name to reference.")
@click.option("--scope", default="", help="Connector scope (e.g. repo:read).")
@click.option("--default", "defaults", multiple=True, help="Connector default k=value (repeatable).")
@_WORKDIR_OPTION
def conn_create_cmd(name: str, secret_name: str, scope: str, defaults: tuple[str, ...], workdir: str) -> None:
    """Create and sign a new connection document under NAME."""
    from bernstein.core.fleet.connection import create_document

    wd = Path(workdir)
    doc = create_document(
        name=name,
        secret_name=secret_name,
        scope=scope,
        connector_defaults=_parse_defaults(defaults),
        identity_dir=_identity_dir(wd),
        chain=_chain(wd),
        store=_store(wd),
    )
    console.print(f"[green]created[/green] {name} (document {doc.document_hash()[:19]}...)")


@conn_group.command("list")
@_WORKDIR_OPTION
def conn_list_cmd(workdir: str) -> None:
    """List every connection document by name."""
    store = _store(Path(workdir))
    names = store.list_names()
    if not names:
        console.print("[dim]no connection documents[/dim]")
        return
    for name in names:
        doc = store.get(name)
        console.print(f"{name} -> secret {doc.secret_name} (scope {doc.scope or '-'}, v{doc.version})")


@conn_group.command("rotate")
@click.argument("name")
@click.option("--secret", "secret_name", default=None, help="New broker-managed secret name.")
@click.option("--scope", default=None, help="New connector scope.")
@_WORKDIR_OPTION
def conn_rotate_cmd(name: str, secret_name: str | None, scope: str | None, workdir: str) -> None:
    """Rotate NAME; consumers re-point at next mint with zero spec edits."""
    from bernstein.core.fleet.connection import rotate_document

    wd = Path(workdir)
    rotated = rotate_document(
        name,
        new_secret_name=secret_name,
        new_scope=scope,
        identity_dir=_identity_dir(wd),
        chain=_chain(wd),
        store=_store(wd),
    )
    console.print(f"[green]rotated[/green] {name} -> v{rotated.version} (secret {rotated.secret_name})")


@conn_group.command("audit")
@click.argument("name", required=False, default=None)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit machine-readable JSON.")
@_WORKDIR_OPTION
def conn_audit_cmd(name: str | None, as_json: bool, workdir: str) -> None:
    """Reconstruct every task that resolved a document, offline from the chain."""
    from bernstein.core.fleet.connection import audit_resolutions

    receipts = audit_resolutions(_chain(Path(workdir)), name=name)
    if not receipts:
        if as_json:
            console.print_json("[]")
        else:
            console.print("[dim]no resolutions recorded[/dim]")
        return
    if as_json:
        console.print_json(
            json.dumps(
                [
                    {
                        "name": r.name,
                        "document_hash": r.document_hash,
                        "task_id": r.task_id,
                        "token_id": r.token_id,
                    }
                    for r in receipts
                ]
            )
        )
        return
    for r in receipts:
        console.print(f"[cyan]{r.name}[/cyan] task {r.task_id} token {r.token_id} ({r.document_hash[:15]}...)")
