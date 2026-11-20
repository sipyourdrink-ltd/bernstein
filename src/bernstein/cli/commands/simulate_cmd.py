"""CLI surface for ``bernstein simulate`` (issue #1374).

Dry-runs a full multi-agent cycle on synthetic data with mock LLMs so
operators see decision flow, costs, and abandonment-rate predictions
BEFORE spending real tokens.

Usage::

    bernstein simulate <plan.yaml> [--from-traces N] [--seed S]
                                   [--budget-cap X] [--out report.json|report.md]

The command exits non-zero when the simulator cannot load the plan, or
when a ``--budget-cap`` is supplied and the predicted p90 spend breaches
it. Otherwise the human-readable Markdown summary is printed to stdout
and (optionally) the structured JSON sidecar is written to ``--out``.
"""

from __future__ import annotations

import sys
from pathlib import Path

import click

from bernstein.core.simulate import (
    SimulationError,
    SimulationOptions,
    render_json,
    render_markdown,
    simulate,
)

__all__ = ["simulate_cmd"]


_DEFAULT_METRICS_DIR = Path(".sdd/metrics")
_DEFAULT_TRACES_DIR = Path(".sdd/traces")


def _resolve_dir(explicit: str | None, default: Path) -> str | None:
    """Pick the dir to consult: explicit > default-if-exists > None."""
    if explicit is not None:
        return explicit
    if default.exists() and default.is_dir():
        return str(default)
    return None


@click.command("simulate")
@click.argument(
    "plan",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
)
@click.option(
    "--from-traces",
    "from_traces",
    type=click.IntRange(min=1),
    default=50,
    show_default=True,
    help="Maximum historical trace records to consult per (role, adapter).",
)
@click.option(
    "--seed",
    type=int,
    default=42,
    show_default=True,
    help="Random seed. Same seed + same plan + same traces yields a byte-identical report.",
)
@click.option(
    "--budget-cap",
    "budget_cap",
    type=click.FloatRange(min=0.0),
    default=None,
    help="USD ceiling for the run. Non-zero exit when the predicted p90 spend exceeds it.",
)
@click.option(
    "--out",
    "out_path",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Write a report file (.md or .markdown for Markdown, otherwise JSON). This does not change stdout.",
)
@click.option(
    "--metrics-dir",
    "metrics_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Path to .sdd/metrics. Defaults to ./.sdd/metrics when present, else cold-start.",
)
@click.option(
    "--traces-dir",
    "traces_dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Path to .sdd/traces. Defaults to ./.sdd/traces when present, else cold-start.",
)
@click.option(
    "--format",
    "fmt",
    type=click.Choice(("md", "json"), case_sensitive=False),
    default="md",
    show_default=True,
    help="Format for standard output.",
)
def simulate_cmd(
    plan: Path,
    from_traces: int,
    seed: int,
    budget_cap: float | None,
    out_path: Path | None,
    metrics_dir: Path | None,
    traces_dir: Path | None,
    fmt: str,
) -> None:
    """Digital-twin simulation of a plan against historical traces (issue #1374).

    PLAN is the YAML plan file to simulate. The simulator never spawns a
    real agent or hits the network - it only reads ``.sdd/traces`` and
    ``.sdd/metrics`` for calibration.
    """
    options = SimulationOptions(
        seed=seed,
        from_traces=from_traces,
        budget_cap=budget_cap,
        metrics_dir=_resolve_dir(str(metrics_dir) if metrics_dir else None, _DEFAULT_METRICS_DIR),
        traces_dir=_resolve_dir(str(traces_dir) if traces_dir else None, _DEFAULT_TRACES_DIR),
    )

    try:
        report = simulate(plan, options)
    except SimulationError as exc:
        click.echo(f"simulate: {exc}", err=True)
        sys.exit(2)

    if fmt.lower() == "json":
        click.echo(render_json(report))
    else:
        click.echo(render_markdown(report))

    if out_path is not None:
        suffix = out_path.suffix.lower()
        if suffix == ".json":
            payload = render_json(report)
        elif suffix in {".md", ".markdown"}:
            payload = render_markdown(report)
        else:
            # Default sidecar is JSON when extension is unknown so the
            # operator always gets a machine-readable artifact.
            payload = render_json(report)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(payload + "\n", encoding="utf-8")
        click.echo(f"\nReport written to {out_path}", err=True)

    if report.aggregate.budget_breach:
        click.echo(
            f"\nbudget cap ${options.budget_cap:.2f} breached by predicted p90 ${report.aggregate.total_cost_p90:.2f}",
            err=True,
        )
        sys.exit(3)
