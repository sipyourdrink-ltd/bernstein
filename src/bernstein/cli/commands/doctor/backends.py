"""Backend probes for the observability doctor.

Each probe returns a :class:`BackendReport` with a list of metric rows
plus an overall :class:`ProbeStatus`. Probes are deliberately small,
synchronous, and tolerant of missing credentials: when a backend is not
configured the probe returns ``status=ProbeStatus.SKIPPED`` with an
empty metric list so the umbrella command can keep going.

Persistence: each probe caches its last numeric values to
``.sdd/observability/<backend>.json`` so the next run can compute a
``delta-since-last-check`` column. The cache is operator-readable JSON
and may be deleted at any time without breaking the probe.

"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
import os
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

_LOGGER = logging.getLogger(__name__)


class ProbeStatus(StrEnum):
    """Overall status reported by a single backend probe."""

    OK = "ok"
    WARN = "warn"
    FAIL = "fail"
    SKIPPED = "skipped"
    ERROR = "error"


@dataclass
class MetricRow:
    """A single metric row in a backend report."""

    name: str
    value: str
    numeric: float | None = None
    threshold: str = ""
    threshold_status: str = "info"
    delta: str = "-"


@dataclass
class BackendReport:
    """Result of a single backend probe."""

    backend: str
    status: ProbeStatus
    detail: str = ""
    metrics: list[MetricRow] = field(default_factory=list)
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """JSON-serialisable view."""

        return {
            "backend": self.backend,
            "status": self.status.value,
            "detail": self.detail,
            "error": self.error,
            "metrics": [dataclasses.asdict(m) for m in self.metrics],
        }


def _cache_dir(workdir: Path | None = None) -> Path:
    root = workdir or Path.cwd()
    return root / ".sdd" / "observability"


def load_previous(backend: str, workdir: Path | None = None) -> dict[str, float]:
    """Return the previous numeric snapshot for ``backend``."""

    path = _cache_dir(workdir) / f"{backend}.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}
    metrics = data.get("metrics", {})
    return {k: float(v) for k, v in metrics.items() if isinstance(v, (int, float))}


def save_snapshot(report: BackendReport, workdir: Path | None = None) -> None:
    """Persist the numeric metrics from ``report`` for the next run."""

    if report.status in (ProbeStatus.SKIPPED, ProbeStatus.ERROR):
        return
    cache_dir = _cache_dir(workdir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "backend": report.backend,
        "captured_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "metrics": {m.name: m.numeric for m in report.metrics if m.numeric is not None},
    }
    (cache_dir / f"{report.backend}.json").write_text(
        json.dumps(payload, indent=2, sort_keys=True),
        encoding="utf-8",
    )


def apply_deltas(report: BackendReport, workdir: Path | None = None) -> BackendReport:
    """Annotate each metric row with a delta-since-last-check label."""

    previous = load_previous(report.backend, workdir=workdir)
    for row in report.metrics:
        if row.numeric is None:
            row.delta = "-"
            continue
        old = previous.get(row.name)
        if old is None:
            row.delta = "new"
            continue
        diff = row.numeric - old
        if abs(diff) < 1e-9:
            row.delta = "0"
        else:
            row.delta = f"{diff:+.2f}".rstrip("0").rstrip(".")
    return report


def _classify(
    value: float,
    *,
    warn_above: float | None = None,
    fail_above: float | None = None,
) -> str:
    """Bucket a numeric value into ``ok|warn|fail`` against thresholds."""

    if fail_above is not None and value >= fail_above:
        return "fail"
    if warn_above is not None and value >= warn_above:
        return "warn"
    return "ok"


def _security_fail_threshold(severity: str) -> int | None:
    """Return the failure threshold for security severity buckets."""

    if severity == "critical":
        return 1
    if severity == "high":
        return 5
    return None


def probe_code_scanning(env: dict[str, str] | None = None) -> BackendReport:
    """Probe GitHub Code Scanning alerts.

    Reads ``GITHUB_TOKEN`` (with ``security_events: read``) and
    ``GITHUB_REPOSITORY`` (``owner/repo``) from env. Soft-fails if
    either is missing.
    """

    env = env or os.environ.copy()
    token = (env.get("GITHUB_TOKEN") or "").strip()
    repo = (env.get("GITHUB_REPOSITORY") or "").strip()
    if not token or not repo:
        return BackendReport(
            backend="code-scanning",
            status=ProbeStatus.SKIPPED,
            detail="GITHUB_TOKEN or GITHUB_REPOSITORY not set",
        )
    try:
        import httpx
    except ImportError:
        return BackendReport(
            backend="code-scanning",
            status=ProbeStatus.ERROR,
            error="httpx not installed",
        )
    api_base = (env.get("GITHUB_API_URL") or "https://api.github.com").rstrip("/")
    try:
        resp = httpx.get(
            f"{api_base}/repos/{repo}/code-scanning/alerts",
            params={"state": "open", "per_page": "100"},
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=5.0,
        )
        resp.raise_for_status()
        alerts = resp.json()
    except Exception as exc:
        return BackendReport(backend="code-scanning", status=ProbeStatus.ERROR, error=str(exc))

    if not isinstance(alerts, list):
        return BackendReport(
            backend="code-scanning",
            status=ProbeStatus.ERROR,
            error="unexpected response shape",
        )

    by_severity = {"critical": 0, "high": 0, "medium": 0, "low": 0, "warning": 0, "note": 0}
    for a in alerts:
        rule = a.get("rule") or {}
        sev = (rule.get("security_severity_level") or rule.get("severity") or "warning").lower()
        by_severity[sev] = by_severity.get(sev, 0) + 1
    total = sum(by_severity.values())
    overall = ProbeStatus.OK
    if by_severity.get("critical", 0):
        overall = ProbeStatus.FAIL
    elif by_severity.get("high", 0):
        overall = ProbeStatus.WARN
    rows = [
        MetricRow(
            name="open_alerts",
            value=str(total),
            numeric=float(total),
            threshold="0",
            threshold_status=_classify(float(total), warn_above=1, fail_above=10),
        ),
    ]
    for sev in ("critical", "high", "medium", "low"):
        count = by_severity.get(sev, 0)
        rows.append(
            MetricRow(
                name=f"{sev}_alerts",
                value=str(count),
                numeric=float(count),
                threshold="0" if sev in ("critical", "high") else "",
                threshold_status=_classify(
                    float(count),
                    warn_above=1 if sev in ("critical", "high") else None,
                    fail_above=_security_fail_threshold(sev),
                ),
            )
        )
    return BackendReport(
        backend="code-scanning",
        status=overall,
        detail=f"{total} open alert(s)",
        metrics=rows,
    )


__all__ = [
    "BackendReport",
    "MetricRow",
    "ProbeStatus",
    "apply_deltas",
    "load_previous",
    "probe_code_scanning",
    "save_snapshot",
]
