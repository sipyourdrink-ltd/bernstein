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
from bernstein.core.persistence.runs_report import RunOutcome, list_finished_runs

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


__all__ = ["runs_group"]
