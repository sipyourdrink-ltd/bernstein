"""Render ``docs/observability/trends.md`` from snapshot JSON files.

The companion to :command:`bernstein doctor observe`. The
``docs-observability-snapshot.yml`` workflow is manual-only
(``workflow_dispatch``, see #2856) and writes one JSON file per
dispatch, named by date, under
``docs/_internal/observability/snapshots/<YYYY-MM-DD>.json`` (the raw
``--json`` payload from ``observe``). Dispatches are sporadic, not
daily, so this script works off whichever snapshot dates actually
exist rather than assuming one sample per calendar day: it reads the
last N *captured snapshots*, picks the headline metric for every
backend, and renders a Markdown document with ASCII sparklines.

The renderer is deliberately dependency-free: it uses only the Python
standard library plus a small unicode sparkline block alphabet. Run it
locally without any Bernstein dependency:

    python scripts/observability/render_trends.py \\
        --snapshots-dir docs/_internal/observability/snapshots \\
        --out docs/observability/trends.md \\
        --days 30
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from typing import Any

# Headline metrics by backend. The trend renderer pulls these out of
# each captured snapshot and plots them as sparklines.
HEADLINE_METRICS: dict[str, list[str]] = {
    "code-scanning": ["open_alerts", "critical_alerts", "high_alerts"],
}

SPARK_BLOCKS = " ▁▂▃▄▅▆▇█"


def _sparkline(values: list[float | None]) -> str:
    """Render a unicode-block sparkline for ``values``.

    ``None`` values render as a space so a metric missing from a given
    snapshot is visible at its actual position rather than collapsed.
    """

    if not values:
        return ""
    finite = [v for v in values if v is not None]
    if not finite:
        return " " * len(values)
    peak = max(finite) or 1.0
    out: list[str] = []
    for v in values:
        if v is None:
            out.append(" ")
            continue
        idx = min(len(SPARK_BLOCKS) - 1, int((v / peak) * (len(SPARK_BLOCKS) - 1)))
        out.append(SPARK_BLOCKS[idx])
    return "".join(out)


def _load_snapshots(snapshots_dir: Path, days: int) -> list[tuple[dt.date, dict[str, Any]]]:
    """Return at most ``days`` of snapshots, oldest first."""

    if not snapshots_dir.exists():
        return []
    entries: list[tuple[dt.date, dict[str, Any]]] = []
    for path in sorted(snapshots_dir.glob("*.json")):
        try:
            day = dt.date.fromisoformat(path.stem)
        except ValueError:
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        entries.append((day, payload))
    return entries[-days:]


def _extract_metric(payload: dict[str, Any], backend: str, metric: str) -> float | None:
    """Return the numeric value of ``metric`` for ``backend`` or None."""

    for b in payload.get("backends") or []:
        if b.get("backend") != backend:
            continue
        for m in b.get("metrics") or []:
            if m.get("name") == metric:
                num = m.get("numeric")
                if isinstance(num, (int, float)):
                    return float(num)
        return None
    return None


def render_markdown(
    snapshots: list[tuple[dt.date, dict[str, Any]]],
    *,
    days: int,
    now: dt.date | None = None,
) -> str:
    """Render the trends Markdown body from the loaded snapshots.

    ``now`` is the reference date for the staleness line; it defaults to
    the current UTC date and is only overridden by tests.
    """

    if not snapshots:
        return (
            "# Observability trends\n\n"
            "_No snapshots captured yet. The `docs-observability-snapshot.yml` "
            "workflow (dispatched manually - see #2856) populates "
            "`docs/_internal/observability/snapshots/<date>.json`._\n"
        )

    if now is None:
        now = dt.datetime.now(dt.UTC).date()

    first = snapshots[0][0]
    last = snapshots[-1][0]
    span_days = (last - first).days
    staleness_days = (now - last).days
    lines = [
        "# Observability trends",
        "",
        f"_Window: {first.isoformat()} -> {last.isoformat()} "
        f"({len(snapshots)} snapshot(s) captured across {span_days} day(s); target {days} snapshots)_",
        "",
        f"_Newest snapshot: {last.isoformat()} ({staleness_days} day(s) old)_",
        "",
        "Sparklines are rendered with unicode block characters. Each tick is "
        "one captured snapshot from `bernstein doctor observe --json`, in "
        "capture order - ticks are not evenly spaced in time, since "
        "dispatches are manual and sporadic. A metric absent from a given "
        "snapshot renders as a blank tick.",
        "",
    ]
    for backend, metrics in HEADLINE_METRICS.items():
        lines.extend(_render_backend_block(backend, metrics, snapshots))
    return "\n".join(lines) + "\n"


def _render_backend_block(
    backend: str,
    metrics: list[str],
    snapshots: list[tuple[dt.date, dict[str, Any]]],
) -> list[str]:
    block: list[str] = [
        f"## {backend}",
        "",
        "| metric | sparkline | first | latest | min | max |",
        "| --- | --- | ---: | ---: | ---: | ---: |",
    ]
    any_value = False
    for metric in metrics:
        series: list[float | None] = [_extract_metric(payload, backend, metric) for _, payload in snapshots]
        finite = [v for v in series if v is not None]
        if not finite:
            block.append(f"| {metric} | _(no data)_ | - | - | - | - |")
            continue
        any_value = True
        spark = _sparkline(series)
        block.append(
            f"| {metric} | `{spark}` | {finite[0]:.2f} | {finite[-1]:.2f} | {min(finite):.2f} | {max(finite):.2f} |"
        )
    block.append("")
    if not any_value:
        block.append("_(no numeric data for this backend in the current window)_")
        block.append("")
    return block


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--snapshots-dir",
        required=True,
        type=Path,
        help="Directory containing per-day observe JSON snapshots.",
    )
    parser.add_argument(
        "--out",
        required=True,
        type=Path,
        help="Markdown output path.",
    )
    parser.add_argument(
        "--days",
        type=int,
        default=30,
        help="Number of trailing captured snapshots to render (not calendar days - dispatches are sporadic).",
    )
    args = parser.parse_args(argv)

    if args.days < 1:
        parser.error("--days must be a positive integer")

    snapshots = _load_snapshots(args.snapshots_dir, days=args.days)
    md = render_markdown(snapshots, days=args.days)
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(md, encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
