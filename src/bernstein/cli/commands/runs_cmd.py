"""``bernstein runs`` -- operator surface over finished runs (#4465).

The classification rule lives in
:mod:`bernstein.core.persistence.runs_report`, next to the ledger it reads.
This module is the thin renderer on top: it turns CLI flags into a
:func:`~bernstein.core.persistence.runs_report.list_finished_runs` call and
prints either a table or the stable ``--json`` rows.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import click
from rich.table import Table

from bernstein.cli.helpers import console
from bernstein.core.observability.decision_log import parse_duration
from bernstein.core.persistence.run_scorecard import (
    RunScorecard,
    VerifyResult,
    build_run_scorecard,
    read_scorecard_artifact,
    verify_scorecard,
    write_scorecard_artifact,
)
from bernstein.core.persistence.runs_report import RunOutcome, list_finished_runs
from bernstein.core.persistence.work_ledger import WorkLedger, run_ledger_dir

#: Rich style per outcome class, for the table renderer only -- the
#: ``--json`` rows never carry color.
_OUTCOME_STYLE: dict[RunOutcome, str] = {
    RunOutcome.PR_OPENED: "green",
    RunOutcome.GATE_FAILED: "red",
    RunOutcome.NO_CHANGES: "dim",
    RunOutcome.INFRA_ERROR: "red",
    RunOutcome.WEDGED: "yellow",
}


@click.group("runs")
def runs_group() -> None:
    """Operator surface over runs projected from the work ledger."""


@runs_group.command("report")
@click.option(
    "--since",
    default=None,
    help='Only include runs started in the last DURATION, e.g. "6h", "2d".',
)
@click.option(
    "--workdir",
    default=None,
    type=click.Path(file_okay=False, path_type=Path),
    help="Project root (defaults to current directory).",
)
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    default=False,
    help="Emit stable machine-readable rows instead of a table.",
)
def runs_report_cmd(since: str | None, workdir: Path | None, output_json: bool) -> None:
    """List finished runs with a classified outcome and one-line evidence.

    \b
    Outcome classes:
      pr-opened     branch published
      gate-failed   a quality gate blocked the run
      no-changes    zero commits over base
      infra-error   adapter/transport death, or no wrap-up was ever recorded
      wedged        the run ended with unspawnable open tasks

    \b
      bernstein runs report                 # every run in the ledger
      bernstein runs report --since 6h      # only runs started in the last 6 hours
      bernstein runs report --json          # stable machine-readable rows
    """
    root = (workdir or Path.cwd()).resolve()
    cutoff = time.time() - parse_duration(since) if since else None
    runs = list_finished_runs(root / ".sdd", since=cutoff)

    if output_json:
        console.print_json(json.dumps({"runs": [run.to_dict() for run in runs]}))
        return

    if not runs:
        console.print("[dim]No finished runs in this repository's ledger.[/dim]")
        return

    table = Table(show_header=True, header_style="bold")
    table.add_column("Run")
    table.add_column("Branch")
    table.add_column("Outcome")
    table.add_column("Evidence")
    for run in runs:
        style = _OUTCOME_STYLE.get(run.outcome, "")
        outcome_text = f"[{style}]{run.outcome.value}[/{style}]" if style else run.outcome.value
        table.add_row(run.run_id, run.branch or "-", outcome_text, run.evidence)
    console.print(table)


@runs_group.command("scorecard")
@click.argument("run_id")
@click.option(
    "--workdir",
    default=None,
    type=click.Path(file_okay=False, path_type=Path),
    help="Project root (defaults to current directory).",
)
@click.option(
    "--verify/--no-verify",
    default=False,
    help="Recompute the scorecard from the live ledger and compare bytes.",
)
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    default=False,
    help="Emit the scorecard content as JSON instead of a one-line summary.",
)
def runs_scorecard_cmd(run_id: str, workdir: Path | None, verify: bool, output_json: bool) -> None:
    """Build or verify the per-run scorecard (#5404).

    By default builds the scorecard from the run's work ledger and writes
    the content-addressed artifact under ``<root>/.sdd/runs/<run_id>/scorecard/``.
    Pass ``--verify`` to re-derive and compare instead of writing. The
    scorecard is the deterministic, content-addressed projection of a
    run's work ledger -- the same facts ``bernstein runs report`` already
    classifies plus a small set of counters (steps, tasks, cost_usd,
    host, parent_run_id, attempt_count, elapsed_seconds).

    \b
    Modes:
      default      build and write the artifact to .sdd/runs/<id>/scorecard/<sha256>.json
      --verify     recompute from the live ledger and compare to the on-disk artifact
      --json       print the scorecard content as JSON instead of a one-line summary

    \b
      bernstein runs scorecard run-20260901T120000p1234567Z
      bernstein runs scorecard <run-id> --verify
      bernstein runs scorecard <run-id> --json
    """
    root = (workdir or Path.cwd()).resolve()
    ledger_dir = run_ledger_dir(root / ".sdd", run_id)
    if not ledger_dir.exists():
        raise click.ClickException(f"work ledger not found for run {run_id!r} at {ledger_dir}")

    if verify:
        artifact_dir = root / ".sdd" / "runs" / run_id / "scorecard"
        artifacts = sorted(artifact_dir.glob("*.json")) if artifact_dir.exists() else []
        if not artifacts:
            raise click.ClickException(f"no scorecard artifact under {artifact_dir}; run without --verify first")
        # When multiple artifacts exist, pick the one whose content
        # matches the live ledger (if any). Otherwise the most recent
        # by mtime.
        chosen = artifacts[-1]
        for candidate in reversed(artifacts):
            try:
                read_scorecard_artifact(candidate)
            except (ValueError, OSError):
                continue
            chosen = candidate
            break
        result: VerifyResult = verify_scorecard(root, run_id, chosen)
        if output_json:
            console.print_json(
                json.dumps(
                    {
                        "ok": result.ok,
                        "artifact_sha256": result.artifact_sha256,
                        "recomputed_sha256": result.recomputed_sha256,
                        "description": result.description,
                        "artifact_path": str(chosen),
                    }
                )
            )
        elif result.ok:
            console.print(f"[green]OK[/green] {result.description} (sha {result.artifact_sha256[:16]}...)")
        else:
            console.print(f"[red]FAIL[/red] {result.description}")
        if not result.ok:
            raise click.ClickException("scorecard verification failed")
        return

    with WorkLedger.open(ledger_dir) as journal:
        scorecard: RunScorecard = build_run_scorecard(journal)
    out_path = write_scorecard_artifact(root, run_id, scorecard)

    if output_json:
        envelope = {
            "artifact": str(out_path),
            "sha256": scorecard.sha256,
            "content": scorecard.content,
        }
        console.print_json(json.dumps(envelope))
        return

    console.print(f"wrote {out_path} (sha {scorecard.sha256[:16]}..., outcome={scorecard.content.get('outcome', '?')})")


__all__ = ["runs_group"]
