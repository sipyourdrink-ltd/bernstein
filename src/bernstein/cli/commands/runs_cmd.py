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
    verify_scorecard,
    write_scorecard_artifact,
)
from bernstein.core.persistence.runs_report import RunOutcome, list_finished_runs
from bernstein.core.persistence.work_ledger import run_ledger_dir

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


def _scorecard_artifact_path(root: Path, run_id: str) -> Path | None:
    """Return the existing scorecard artifact path for *run_id* under *root*, if any."""
    from bernstein.core.persistence.run_scorecard import _scorecard_dir

    artifact_dir = _scorecard_dir(root, run_id)
    if not artifact_dir.exists():
        return None
    artifacts = sorted(artifact_dir.glob("*.json"))
    return artifacts[0] if artifacts else None


@runs_group.command("scorecard")
@click.argument("run_id")
@click.option(
    "--workdir",
    default=".",
    type=click.Path(file_okay=False, path_type=Path),
    show_default=True,
    help="Project root (defaults to current directory).",
)
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    default=False,
    help="Emit the scorecard envelope as JSON instead of a human summary.",
)
@click.option(
    "--verify",
    is_flag=True,
    default=False,
    help="Re-derive the scorecard from the live ledger and compare to the on-disk artifact.",
)
def runs_scorecard_cmd(run_id: str, workdir: Path, output_json: bool, verify: bool) -> None:
    """Build or verify the per-run scorecard (#5404).

    By default builds the scorecard from the run's work ledger and writes
    the content-addressed artifact under ``<root>/.sdd/runs/<run_id>/scorecard/``.
    Pass ``--verify`` to re-derive and compare instead of writing.
    """
    root = workdir.resolve()
    sdd_root = root / ".sdd"
    ledger_dir = run_ledger_dir(sdd_root, run_id)
    if not ledger_dir.exists():
        msg = f"work ledger not found for run {run_id!r} at {ledger_dir}"
        if output_json and verify:
            console.print_json(json.dumps({"ok": False, "description": msg}))
        else:
            console.print(msg)
        raise click.exceptions.Exit(1)

    if verify:
        artifact_path = _scorecard_artifact_path(root, run_id)
        if artifact_path is None:
            msg = f"no scorecard artifact found for run {run_id!r}; run without --verify first"
            if output_json:
                console.print_json(json.dumps({"ok": False, "description": msg}))
            else:
                console.print(msg)
            raise click.exceptions.Exit(1)
        result: VerifyResult = verify_scorecard(root, run_id, artifact_path)
        if output_json:
            payload = {
                "ok": result.ok,
                "artifact_sha256": result.artifact_sha256,
                "recomputed_sha256": result.recomputed_sha256,
                "description": result.description,
            }
            console.print_json(json.dumps(payload))
        else:
            verdict = "OK" if result.ok else "MISMATCH"
            console.print(f"{verdict}: {result.description}")
        if not result.ok:
            raise click.exceptions.Exit(1)
        return

    from bernstein.core.persistence.work_ledger import WorkLedger

    with WorkLedger.open(ledger_dir) as journal:
        scorecard: RunScorecard = build_run_scorecard(journal)
    out_path = write_scorecard_artifact(root, run_id, scorecard)

    if output_json:
        envelope = {"content": scorecard.content, "sha256": scorecard.sha256}
        console.print_json(json.dumps(envelope))
        return

    console.print(
        f"wrote scorecard for {run_id} (outcome={scorecard.content.get('outcome', '?')}) -> {out_path}"
    )


__all__ = ["runs_group"]
