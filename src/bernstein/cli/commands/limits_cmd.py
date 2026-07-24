"""``bernstein limits`` - named resource pools with lease-backed admission (#2544).

The admission subsystem treats the grant as the artifact: pool occupancy, queue
order, effective rate limits, and postures are a pure projection of a
hash-chained ledger, never a mutable side table. This command group is the
operator surface over it:

* ``pool create``  - name a slot pool (``staging-env --slots 1``).
* ``tag set``      - bound a task tag's concurrency (``--limit 0`` quarantines).
* ``rate set``     - define a fleet-wide named rate limit with adaptive decay.
* ``queue create`` / ``queue pause`` - operator-defined named queues.
* ``status``       - show projected pools, tags, rates, queues, and grants.
* ``verify``       - recompute the projection from genesis; fail closed on any
  hash mismatch or any grant the projection would not have issued.
"""

from __future__ import annotations

import json
from pathlib import Path

import click
from rich.table import Table

from bernstein.cli.helpers import console
from bernstein.core.admission.engine import AdmissionEngine
from bernstein.core.admission.models import Posture, canonical_name
from bernstein.core.persistence.work_ledger import LedgerError

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_VERIFY_FAILED = 2

_WORKDIR_OPTION = click.option(
    "--workdir",
    default=None,
    type=click.Path(file_okay=False, path_type=Path),
    help="Project root (defaults to current directory).",
)
_JSON_OPTION = click.option(
    "--json",
    "output_json",
    is_flag=True,
    default=False,
    help="Emit machine-readable JSON instead of the Rich summary.",
)
_POSTURE_OPTION = click.option(
    "--posture",
    type=click.Choice([p.value for p in Posture]),
    default=Posture.ENFORCE.value,
    show_default=True,
    help="Enforcement posture: enforce (block), advise (waiver), off (inert).",
)


def _engine(workdir: Path | None) -> AdmissionEngine:
    root = (workdir or Path.cwd()).resolve()
    return AdmissionEngine.for_workdir(root)


@click.group("limits")
def limits_group() -> None:
    """Named resource pools with lease-backed admission (verify, status, CRUD)."""


# ---------------------------------------------------------------------------
# pool
# ---------------------------------------------------------------------------


@limits_group.group("pool")
def pool_group() -> None:
    """Named slot pools: one staging env, one migration lock, N seats."""


@pool_group.command("create")
@click.argument("name")
@click.option("--slots", type=int, required=True, help="Maximum concurrent grants (0 quarantines).")
@_POSTURE_OPTION
@_WORKDIR_OPTION
@_JSON_OPTION
def pool_create_cmd(name: str, slots: int, posture: str, workdir: Path | None, output_json: bool) -> None:
    """Create or update a named slot pool."""
    try:
        entry_hash = _engine(workdir).set_pool(name, slots, posture=Posture(posture))
    except (ValueError, LedgerError) as exc:
        console.print(f"[red]Pool create failed:[/red] {exc}")
        raise SystemExit(EXIT_ERROR) from None
    if output_json:
        console.print_json(json.dumps({"name": name, "slots": slots, "posture": posture, "entry_hash": entry_hash}))
    else:
        console.print(f"[green]Pool set:[/green] {name} slots={slots} posture={posture} (row {entry_hash[:16]}...)")


# ---------------------------------------------------------------------------
# tag
# ---------------------------------------------------------------------------


@limits_group.group("tag")
def tag_group() -> None:
    """Task-tag concurrency ceilings (--limit 0 quarantines the class)."""


@tag_group.command("set")
@click.argument("tag")
@click.option("--limit", type=int, required=True, help="Max concurrent grants holding the tag (0 quarantines).")
@_POSTURE_OPTION
@_WORKDIR_OPTION
@_JSON_OPTION
def tag_set_cmd(tag: str, limit: int, posture: str, workdir: Path | None, output_json: bool) -> None:
    """Set a concurrency ceiling over a task tag."""
    try:
        entry_hash = _engine(workdir).set_tag_limit(tag, limit, posture=Posture(posture))
    except (ValueError, LedgerError) as exc:
        console.print(f"[red]Tag set failed:[/red] {exc}")
        raise SystemExit(EXIT_ERROR) from None
    if output_json:
        console.print_json(json.dumps({"tag": tag, "limit": limit, "posture": posture, "entry_hash": entry_hash}))
    else:
        note = " [yellow](quarantine)[/yellow]" if limit == 0 else ""
        console.print(
            f"[green]Tag limit set:[/green] {tag} limit={limit} posture={posture}{note} (row {entry_hash[:16]}...)"
        )


# ---------------------------------------------------------------------------
# rate
# ---------------------------------------------------------------------------


@limits_group.group("rate")
def rate_group() -> None:
    """Fleet-wide named rate limits with adaptive decay over recorded 429s."""


@rate_group.command("set")
@click.argument("name")
@click.option("--base-limit", type=int, required=True, help="Ceiling with zero recent 429 observations.")
@click.option("--floor", type=int, default=1, show_default=True, help="Lowest the adaptive limit may decay to.")
@_POSTURE_OPTION
@_WORKDIR_OPTION
@_JSON_OPTION
def rate_set_cmd(name: str, base_limit: int, floor: int, posture: str, workdir: Path | None, output_json: bool) -> None:
    """Define a fleet-wide named rate limit."""
    try:
        entry_hash = _engine(workdir).set_rate_limit(name, base_limit, floor=floor, posture=Posture(posture))
    except (ValueError, LedgerError) as exc:
        console.print(f"[red]Rate set failed:[/red] {exc}")
        raise SystemExit(EXIT_ERROR) from None
    if output_json:
        console.print_json(
            json.dumps(
                {"name": name, "base_limit": base_limit, "floor": floor, "posture": posture, "entry_hash": entry_hash}
            )
        )
    else:
        console.print(f"[green]Rate limit set:[/green] {name} base={base_limit} floor={floor} posture={posture}")


# ---------------------------------------------------------------------------
# queue
# ---------------------------------------------------------------------------


@limits_group.group("queue")
def queue_group() -> None:
    """Operator-creatable named queues generalising the DRR scheduler."""


@queue_group.command("create")
@click.argument("name")
@click.option(
    "--priority", type=int, default=0, show_default=True, help="Higher runs first; aging lifts starved queues."
)
@_WORKDIR_OPTION
@_JSON_OPTION
def queue_create_cmd(name: str, priority: int, workdir: Path | None, output_json: bool) -> None:
    """Create or update a named queue."""
    try:
        entry_hash = _engine(workdir).set_queue(name, priority=priority)
    except (ValueError, LedgerError) as exc:
        console.print(f"[red]Queue create failed:[/red] {exc}")
        raise SystemExit(EXIT_ERROR) from None
    if output_json:
        console.print_json(json.dumps({"name": name, "priority": priority, "entry_hash": entry_hash}))
    else:
        console.print(f"[green]Queue set:[/green] {name} priority={priority}")


@queue_group.command("pause")
@click.argument("name")
@click.option("--resume", is_flag=True, default=False, help="Resume instead of pause.")
@_WORKDIR_OPTION
@_JSON_OPTION
def queue_pause_cmd(name: str, resume: bool, workdir: Path | None, output_json: bool) -> None:
    """Pause (or resume) a named queue."""
    engine = _engine(workdir)
    state = engine.state()
    # Canonicalize the name so a mis-cased argument resolves to the same queue,
    # and refuse an unknown queue instead of conjuring a phantom one (with its
    # priority silently reset to 0).
    try:
        canonical = canonical_name(name)
    except ValueError as exc:
        console.print(f"[red]Queue update failed:[/red] {exc}")
        raise SystemExit(EXIT_ERROR) from None
    spec = state.queues.get(canonical)
    if spec is None:
        console.print(f"[red]Queue update failed:[/red] no such queue {name!r}")
        raise SystemExit(EXIT_ERROR) from None
    try:
        entry_hash = engine.set_queue(canonical, priority=spec.priority, paused=not resume)
    except (ValueError, LedgerError) as exc:
        console.print(f"[red]Queue update failed:[/red] {exc}")
        raise SystemExit(EXIT_ERROR) from None
    action = "resumed" if resume else "paused"
    if output_json:
        console.print_json(json.dumps({"name": name, "paused": not resume, "entry_hash": entry_hash}))
    else:
        console.print(f"[green]Queue {action}:[/green] {name}")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@limits_group.command("status")
@_WORKDIR_OPTION
@_JSON_OPTION
def limits_status_cmd(workdir: Path | None, output_json: bool) -> None:
    """Show the projected admission state (pools, tags, rates, queues, grants)."""
    state = _engine(workdir).state()
    if output_json:
        console.print_json(state.to_canonical_json())
        return

    console.print(f"[bold]Admission state[/bold] (head {state.head_hash[:16]}..., {state.entries} rows)")
    if state.pools:
        table = Table(title="Pools", show_lines=False)
        table.add_column("name")
        table.add_column("slots", justify="right")
        table.add_column("held", justify="right")
        table.add_column("posture")
        for name in sorted(state.pools):
            spec = state.pools[name]
            table.add_row(name, str(spec.slots), str(state.pool_occupancy(name)), spec.posture.value)
        console.print(table)
    if state.tag_limits:
        occ = state.tag_occupancy()
        table = Table(title="Tag limits")
        table.add_column("tag")
        table.add_column("limit", justify="right")
        table.add_column("held", justify="right")
        table.add_column("posture")
        for tag in sorted(state.tag_limits):
            spec = state.tag_limits[tag]
            table.add_row(tag, str(spec.limit), str(occ.get(tag, 0)), spec.posture.value)
        console.print(table)
    console.print(
        f"Active grants: {len(state.active_grants)}  Waivers: {state.waivers}  Quarantines: {len(state.quarantines)}"
    )


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


@limits_group.command("verify")
@_WORKDIR_OPTION
@_JSON_OPTION
def limits_verify_cmd(workdir: Path | None, output_json: bool) -> None:
    """Recompute admission state from genesis and fail closed on any drift.

    \b
    Exit codes:
        0  the admission ledger verifies end to end
        2  verification failed (the exact position is named)
    """
    result = _engine(workdir).verify()
    if output_json:
        console.print_json(
            json.dumps(
                {
                    "ok": result.ok,
                    "head_hash": result.head_hash,
                    "entries": result.entries,
                    "errors": list(result.errors),
                }
            )
        )
    elif result.ok:
        console.print(
            f"[green]Admission ledger verified:[/green] {result.entries} rows, head {result.head_hash[:16]}..."
        )
    else:
        console.print("[red]Admission verification failed:[/red]")
        for error in result.errors:
            console.print(f"  [red]-[/red] {error}")
    if not result.ok:
        raise SystemExit(EXIT_VERIFY_FAILED)


__all__ = ["EXIT_ERROR", "EXIT_OK", "EXIT_VERIFY_FAILED", "limits_group"]
