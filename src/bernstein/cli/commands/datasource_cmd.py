"""``bernstein datasource`` - read-only queries with content-addressed receipts.

A datasource is an operator-configured, read-only SQL connection. Running a
query through it canonicalises the result set and binds it into a signed lineage
receipt: the exact rows that grounded an agent's answer become a
content-addressed, offline-verifiable record instead of unrecorded prompt text.

Subcommands:

* ``register`` / ``list`` - manage connections (DSN secrets stay in the
  operator-only registry; only the connection id ever reaches a receipt).
* ``query`` - execute a read-only SELECT, emit a receipt, print the canonical
  text rendering the agent would see.
* ``verify`` - offline-verify a receipt's signature + chain anchor + stored
  copy; ``--re-execute`` re-runs the query and reports MATCH or DRIFT.
"""

from __future__ import annotations

from pathlib import Path

import click

from bernstein.cli.helpers import console
from bernstein.core.datasources.connection import DataSourceConnection
from bernstein.core.datasources.errors import DataSourceError
from bernstein.core.datasources.result import render_text
from bernstein.core.datasources.service import build_connection_registry, build_receipt_store


def _sdd(workdir: Path) -> Path:
    return workdir.resolve() / ".sdd"


@click.group("datasource")
def datasource_group() -> None:
    """Read-only datasources with content-addressed query receipts."""


@datasource_group.command("register")
@click.argument("connection_id")
@click.argument("dsn")
@click.option("--driver", default="sqlite", show_default=True, help="Engine driver (only 'sqlite' ships today).")
@click.option("--description", default="", help="Optional human note (never put secrets here).")
@click.option("--workdir", type=click.Path(file_okay=False, path_type=Path), default=Path(), show_default=True)
def register_cmd(connection_id: str, dsn: str, driver: str, description: str, workdir: Path) -> None:
    """Register a read-only connection CONNECTION_ID pointing at DSN.

    For the ``sqlite`` driver, DSN is a database file path or ``:memory:``.
    """
    registry = build_connection_registry(_sdd(workdir))
    try:
        connection = DataSourceConnection(id=connection_id, driver=driver, dsn=dsn, description=description)
        registry.put(connection)
    except DataSourceError as exc:
        console.print(f"[red]Cannot register connection:[/red] {exc}")
        raise SystemExit(1) from exc
    console.print(
        f"[green]Registered datasource[/green] [bold]{connection.id}[/bold] "
        f"([dim]{connection.driver}[/dim]) -> {connection.redacted_dsn}"
    )


@datasource_group.command("list")
@click.option("--workdir", type=click.Path(file_okay=False, path_type=Path), default=Path(), show_default=True)
def list_cmd(workdir: Path) -> None:
    """List registered datasource connections (DSN passwords redacted)."""
    from rich.table import Table

    registry = build_connection_registry(_sdd(workdir))
    connections = registry.list_connections()
    if not connections:
        console.print("[dim]No datasources. Register one with 'bernstein datasource register'.[/dim]")
        return
    table = Table(title="Datasource connections", header_style="bold cyan")
    table.add_column("Id", style="bold")
    table.add_column("Driver")
    table.add_column("DSN (redacted)", style="dim")
    table.add_column("Description")
    for conn in connections:
        table.add_row(conn.id, conn.driver, conn.redacted_dsn, conn.description)
    console.print(table)


@datasource_group.command("query")
@click.argument("connection_id")
@click.argument("sql")
@click.option("--param", "params", multiple=True, help="Positional bind parameter (repeatable).")
@click.option("--row-cap", type=int, default=None, help="Max rows per receipt (defaults to engine cap).")
@click.option(
    "--store-copy/--no-store-copy",
    default=True,
    show_default=True,
    help="Persist a re-hashable result copy.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the receipt as JSON.")
@click.option("--workdir", type=click.Path(file_okay=False, path_type=Path), default=Path(), show_default=True)
def query_cmd(
    connection_id: str,
    sql: str,
    params: tuple[str, ...],
    row_cap: int | None,
    store_copy: bool,
    as_json: bool,
    workdir: Path,
) -> None:
    """Run read-only SQL against CONNECTION_ID and emit a query receipt."""
    import json as _json

    sdd = _sdd(workdir)
    registry = build_connection_registry(sdd)
    try:
        connection = registry.get(connection_id)
        engine = connection.open_engine()
        bind = list(params) if params else None
        from bernstein.core.datasources.engine import DEFAULT_ROW_CAP

        cap = row_cap if row_cap is not None else DEFAULT_ROW_CAP
        result = engine.execute(sql, bind, row_cap=cap)
        store = build_receipt_store(sdd)
        receipt = store.record(
            connection=connection,
            query_text=sql,
            params=bind,
            result=result,
            store_result_copy=store_copy,
        )
    except DataSourceError as exc:
        console.print(f"[red]Query failed:[/red] {exc}")
        raise SystemExit(1) from exc

    if as_json:
        click.echo(_json.dumps(receipt.to_dict(), indent=2, sort_keys=True))
        return
    console.print(render_text(result))
    console.print(
        f"\n[green]Receipt[/green] [bold]{receipt.receipt_id}[/bold]  "
        f"content_hash={receipt.content_hash[:23]}...  rows={receipt.row_count}"
        + ("  [yellow]truncated[/yellow]" if receipt.truncated else "")
    )


@datasource_group.command("verify")
@click.argument("receipt_id")
@click.option("--re-execute", "re_execute", is_flag=True, default=False, help="Re-run the query and report drift.")
@click.option(
    "--connection",
    "connection_id",
    default=None,
    help="Connection to re-execute against (default: recorded id).",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit the result as JSON.")
@click.option("--workdir", type=click.Path(file_okay=False, path_type=Path), default=Path(), show_default=True)
def verify_cmd(
    receipt_id: str,
    re_execute: bool,
    connection_id: str | None,
    as_json: bool,
    workdir: Path,
) -> None:
    """Verify a receipt offline; optionally re-execute and report drift."""
    import json as _json

    sdd = _sdd(workdir)
    store = build_receipt_store(sdd)
    try:
        outcome = store.verify(receipt_id)
    except DataSourceError as exc:
        console.print(f"[red]Cannot verify:[/red] {exc}")
        raise SystemExit(1) from exc

    if as_json and not re_execute:
        click.echo(_json.dumps({"ok": outcome.ok, "checks": outcome.checks, "failures": outcome.failures}, indent=2))
    elif outcome.ok:
        console.print(f"[bold green]Receipt verified[/bold green] {receipt_id}")
        for name in sorted(outcome.checks):
            console.print(f"  [green]OK[/green] {name}")
    else:
        console.print(f"[bold red]Receipt verification FAILED[/bold red] {receipt_id}")
        for failure in outcome.failures:
            console.print(f"  [red]![/red] {failure}")

    exit_code = 0 if outcome.ok else 1

    if re_execute:
        registry = build_connection_registry(sdd)
        try:
            recorded = store.load(receipt_id)
            conn = registry.get(connection_id or recorded.connection_id)
            drift = store.reexecute(receipt_id, conn)
        except DataSourceError as exc:
            console.print(f"[red]Cannot re-execute:[/red] {exc}")
            raise SystemExit(1) from exc
        if as_json:
            click.echo(
                _json.dumps(
                    {
                        "status": drift.status,
                        "recorded_hash": drift.recorded_hash,
                        "live_hash": drift.live_hash,
                        "recorded_row_count": drift.recorded_row_count,
                        "live_row_count": drift.live_row_count,
                    },
                    indent=2,
                )
            )
        elif drift.match:
            console.print(f"[bold green]{drift.status}[/bold green]  live result matches the receipt.")
        else:
            console.print(f"[bold red]{drift.status}[/bold red]  the data changed under the agent.")
            console.print(f"  recorded: {drift.recorded_hash}  ({drift.recorded_row_count} rows)")
            console.print(f"  live:     {drift.live_hash}  ({drift.live_row_count} rows)")
        if not drift.match:
            exit_code = 1

    raise SystemExit(exit_code)


__all__ = ["datasource_group"]
