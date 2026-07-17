"""``bernstein ctx``: switchable operating contexts (#2550).

An operating context atomically pins server URL, store DSN, adapter defaults,
and a budget-envelope name as one named unit. Activation inserts one layer
into the config precedence chain between project and global, and is itself an
audit event embedding the canonical effective-settings hash.

The ``context`` command name is taken by the worker context-capsule surface
(#2545), so this fleet surface is ``ctx``.

    bernstein ctx create <name> [--server-url URL] [--store-dsn DSN] [--set k=v]
    bernstein ctx use    <name>
    bernstein ctx show   [<name>]
    bernstein ctx list
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from bernstein.cli.helpers import console


def _load_hmac_key() -> bytes:
    from bernstein.core.security.audit import load_or_create_audit_key

    return load_or_create_audit_key()


def _store(workdir: Path):
    from bernstein.core.fleet.context import ContextStore
    from bernstein.core.security.audit_chain import AuditChainStore

    sdd = Path(workdir) / ".sdd"
    chain = AuditChainStore(sdd / "audit", key=_load_hmac_key())
    return ContextStore(sdd / "fleet" / "contexts", chain=chain)


def _parse_layer(pairs: tuple[str, ...]) -> dict[str, object]:
    layer: dict[str, object] = {}
    for pair in pairs:
        if "=" not in pair:
            raise click.BadParameter(f"expected key=value, got {pair!r}")
        key, raw = pair.split("=", 1)
        try:
            layer[key] = json.loads(raw)
        except json.JSONDecodeError:
            layer[key] = raw
    return layer


_WORKDIR_OPTION = click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    help="Project directory holding .sdd (default: current directory).",
)


@click.group("ctx")
def ctx_group() -> None:
    """Switchable operating contexts (create / use / show / list).

    \b
      bernstein ctx create <name> [--server-url URL] [--set k=v]
      bernstein ctx use    <name>
      bernstein ctx show   [<name>]
      bernstein ctx list
    """


@ctx_group.command("create")
@click.argument("name")
@click.option("--server-url", default="", help="Task-server URL the context pins.")
@click.option("--store-dsn", default="", help="Persistence DSN the context pins.")
@click.option("--budget-envelope", default="", help="Named budget envelope active under this context.")
@click.option("--set", "layer", multiple=True, help="Config-layer override k=value (repeatable).")
@_WORKDIR_OPTION
def ctx_create_cmd(
    name: str,
    server_url: str,
    store_dsn: str,
    budget_envelope: str,
    layer: tuple[str, ...],
    workdir: str,
) -> None:
    """Define a new operating context named NAME."""
    from bernstein.core.fleet.context import OperatingContext

    store = _store(Path(workdir))
    store.create(
        OperatingContext(
            name=name,
            server_url=server_url,
            store_dsn=store_dsn,
            budget_envelope=budget_envelope,
            config_layer=_parse_layer(layer),
        )
    )
    console.print(f"[green]created context[/green] {name}")


@ctx_group.command("use")
@click.argument("name")
@_WORKDIR_OPTION
def ctx_use_cmd(name: str, workdir: str) -> None:
    """Atomically activate context NAME (recorded on the audit chain)."""
    store = _store(Path(workdir))
    try:
        receipt = store.activate(name)
    except KeyError:
        console.print(f"[red]no such context:[/red] {name}")
        raise SystemExit(1) from None
    console.print(f"[green]using context[/green] {name} (settings {receipt.settings_hash[:19]}...)")


@ctx_group.command("show")
@click.argument("name", required=False, default=None)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit machine-readable JSON.")
@_WORKDIR_OPTION
def ctx_show_cmd(name: str | None, as_json: bool, workdir: str) -> None:
    """Show a context (or the active one when NAME is omitted)."""
    store = _store(Path(workdir))
    if name is None:
        ctx = store.active()
        if ctx is None:
            console.print("[dim]no active context[/dim]")
            return
    else:
        try:
            ctx = store.get(name)
        except KeyError:
            console.print(f"[red]no such context:[/red] {name}")
            raise SystemExit(1) from None
    if as_json:
        console.print_json(json.dumps({**ctx.to_document(), "settings_hash": ctx.settings_hash()}))
        return
    console.print(f"[cyan]{ctx.name}[/cyan]  (settings {ctx.settings_hash()[:19]}...)")
    console.print(f"  server_url      = {ctx.server_url or '-'}")
    console.print(f"  store_dsn       = {ctx.store_dsn or '-'}")
    console.print(f"  budget_envelope = {ctx.budget_envelope or '-'}")
    if ctx.config_layer:
        console.print(f"  config_layer    = {ctx.config_layer}")


@ctx_group.command("list")
@_WORKDIR_OPTION
def ctx_list_cmd(workdir: str) -> None:
    """List every operating context, marking the active one."""
    store = _store(Path(workdir))
    names = store.list_names()
    if not names:
        console.print("[dim]no operating contexts[/dim]")
        return
    active = store.active_name()
    for name in names:
        marker = "[green]*[/green]" if name == active else " "
        console.print(f"{marker} {name}")
