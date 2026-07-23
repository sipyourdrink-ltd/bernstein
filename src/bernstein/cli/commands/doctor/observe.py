"""``bernstein doctor observe`` -- unified observability surface.

Runs every per-backend probe in order (Code Scanning), aggregates them
into a single operator-facing table, and supports JSON output plus a
``--watch`` mode that re-runs every 60s.

Each backend soft-fails to ``SKIPPED`` when its credentials are not
configured, so the umbrella keeps running and the operator sees which
backends are wired at a glance.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

import click

if TYPE_CHECKING:
    from collections.abc import Callable

from bernstein.cli.commands.doctor._render import render_aggregate
from bernstein.cli.commands.doctor.backends import (
    BackendReport,
    ProbeStatus,
    probe_code_scanning,
)
from bernstein.cli.helpers import console

_LOGGER = logging.getLogger(__name__)

#: Backend order matters: it controls table grouping and the JSON
#: serialisation order.
DEFAULT_PROBES: tuple[tuple[str, Callable[[], BackendReport]], ...] = (("code-scanning", probe_code_scanning),)

DEFAULT_WATCH_INTERVAL = 60


def collect_reports(
    probes: tuple[tuple[str, Callable[[], BackendReport]], ...] | None = None,
) -> list[BackendReport]:
    """Run each probe in order and return the list of reports.

    Probes are isolated: a crash in one probe is reported as an
    ``ERROR`` row but does not stop the others. The function never
    raises.

    ``probes`` defaults to ``None`` so callers always pick up the
    current module-level ``DEFAULT_PROBES`` (helpful when tests
    monkeypatch the constant).
    """

    if probes is None:
        probes = DEFAULT_PROBES
    reports: list[BackendReport] = []
    for name, probe in probes:
        try:
            reports.append(probe())
        except Exception as exc:
            # Log the full error for debugging, but only persist the
            # exception type in the report so raw messages (which may
            # carry tokens or URLs) never reach snapshots or stdout.
            _LOGGER.debug("probe %r crashed", name, exc_info=True)
            reports.append(
                BackendReport(
                    backend=name,
                    status=ProbeStatus.ERROR,
                    error=f"unhandled probe error: {type(exc).__name__}",
                )
            )
    return reports


@click.command("observe")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit aggregated JSON instead of the Rich table.",
)
@click.option(
    "--watch",
    "watch",
    is_flag=True,
    default=False,
    help="Re-run every 60s until interrupted (Ctrl-C to stop).",
)
@click.option(
    "--interval",
    "interval",
    type=int,
    default=DEFAULT_WATCH_INTERVAL,
    show_default=True,
    help="Watch interval in seconds (only meaningful with --watch).",
)
@click.option(
    "--no-persist",
    "no_persist",
    is_flag=True,
    default=False,
    help="Do not write the snapshot cache used for delta-since-last-check.",
)
def observe_cmd(as_json: bool, watch: bool, interval: int, no_persist: bool) -> None:
    """Aggregate observability backends (Code Scanning).

    \b
    The umbrella runs each per-backend probe in order. Backends that
    are not configured soft-fail to SKIPPED so the umbrella keeps going
    on a fresh checkout.

    \b
    Examples:
      bernstein doctor observe
      bernstein doctor observe --json
      bernstein doctor observe --watch
      bernstein doctor observe --watch --interval 30
    """

    interval = max(1, interval)
    persist = not no_persist

    def _one_pass() -> int:
        reports = collect_reports()
        return render_aggregate(
            reports,
            console=console,
            as_json=as_json,
            persist=persist,
        )

    if not watch:
        raise SystemExit(_one_pass())

    if as_json:
        try:
            while True:
                _one_pass()
                time.sleep(interval)
        except KeyboardInterrupt:
            raise SystemExit(0) from None

    try:
        while True:
            console.clear()
            exit_code = _one_pass()
            console.print(f"[dim]watch: refreshes every {interval}s. last exit={exit_code}. Ctrl-C to stop.[/dim]")
            time.sleep(interval)
    except KeyboardInterrupt:
        raise SystemExit(0) from None


def register(parent: click.Group) -> None:
    """Attach the umbrella subcommand to the parent ``doctor`` group."""

    parent.add_command(observe_cmd)
