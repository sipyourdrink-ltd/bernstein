"""``bernstein govern reconcile``: diff the governed surface against desired state.

Issue #5085::

    bernstein govern reconcile --propose --desired desired.json [--workdir w] [--full]

The propose run enumerates every registered adapter, cost lane, scheduled task
and capability entry, diffs that snapshot against the desired-state document,
and writes the result as one anchored governance decision record. It changes
nothing else: no entity is added, removed or mutated, so the diff is a
reviewable artefact an operator reads before anything executes.

Output and exit codes:

* ``0`` -- no drift; every entity is unchanged. One "no drift" record is
  written, and the same run against an unchanged environment writes the same
  record every time.
* ``1`` -- the desired-state document could not be read.
* ``2`` -- drift; one ``drift`` verdict line is printed per drifted entity.

By default only drifted entities are printed -- a consecutive run reports what
moved since the previous run's record. ``--full`` prints one ``state`` line per
entity regardless.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import click

from bernstein.core.govern.reconcile import propose_reconcile, snapshot_surface
from bernstein.core.govern.reconcile_models import DesiredState, ReconcileEntry

#: The lineage run every reconcile record anchors to. Fixed so a later run can
#: recover the previous observed state from the same run's records, the way
#: ``governance plan`` pins ``govern-plan``.
RECONCILE_RUN_ID = "govern-reconcile"


def _load_hmac_key() -> bytes:
    from bernstein.core.security.audit import load_or_create_audit_key

    return load_or_create_audit_key()


@click.command("reconcile")
@click.option(
    "--propose",
    is_flag=True,
    default=False,
    help="Compute the diff and record it. The only supported mode: nothing is applied.",
)
@click.option(
    "--desired",
    "desired_file",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="JSON desired-state document (entities with prune / self_heal).",
)
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    show_default=True,
    help="Project root containing .sdd/.",
)
@click.option(
    "--full",
    is_flag=True,
    default=False,
    help="Print one line per entity instead of only what drifted.",
)
def govern_reconcile_cmd(propose: bool, desired_file: str, workdir: str, full: bool) -> None:
    """Diff the governed surface against the desired state and record it.

    Exit codes: 0 = no drift, 1 = unreadable desired state, 2 = drift.
    """
    if not propose:
        click.echo("govern reconcile requires --propose; applying a diff is not implemented.", err=True)
        raise SystemExit(1)

    root = Path(workdir).resolve()
    try:
        desired = DesiredState.from_dict(json.loads(Path(desired_file).read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
        click.echo(f"unreadable desired state: {exc}", err=True)
        raise SystemExit(1) from exc

    now = int(time.time())
    snapshot = snapshot_surface(sdd_dir=root / ".sdd", observed_at=now)
    diff, decision = propose_reconcile(
        run_id=RECONCILE_RUN_ID,
        lineage_root=root / ".sdd" / "lineage",
        hmac_key=_load_hmac_key(),
        snapshot=snapshot,
        desired=desired,
        now=now,
    )

    if full:
        for entry in diff.entries:
            click.echo(f"state {_verdict_line(entry)}")
    for entry in diff.drifted:
        click.echo(f"drift {_verdict_line(entry)}")

    click.echo(f"entities {len(diff.entries)}  drifted {len(diff.drifted)}")
    click.echo(f"record {decision.journal_entry_hash}")
    if not diff.drifted:
        click.echo(f"no drift -- {len(diff.entries)} entities, all unchanged")
        raise SystemExit(0)
    raise SystemExit(2)


def _verdict_line(entry: ReconcileEntry) -> str:
    """Render one entity's verdict as a single machine-countable line.

    Values are JSON-quoted so a value containing spaces (a cron expression,
    say) cannot be mistaken for the start of the next field.
    """
    return (
        f"{entry.status.value} {entry.kind.value}:{entry.entity_id} "
        f"observed={json.dumps(entry.observed_value)} "
        f"declared={json.dumps(entry.declared_value)} "
        f"action={entry.action.value}"
    )


__all__ = [
    "RECONCILE_RUN_ID",
    "govern_reconcile_cmd",
]
