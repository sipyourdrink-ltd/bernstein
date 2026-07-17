"""``bernstein mission`` - ledger-projected multi-day goals (#2509).

A mission is a declared decomposition of a goal into phases, each with a
verification gate and a budget envelope. Mission status is never stored: it is
a pure deterministic projection over the work-ledger chain plus the evidence
bundles the phase receipts reference (see
:mod:`bernstein.core.orchestration.missions`). This command group is the
operator surface over it:

* ``define``  - validate a mission spec and write the ``mission.defined``
  transition into a fresh work ledger.
* ``status``  - project the current mission status from the ledger and print
  it (with the ``mission_status_hash`` two hosts must agree on).
* ``verify``  - re-verify the chain end to end and prove every referenced
  evidence bundle is intact; a tampered entry or a deleted bundle fails.
* ``resume``  - rebuild mission state purely by replaying the ledger on any
  clone, reproducing the identical status hash after a restart or reimage.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from bernstein.cli.helpers import console
from bernstein.core.orchestration.missions import (
    MISSION_UNVERIFIED,
    MissionProjection,
    MissionSpec,
    MissionSpecError,
    define_mission,
    mission_ledger_dir,
    project_mission_from_ledger,
)
from bernstein.core.persistence.work_ledger import LedgerError, LedgerReader, WorkLedger

# Exit codes shared across the group so operators (and the dashboard) can
# branch on the specific failure mode.
EXIT_OK = 0
EXIT_NO_MISSION = 1
EXIT_VERIFY_FAILED = 2
EXIT_BAD_SPEC = 3

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


def _sdd_dir(workdir: Path | None) -> Path:
    return (workdir or Path.cwd()).resolve() / ".sdd"


def _workdir(workdir: Path | None) -> Path:
    return (workdir or Path.cwd()).resolve()


@click.group("mission")
def mission_group() -> None:
    """Ledger-projected missions: define, status, verify, resume."""


# ---------------------------------------------------------------------------
# define
# ---------------------------------------------------------------------------


@mission_group.command("define")
@click.argument("spec_path", type=click.Path(exists=True, dir_okay=False, path_type=Path))
@_WORKDIR_OPTION
@_JSON_OPTION
def mission_define_cmd(spec_path: Path, workdir: Path | None, output_json: bool) -> None:
    """Define a mission from a JSON spec, writing ledger entries only.

    The spec is validated at the boundary; the goal text is bound into the
    ledger by digest, never verbatim. No status row is written -- status is
    projected on demand.

    \b
    Exit codes:
        0  mission defined
        3  the spec failed validation
    """
    try:
        raw = json.loads(spec_path.read_text(encoding="utf-8"))
        spec = MissionSpec.from_dict(raw)
    except (json.JSONDecodeError, MissionSpecError, TypeError, ValueError) as exc:
        console.print(f"[red]Invalid mission spec:[/red] {exc}")
        raise SystemExit(EXIT_BAD_SPEC) from None

    ledger_dir = mission_ledger_dir(_sdd_dir(workdir), spec.mission_id)
    try:
        ledger = WorkLedger.open(ledger_dir)
        entry = define_mission(ledger=ledger, spec=spec)
        ledger.close()
    except LedgerError as exc:
        console.print(f"[red]Failed to write mission definition:[/red] {exc}")
        raise SystemExit(EXIT_VERIFY_FAILED) from None

    if output_json:
        console.print_json(
            json.dumps(
                {
                    "mission_id": spec.mission_id,
                    "spec_hash": spec.spec_hash(),
                    "phases": list(spec.phase_ids()),
                    "entry_hash": entry.entry_hash,
                }
            )
        )
    else:
        console.print(
            f"[green]Mission defined:[/green] {spec.mission_id} "
            f"({len(spec.phases)} phase(s)), spec {spec.spec_hash()[:16]}..."
        )
        console.print(f"[dim]Ledger:[/dim] {ledger_dir}")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def _require_mission(workdir: Path | None, mission_id: str) -> None:
    ledger_dir = mission_ledger_dir(_sdd_dir(workdir), mission_id)
    if not LedgerReader(ledger_dir).exists():
        console.print(f"[red]No mission ledger for {mission_id!r}[/red] at {ledger_dir}")
        raise SystemExit(EXIT_NO_MISSION)


def _render_status(proj: MissionProjection) -> None:
    from rich.panel import Panel
    from rich.table import Table

    status = proj.status
    console.print()
    console.print(
        Panel(
            f"[bold]Mission[/bold] [cyan]{status.mission_id}[/cyan]  overall: [bold]{status.overall}[/bold]",
            border_style="green" if proj.ledger_verified else "red",
            expand=False,
        )
    )
    table = Table(show_header=True, box=None, padding=(0, 2))
    table.add_column("Phase", style="cyan", no_wrap=True)
    table.add_column("State")
    table.add_column("Envelope")
    table.add_column("Budget", justify="right")
    table.add_column("Spend", justify="right")
    for phase in status.phases:
        table.add_row(
            phase.phase_id,
            phase.state,
            phase.envelope,
            f"${phase.budget_usd:.2f}",
            f"${phase.spend_usd:.2f}",
        )
    console.print(table)
    console.print(f"[dim]status hash:[/dim] {proj.status_hash}")
    console.print(
        f"[dim]ledger verified:[/dim] {proj.ledger_verified}  [dim]evidence verified:[/dim] {proj.evidence_verified}"
    )
    console.print()


@mission_group.command("status")
@click.argument("mission_id")
@_WORKDIR_OPTION
@_JSON_OPTION
def mission_status_cmd(mission_id: str, workdir: Path | None, output_json: bool) -> None:
    """Project and print the current mission status from the ledger.

    \b
    Exit codes:
        0  status projected
        1  no mission ledger for this id
    """
    _require_mission(workdir, mission_id)
    projection = project_mission_from_ledger(
        sdd_dir=_sdd_dir(workdir), workdir=_workdir(workdir), mission_id=mission_id
    )
    if output_json:
        payload = {
            **projection.status.to_dict(),
            "mission_status_hash": projection.status_hash,
            "ledger_head": projection.ledger_head,
            "ledger_verified": projection.ledger_verified,
            "evidence_verified": projection.evidence_verified,
        }
        console.print_json(json.dumps(payload))
    else:
        _render_status(projection)


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


@mission_group.command("verify")
@click.argument("mission_id")
@_WORKDIR_OPTION
@_JSON_OPTION
def mission_verify_cmd(mission_id: str, workdir: Path | None, output_json: bool) -> None:
    """Verify the mission chain and every referenced evidence bundle.

    Fails when a ledger entry was tampered with (surfaced at its exact chain
    position) or when a referenced evidence bundle is missing or altered (the
    projection marks the phase unverified).

    \b
    Exit codes:
        0  chain + evidence verify; every passed phase is provable
        1  no mission ledger for this id
        2  verification failed (chain torn or evidence diverged)
    """
    _require_mission(workdir, mission_id)
    projection = project_mission_from_ledger(
        sdd_dir=_sdd_dir(workdir), workdir=_workdir(workdir), mission_id=mission_id
    )
    ok = projection.ledger_verified and projection.evidence_verified and projection.status.overall != MISSION_UNVERIFIED

    if output_json:
        console.print_json(
            json.dumps(
                {
                    "mission_id": mission_id,
                    "ok": ok,
                    "ledger_verified": projection.ledger_verified,
                    "evidence_verified": projection.evidence_verified,
                    "overall": projection.status.overall,
                    "mission_status_hash": projection.status_hash,
                }
            )
        )
    elif ok:
        console.print(
            f"[green]Mission verified:[/green] {mission_id}, "
            f"overall {projection.status.overall}, status {projection.status_hash[:16]}..."
        )
    else:
        console.print(f"[red]Mission verification failed for {mission_id!r}:[/red]")
        if not projection.ledger_verified:
            console.print("  [red]-[/red] the work-ledger chain does not verify (a ledger entry was tampered with)")
        if not projection.evidence_verified:
            unverified = [p.phase_id for p in projection.status.phases if p.state == "unverified"]
            console.print(f"  [red]-[/red] evidence diverged for phase(s): {', '.join(unverified) or 'unknown'}")
    if not ok:
        raise SystemExit(EXIT_VERIFY_FAILED)


# ---------------------------------------------------------------------------
# resume
# ---------------------------------------------------------------------------


@mission_group.command("resume")
@click.argument("mission_id")
@_WORKDIR_OPTION
@_JSON_OPTION
def mission_resume_cmd(mission_id: str, workdir: Path | None, output_json: bool) -> None:
    """Rebuild mission state purely by replaying the ledger on this clone.

    A mission survives restart, reimage, and machine moves with no auxiliary
    state files: the status hash reproduced here is byte-identical to the one
    projected on the host that ran the mission, provided the ledger is a
    byte-identical copy.

    \b
    Exit codes:
        0  mission state rebuilt from the ledger
        1  no mission ledger for this id
    """
    _require_mission(workdir, mission_id)
    projection = project_mission_from_ledger(
        sdd_dir=_sdd_dir(workdir), workdir=_workdir(workdir), mission_id=mission_id
    )
    if output_json:
        payload = {
            **projection.status.to_dict(),
            "mission_status_hash": projection.status_hash,
            "ledger_head": projection.ledger_head,
            "ledger_verified": projection.ledger_verified,
            "evidence_verified": projection.evidence_verified,
            "entry_count": projection.entry_count,
        }
        console.print_json(json.dumps(payload))
    else:
        _render_status(projection)
        console.print(
            f"[green]Resumed mission[/green] {mission_id} from {projection.entry_count} ledger entries "
            f"(status {projection.status_hash[:16]}...)"
        )


__all__ = [
    "EXIT_BAD_SPEC",
    "EXIT_NO_MISSION",
    "EXIT_OK",
    "EXIT_VERIFY_FAILED",
    "mission_group",
]
