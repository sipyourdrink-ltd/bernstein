"""``bernstein gate``: verify signed maker-checker / judge-panel adjudications.

Issue #2294. Each phase-gate boundary writes a signed adjudication record

    {inputs_hash, rubric_hash, panel_config, per_judge_verdict, final_verdict}

anchored to the run's lineage spine. ``gate verify <run>`` recomputes
``inputs_hash`` from the claimed inputs and confirms the recorded panel saw
exactly those inputs, then confirms the record is still anchored in a spine
that itself verifies:

    bernstein gate verify <run> --inputs inputs.json

Exit codes: 0 = verified, 1 = no record / bad input, 2 = mismatch or
command-line usage error.
"""

from __future__ import annotations

import json
from pathlib import Path

import click

from bernstein.cli.helpers import console
from bernstein.core.security.audit import load_or_create_audit_key

_EXIT_BAD_INPUT_OR_NO_RECORD = 1
_EXIT_MISMATCH = 2


def _lineage_root(workdir: Path) -> Path:
    return workdir / ".sdd" / "lineage"


@click.group("gate")
def gate_group() -> None:
    """Verify signed maker-checker / judge-panel gate adjudications.

    \b
      bernstein gate verify <run> --inputs inputs.json
    """


@gate_group.command("verify")
@click.argument("run_id", required=True)
@click.option(
    "--inputs",
    "inputs_file",
    required=True,
    type=click.Path(exists=True, dir_okay=False),
    help="Canonical JSON of the inputs the panel is claimed to have seen.",
)
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    show_default=True,
    help="Project root containing .sdd/.",
)
def gate_verify_cmd(run_id: str, inputs_file: str, workdir: str) -> None:
    """Recompute ``inputs_hash`` for *run_id* and confirm the panel saw it.

    A record whose recomputed ``inputs_hash`` matches the claimed inputs, whose
    panel is independent, and whose spine anchor still verifies passes. If no
    record for the run matches the claimed inputs, the command exits non-zero.
    """
    from bernstein.core.quality.adjudication import (
        _canonical_json,
        read_records,
        verify_adjudication,
    )

    root = Path(workdir).resolve()
    try:
        claimed = json.loads(Path(inputs_file).read_text(encoding="utf-8"))
        # JSON escapes can decode to lone surrogates, which the inputs hash
        # cannot encode as UTF-8. Check the claim before reading records.
        _canonical_json(claimed)
    except (OSError, ValueError, RecursionError) as exc:
        raise click.ClickException(f"Invalid JSON in --inputs: {exc}") from exc

    key = load_or_create_audit_key()
    lineage_root = _lineage_root(root)

    records = read_records(lineage_root, run_id)
    if not records:
        click.echo(f"NO RECORD -- no adjudication records for run {run_id!r}", err=True)
        raise SystemExit(_EXIT_BAD_INPUT_OR_NO_RECORD)

    results = [
        verify_adjudication(
            run_id=run_id,
            lineage_root=lineage_root,
            hmac_key=key,
            record=record,
            claimed_inputs=claimed,
        )
        for record in records
    ]
    if any(result.ok for result in results):
        console.print()
        console.print(f"[bold]Gate verify[/bold] run={run_id}")
        console.print(f"  records   {len(records)}")
        console.print("[green]OK[/green] -- the panel saw the claimed inputs; record anchored and verified.")
        raise SystemExit(0)

    reason = next((r.reason for r in results if r.reason), "no matching record")
    click.echo(f"MISMATCH -- {reason}", err=True)
    raise SystemExit(_EXIT_MISMATCH)


__all__ = ["gate_group"]
