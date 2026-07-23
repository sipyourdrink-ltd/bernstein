"""Day-over-day regression gate for ``bernstein doctor observe`` snapshots.

The daily workflow ``docs-observability-snapshot.yml`` appends one JSON
snapshot per day under ``docs/_internal/observability/snapshots/<YYYY-MM-DD>.json``.
Every metric row in a snapshot already carries a ``threshold_status``
(``ok|warn|fail``) computed at probe time. Until now that verdict was
written and never read. This gate diffs the two most recent snapshots and
turns the verdict into an actionable signal:

* a ``threshold_status`` flip for the worse (``ok -> warn`` / ``* -> fail``),
* a numeric worsening of a security metric (new or increased vulns/alerts),
* a backend that silently lost its credentials (``ok -> skipped/error``).

The numeric delta is computed here from the two snapshot files rather than
read from the row's own ``delta`` field: the daily job runs ``--no-persist``
so that field is inert (``"new"``/``"-"``).

Like its sibling ``render_trends.py`` the module is dependency-free (Python
standard library only) so it runs in CI and locally without any Bernstein
import::

    python scripts/observability/gate.py \\
        --snapshots docs/_internal/observability/snapshots \\
        --out .ci/observability/regressions.json

Exit code is ``1`` when at least one ``fail``-severity regression is found,
``0`` otherwise (including when fewer than two snapshots exist).
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

#: ``(backend, metric)`` security signals whose increase is release-blocking.
_FAIL_ON_INCREASE: frozenset[tuple[str, str]] = frozenset(
    {
        ("code-scanning", "critical_alerts"),
        ("code-scanning", "high_alerts"),
    }
)

#: ``(backend, metric)`` security signals whose increase is worth a warning.
_WARN_ON_INCREASE: frozenset[tuple[str, str]] = frozenset(
    {
        ("code-scanning", "open_alerts"),
    }
)

#: Backend-level statuses that mean the probe produced real data.
_ACTIVE_STATUSES: frozenset[str] = frozenset({"ok", "warn", "fail"})

_SENTINEL_METRIC = "__backend__"


@dataclass(frozen=True)
class Regression:
    """A single detected day-over-day regression."""

    backend: str
    metric: str
    prev: float | None
    curr: float | None
    delta: float | None
    status: str
    severity: str  # "fail" | "warn"
    reason: str


def _num(metric: dict[str, Any] | None) -> float | None:
    """Return the numeric value of a metric row, or ``None``."""

    if not metric:
        return None
    value = metric.get("numeric")
    if isinstance(value, bool):  # bool is an int subclass; never a metric value
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _fmt_delta(delta: float) -> str:
    """Format a numeric delta compactly (``+3`` / ``-1.4``)."""

    if abs(delta - round(delta)) < 1e-9:
        return f"{delta:+.0f}"
    return f"{delta:+.1f}"


def _classify_metric(
    backend: str,
    metric: str,
    prev: dict[str, Any] | None,
    curr: dict[str, Any],
) -> Regression | None:
    """Return a :class:`Regression` for one metric, or ``None`` if clean."""

    c_status = str(curr.get("threshold_status") or "")
    p_status = str(prev.get("threshold_status") or "") if prev else None
    c_num = _num(curr)
    p_num = _num(prev)
    delta = c_num - p_num if (c_num is not None and p_num is not None) else None

    worse = delta is not None and delta > 0

    key = (backend, metric)
    candidates: list[tuple[str, str]] = []

    # A metric that crosses its own fail threshold is always a fail regression.
    if c_status == "fail" and p_status != "fail":
        candidates.append(("fail", f"{p_status or 'new'}->fail"))
    # Critical / high security signals that increase are release-blocking.
    if key in _FAIL_ON_INCREASE and worse and delta is not None:
        candidates.append(("fail", f"{metric} {_fmt_delta(delta)}"))
    # A metric that slips from ok into warn is a warning regression.
    if c_status == "warn" and p_status == "ok":
        candidates.append(("warn", "ok->warn"))
    # New or increased lower-severity security signals warn.
    if key in _WARN_ON_INCREASE and worse and delta is not None:
        candidates.append(("warn", f"{metric} {_fmt_delta(delta)}"))

    if not candidates:
        return None

    severity = "fail" if any(sev == "fail" for sev, _ in candidates) else "warn"
    reason = next(reason for sev, reason in candidates if sev == severity)
    return Regression(
        backend=backend,
        metric=metric,
        prev=p_num,
        curr=c_num,
        delta=delta,
        status=c_status,
        severity=severity,
        reason=reason,
    )


def _backends(payload: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Index a snapshot's backends by name."""

    if not payload:
        return {}
    return {b.get("backend", "?"): b for b in payload.get("backends") or []}


def detect_regressions(
    prev: dict[str, Any] | None,
    curr: dict[str, Any] | None,
) -> list[Regression]:
    """Diff two snapshots and return the list of regressions.

    ``prev`` may be ``None`` (first ever snapshot); in that case only
    unambiguous ``fail``-status metrics are flagged and no lost-creds
    check runs. The function never raises on malformed input.
    """

    if not curr:
        return []

    regressions: list[Regression] = []
    curr_backends = _backends(curr)
    prev_backends = _backends(prev)

    # Metric-level regressions.
    for name, cb in curr_backends.items():
        pb = prev_backends.get(name)
        prev_metrics = {m.get("name"): m for m in (pb.get("metrics") if pb else []) or []}
        for cm in cb.get("metrics") or []:
            metric = cm.get("name")
            if not metric:
                continue
            reg = _classify_metric(name, metric, prev_metrics.get(metric), cm)
            if reg is not None:
                regressions.append(reg)

    # Backend-level lost-credentials regressions: a backend that produced
    # real data yesterday and is skipped / errored / gone today.
    for name, pb in prev_backends.items():
        if pb.get("status") not in _ACTIVE_STATUSES or not (pb.get("metrics") or []):
            continue
        cb = curr_backends.get(name)
        curr_status = cb.get("status") if cb else "absent"
        if curr_status not in _ACTIVE_STATUSES:
            regressions.append(
                Regression(
                    backend=name,
                    metric=_SENTINEL_METRIC,
                    prev=None,
                    curr=None,
                    delta=None,
                    status=str(curr_status),
                    severity="warn",
                    reason=f"backend lost creds ({pb.get('status')}->{curr_status})",
                )
            )

    return regressions


def _load(path: Path) -> dict[str, Any] | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def load_two_latest(
    snapshots_dir: Path,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Return ``(prev, curr)`` for the two most recent dated snapshots.

    Files are ordered by their ISO-date stem. When fewer than two valid
    snapshots exist, the missing slot(s) are ``None``.
    """

    if not snapshots_dir.exists():
        return (None, None)
    dated: list[tuple[dt.date, Path]] = []
    for path in snapshots_dir.glob("*.json"):
        try:
            day = dt.date.fromisoformat(path.stem)
        except ValueError:
            continue
        dated.append((day, path))
    dated.sort(key=lambda item: item[0])
    if not dated:
        return (None, None)
    curr = _load(dated[-1][1])
    prev = _load(dated[-2][1]) if len(dated) >= 2 else None
    return (prev, curr)


def _one_line(regressions: list[Regression]) -> str:
    if not regressions:
        return "observability gate: no regressions"
    fails = sum(1 for r in regressions if r.severity == "fail")
    warns = len(regressions) - fails
    return f"observability gate: {fails} fail, {warns} warn regression(s)"


def _render_summary(regressions: list[Regression]) -> str:
    """Render a Markdown summary block for the CI step summary."""

    lines = ["## Observability regression gate", "", _one_line(regressions), ""]
    if regressions:
        lines += ["| severity | backend | metric | prev | curr | reason |", "| --- | --- | --- | ---: | ---: | --- |"]
        for r in sorted(regressions, key=lambda x: (x.severity != "fail", x.backend, x.metric)):
            metric = "" if r.metric == _SENTINEL_METRIC else r.metric
            prev = "-" if r.prev is None else f"{r.prev:g}"
            curr = "-" if r.curr is None else f"{r.curr:g}"
            lines.append(f"| {r.severity} | {r.backend} | {metric} | {prev} | {curr} | {r.reason} |")
        lines.append("")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--snapshots",
        required=True,
        type=Path,
        help="Directory containing per-day observe JSON snapshots.",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Write the regressions list as JSON to this path.",
    )
    parser.add_argument(
        "--summary-out",
        type=Path,
        default=None,
        help="Append a Markdown summary block to this path (e.g. $GITHUB_STEP_SUMMARY).",
    )
    args = parser.parse_args(argv)

    prev, curr = load_two_latest(args.snapshots)
    regressions = detect_regressions(prev, curr)

    payload = [asdict(r) for r in regressions]
    if args.out is not None:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    if args.summary_out is not None:
        with args.summary_out.open("a", encoding="utf-8") as handle:
            handle.write(_render_summary(regressions))

    print(_one_line(regressions))
    for r in regressions:
        metric = "" if r.metric == _SENTINEL_METRIC else f".{r.metric}"
        print(f"  [{r.severity}] {r.backend}{metric}: {r.reason}")

    return 1 if any(r.severity == "fail" for r in regressions) else 0


if __name__ == "__main__":
    sys.exit(main())
