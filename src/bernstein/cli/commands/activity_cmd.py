"""``bernstein activity``: verify typed activity boundary crossings (#2311).

Bernstein's deterministic scheduler dispatches any agent modality -- research,
browser/computer-use, data, ops, coding -- behind one typed activity boundary,
anchoring each crossing into the run's canonical event journal as an
``activity.result`` entry that pins the ``evidence_set_hash`` (a pure function of
the content-addressed evidence the activity gathered).

    bernstein activity verify <run>

``verify`` walks the run journal, recomputes each anchored activity's
``evidence_set_hash`` from its pinned observation hashes, and -- when the run's
content store is present -- reattaches the evidence bytes and re-verifies every
content hash. A tampered journal entry or a divergent stored blob fails. Exit
codes: 0 = verified, 1 = no run / no activity, 2 = mismatch (tamper).
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from bernstein.cli.helpers import console


@click.group(name="activity")
def activity_group() -> None:
    """Typed activity-boundary tooling for any agent modality.

    \b
    Examples:
      bernstein activity verify run-42
      bernstein activity verify run-42 --json
    """


@activity_group.command("verify")
@click.argument("run")
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    show_default=True,
    help="Project root containing .sdd/.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Emit machine-readable JSON.")
def activity_verify_cmd(run: str, workdir: str, as_json: bool) -> None:
    """Recompute and re-verify every activity anchored in RUN's journal.

    Confirms the journal's Merkle chain is intact, recomputes each activity's
    ``evidence_set_hash`` from its pinned observation hashes, and reattaches the
    evidence bytes from the run's content store (when present). Exit codes:
    0 = verified, 1 = no run / no activity, 2 = mismatch (tamper).
    """
    from bernstein.core.orchestration.activity_modalities import (
        ContentStore,
        verify_run_activities,
    )

    root = Path(workdir).resolve()
    sdd_dir = root / ".sdd"
    cas_dir = sdd_dir / "cas"
    store = ContentStore(cas_dir) if cas_dir.exists() else None

    result = verify_run_activities(sdd_dir, run_id=run, store=store)

    if as_json:
        payload = {
            "run": result.run_id,
            "found": result.found,
            "ok": result.ok,
            "chain_ok": result.chain_ok,
            "reason": result.reason,
            "stages": [
                {
                    "stage_id": s.stage_id,
                    "kind": s.kind,
                    "ok": s.ok,
                    "evidence_reattached": s.evidence_reattached,
                    "reason": s.reason,
                }
                for s in result.stages
            ],
        }
        console.print_json(json.dumps(payload))
    else:
        console.print()
        console.print(f"[bold]Activity verify[/bold] run={result.run_id}")
        if not result.found:
            console.print(f"[yellow]NO ACTIVITY[/yellow] -- {result.reason}")
        else:
            for stage in result.stages:
                if stage.ok:
                    tag = "[green]OK[/green]"
                    extra = " (evidence reattached)" if stage.evidence_reattached else ""
                    console.print(f"  {tag} {stage.stage_id} [{stage.kind}]{extra}")
                else:
                    console.print(f"  [red]MISMATCH[/red] {stage.stage_id} [{stage.kind}] -- {stage.reason}")
            if result.ok:
                console.print("[green]verified[/green] -- every activity reconstructs from the journal.")

    if not result.found:
        raise SystemExit(1)
    if not result.ok:
        raise SystemExit(2)
    raise SystemExit(0)


__all__ = ["activity_group"]
