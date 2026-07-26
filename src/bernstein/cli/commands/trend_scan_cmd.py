"""CLI surface for the scheduled trend-scan job.

``bernstein trend-scan run`` ingests upstream dependency-relevant signals
into the orchestrator backlog directory as a markdown rollup. No tickets are
filed automatically.

Network access is opt-in: the default fetcher is the offline stub
(``--offline-stub`` / ``BERNSTEIN_TREND_SCAN_OFFLINE=1``), which returns an
empty result set. Operators wire a real fetcher by passing ``--fetcher-cmd``
that points to an executable returning one JSON item per line on stdout.
This keeps the CLI testable in CI without leaking outbound HTTP into unit
tests.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import click

from bernstein.core.devops.trend_scan import (
    RawItem,
    SourceSpec,
    TrendScanConfig,
    load_default_sources,
    run_scan,
)

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = ["trend_scan_group"]


def _parse_sources_json(path: Path) -> tuple[SourceSpec, ...]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        msg = "--sources file must contain a JSON array of source specs"
        raise click.BadParameter(msg)
    specs: list[SourceSpec] = []
    for entry in raw:
        if not isinstance(entry, dict):
            msg = "each source spec must be an object"
            raise click.BadParameter(msg)
        try:
            specs.append(
                SourceSpec(
                    name=str(entry["name"]),
                    tier=int(entry["tier"]),  # type: ignore[arg-type]
                    keywords=tuple(entry.get("keywords", ())),
                    boost_keywords=tuple(entry.get("boost_keywords", ())),
                    negative_keywords=tuple(entry.get("negative_keywords", ())),
                    min_score=float(entry.get("min_score", 0.5)),
                )
            )
        except (KeyError, TypeError, ValueError) as exc:
            msg = f"invalid source spec entry: {exc}"
            raise click.BadParameter(msg) from exc
    return tuple(specs)


def _filter_by_tier(sources: Iterable[SourceSpec], tier: str) -> tuple[SourceSpec, ...]:
    if tier == "all":
        return tuple(sources)
    want = int(tier)
    return tuple(s for s in sources if s.tier == want)


def _stub_fetcher(_spec: SourceSpec) -> Iterable[RawItem]:
    """Offline stub. Returns no items.

    Used when the operator has not wired an external fetcher. The rollup
    still gets written (with a "no candidates" body), which is useful for
    smoke-testing the scheduled workflow.
    """

    return ()


def _make_subprocess_fetcher(fetcher_cmd: str, timeout: float):
    def _fetch(spec: SourceSpec) -> Iterable[RawItem]:
        argv = [*shlex.split(fetcher_cmd), spec.name, str(spec.tier)]
        try:
            completed = subprocess.run(
                argv,
                check=False,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            click.echo(f"trend-scan: fetcher timed out for source={spec.name}", err=True)
            return ()
        if completed.returncode != 0:
            click.echo(
                f"trend-scan: fetcher exit {completed.returncode} for source={spec.name}: "
                f"{completed.stderr.strip()[:200]}",
                err=True,
            )
            return ()
        items: list[RawItem] = []
        for raw_line in completed.stdout.splitlines():
            line = raw_line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except json.JSONDecodeError:
                click.echo(f"trend-scan: ignoring non-JSON line from source={spec.name}", err=True)
                continue
            try:
                items.append(
                    RawItem(
                        title=str(payload["title"]),
                        url=str(payload.get("url", "")),
                        ts=str(payload.get("ts", "")),
                        body=str(payload.get("body", "")),
                    )
                )
            except (KeyError, TypeError):
                click.echo(f"trend-scan: ignoring malformed item from source={spec.name}", err=True)
                continue
        return items

    return _fetch


@click.group(name="trend-scan")
def trend_scan_group() -> None:
    """Scheduled upstream-signal sweep.

    Produces an operator-reviewable markdown rollup. No tickets are filed
    automatically; the operator decides which rows become tickets.
    """


@trend_scan_group.command("run")
@click.option(
    "--tier",
    type=click.Choice(["all", "1", "2", "3"]),
    default="all",
    show_default=True,
    help="Which tier(s) to scan.",
)
@click.option(
    "--output",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Rollup output path. Defaults to .sdd/trend-scan/rollup-<date>.md.",
)
@click.option(
    "--backlog-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path(".sdd/backlog"),
    show_default=True,
    help="Backlog directory used for gap analysis.",
)
@click.option(
    "--rollup-dir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path(".sdd/trend-scan"),
    show_default=True,
    help="Directory for rollup files when --output is not set.",
)
@click.option(
    "--sources",
    "sources_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=None,
    help="JSON file overriding the default source specs.",
)
@click.option(
    "--fetcher-cmd",
    type=str,
    default=None,
    help="Executable that returns one JSON item per line on stdout. "
    "Invoked as: <cmd> <source_name> <tier>. If unset, use --offline-stub.",
)
@click.option(
    "--fetcher-timeout",
    type=float,
    default=30.0,
    show_default=True,
    help="Per-source fetcher timeout in seconds.",
)
@click.option(
    "--max-per-source",
    type=click.IntRange(min=1, max=50),
    default=5,
    show_default=True,
    help="Cap on candidates surfaced per source.",
)
@click.option(
    "--offline-stub/--no-offline-stub",
    default=False,
    help="Force the offline stub fetcher (no network).",
)
def trend_scan_run(
    tier: str,
    output: Path | None,
    backlog_dir: Path,
    rollup_dir: Path,
    sources_path: Path | None,
    fetcher_cmd: str | None,
    fetcher_timeout: float,
    max_per_source: int,
    offline_stub: bool,
) -> None:
    """Run the scan and write the rollup."""

    sources = _parse_sources_json(sources_path) if sources_path is not None else load_default_sources()

    selected = _filter_by_tier(sources, tier)
    if not selected:
        click.echo(f"trend-scan: no sources selected for tier={tier}", err=True)
        sys.exit(2)

    config = TrendScanConfig(
        sources=selected,
        backlog_dir=backlog_dir,
        rollup_dir=rollup_dir,
        max_candidates_per_source=max_per_source,
    )

    if offline_stub or os.environ.get("BERNSTEIN_TREND_SCAN_OFFLINE") == "1":
        fetcher = _stub_fetcher
    elif fetcher_cmd is None:
        click.echo(
            "trend-scan: no fetcher configured. Pass --fetcher-cmd <executable>, "
            "or --offline-stub to run the no-op stub fetcher.",
            err=True,
        )
        sys.exit(2)
    else:
        fetcher = _make_subprocess_fetcher(fetcher_cmd, fetcher_timeout)

    result = run_scan(config, fetcher=fetcher, output_path=output)

    click.echo(f"trend-scan: wrote rollup with {len(result.candidates)} candidates -> {result.rollup_path}")
