"""``bernstein var``: audit-chained fleet variables (#2550).

A fleet variable is a named JSON value whose identity is its audit-chain
segment. Every ``set`` is a signed chain event carrying the old and new value
hashes; ``history`` renders that segment; a claim-time read (in the run path)
pins the value into task lineage so replay resolves from the pin, never the
live value.

    bernstein var set   <name> <json-value>
    bernstein var get   <name>
    bernstein var list
    bernstein var history <name>
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
    from bernstein.core.fleet.variables import FleetVariableStore
    from bernstein.core.security.audit_chain import AuditChainStore

    sdd = Path(workdir) / ".sdd"
    chain = AuditChainStore(sdd / "audit", key=_load_hmac_key())
    return FleetVariableStore(sdd / "fleet" / "variables", chain=chain)


#: A raw value that begins with one of these characters is unambiguously
#: intended as JSON; if it then fails to parse it is a user error, not a
#: string. A bare word (``hello``) is still accepted as a string.
_JSON_INTENT_PREFIXES = ("{", "[", '"')


def _parse_value(raw: str) -> object:
    """Parse *raw* as JSON, falling back to a literal string for bare words.

    A value that clearly signals JSON intent (starts with ``{``, ``[`` or
    ``"``) but fails to parse is rejected rather than silently stored as the
    literal string, so a malformed object is caught instead of persisted.
    """
    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        if raw[:1] in _JSON_INTENT_PREFIXES:
            raise click.BadParameter(f"value looks like JSON but is invalid: {exc}") from exc
        return raw


_WORKDIR_OPTION = click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    help="Project directory holding .sdd (default: current directory).",
)


@click.group("var")
def var_group() -> None:
    """Audit-chained fleet variables (set / get / list / history).

    \b
      bernstein var set   <name> <json-value>
      bernstein var get   <name>
      bernstein var list
      bernstein var history <name>
    """


@var_group.command("set")
@click.argument("name")
@click.argument("value")
@click.option("--actor", default="operator", help="Actor recorded on the chain event.")
@_WORKDIR_OPTION
def var_set_cmd(name: str, value: str, actor: str, workdir: str) -> None:
    """Write VALUE (parsed as JSON) under NAME as an audit-chain event."""
    store = _store(Path(workdir))
    write = store.set(name, _parse_value(value), actor=actor)
    console.print(f"[green]set[/green] {name} (position {write.chain_position}, hash {write.new_value_hash[:19]}...)")


@var_group.command("get")
@click.argument("name")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the raw JSON value.")
@_WORKDIR_OPTION
def var_get_cmd(name: str, as_json: bool, workdir: str) -> None:
    """Print the current value of NAME."""
    store = _store(Path(workdir))
    try:
        value = store.get(name)
    except KeyError:
        console.print(f"[red]no such variable:[/red] {name}")
        raise SystemExit(1) from None
    if as_json:
        console.print_json(json.dumps(value))
    else:
        console.print(repr(value))


@var_group.command("list")
@click.option(
    "--values",
    is_flag=True,
    default=False,
    help="Also print each current value (may expose sensitive config).",
)
@_WORKDIR_OPTION
def var_list_cmd(values: bool, workdir: str) -> None:
    """List every variable name (names only by default).

    Values are shown only with ``--values`` so a routine listing does not
    spill potentially sensitive configuration to the terminal or logs.
    """
    store = _store(Path(workdir))
    names = store.list_names()
    if not names:
        console.print("[dim]no fleet variables set[/dim]")
        return
    for name in names:
        if values:
            console.print(f"{name} = {store.get(name)!r}")
        else:
            console.print(name)


@var_group.command("history")
@click.argument("name")
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit machine-readable JSON.")
@_WORKDIR_OPTION
def var_history_cmd(name: str, as_json: bool, workdir: str) -> None:
    """Render NAME's chain segment: every write with its value hashes.

    This resolves from the chain alone, so two workers reading different
    values are explained by the write that landed between their positions -
    with no server running.
    """
    store = _store(Path(workdir))
    history = store.history(name)
    if not history:
        console.print(f"[dim]no history for[/dim] {name}")
        return
    if as_json:
        console.print_json(
            json.dumps(
                [
                    {
                        "chain_position": w.chain_position,
                        "old_value_hash": w.old_value_hash,
                        "new_value_hash": w.new_value_hash,
                        "actor": w.actor,
                    }
                    for w in history
                ]
            )
        )
        return
    for w in history:
        console.print(
            f"[cyan]{w.chain_position}[/cyan] {w.actor}: "
            f"{w.old_value_hash[:15] or '(genesis)'} -> {w.new_value_hash[:15]}"
        )
