#!/usr/bin/env python3
"""Render a single consolidated GitHub issue from live SonarQube findings.

This is the consolidated counterpart to ``scripts/sweep_sonar_findings.py``.
Where the sweeper emits one backlog ticket per finding, this script renders
ONE GitHub issue thread that mirrors the current open-finding set and is
re-rendered idempotently on every run. An agent (or an operator) works the
thread top-down; the next scan drops items that have been fixed.

The issue body carries a hidden marker ``<!-- sonar-tracker:bernstein -->``
so re-runs find and edit the existing issue instead of opening duplicates.

Public-artefact discipline
--------------------------
A GitHub issue in this repository is a public artefact. Like the sweeper,
this renderer NEVER copies the raw Sonar ``message``/``htmlDesc`` text into
the issue. Every human-readable description is synthesised from the shared
pre-vetted rule-family blurb table in ``scripts/sweep_sonar_findings.py``
(falling back to a neutral default keyed only on the rule id). The fully
rendered body is then scanned against a forbidden-substring guard before it
is written, so a new rule family that lands without a blurb still cannot
leak disallowed phrasing.

Env vars (matching the CI contract used by the scan + sweeper):
  - ``SONAR_HOST_URL``  e.g. ``https://sonar.bernstein.run``
  - ``SONAR_TOKEN``     user token with Browse permission on the project
  - ``SONAR_PROJECT_KEY`` optional, defaults to ``bernstein``
  - ``GITHUB_TOKEN``    token used for ``gh issue`` operations
  - ``GITHUB_REPOSITORY`` optional ``owner/name``; falls back to the
    ``gh`` default repo resolved from the checkout.

Usage
-----

    python scripts/render_sonar_tracker.py [--dry-run] \\
        [--fixture path/to/issues.json] [--output-body path.md] \\
        [--repo owner/name]

Exit codes
----------

- 0: clean run (rendered + synced, or no-op when SONAR_TOKEN is empty).
- 1: Sonar API failed after retries, or the GitHub sync failed.
- 2: misconfiguration (missing env vars, bad CLI args).
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as _dt
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    RunnerFn = Callable[..., Any]
else:
    RunnerFn = Any

# Make the bernstein package importable when running from the source tree,
# and let us reuse the sweeper's vetted blurb table + Sonar helpers.
_REPO_ROOT = Path(__file__).resolve().parent.parent
_SRC = _REPO_ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
if str(_REPO_ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT / "scripts"))

from sweep_sonar_findings import (  # noqa: E402
    FORBIDDEN_SUBSTRINGS as _SWEEPER_FORBIDDEN,
)
from sweep_sonar_findings import (  # noqa: E402
    SEVERITY_ORDER,
    SEVERITY_RANK,
    Finding,
    SonarAPIError,
    _auth,
    _component_path,
    _normalise_issue,
    _request_with_retries,
    safe_why,
)

from bernstein.core.observability.sonar import (  # noqa: E402
    SonarConfig,
    load_config,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Hidden marker that uniquely identifies the tracker issue. Searched for in
# the body of open issues so re-runs edit instead of duplicating.
# Consolidated SonarQube findings tracker. See docs/operations/sonar-tracker.md.
TRACKER_MARKER = "<!-- sonar-tracker:bernstein -->"

TRACKER_TITLE = "SonarQube findings tracker"
TRACKER_LABELS = ("sonar-tracker", "automated")
PRIMARY_LABEL = "sonar-tracker"

# GitHub rejects issue bodies above 65536 characters. Keep a small safety
# margin so trailing newlines / CRLF normalisation never tip us over.
GITHUB_BODY_LIMIT = 65536
_BODY_SAFETY_MARGIN = 256

DEFAULT_PAGE_SIZE = 500
MAX_PAGES = 40  # 40 * 500 = 20k findings ceiling; well above current volume.

DEFAULT_TIMEOUT_SECONDS = 20.0

# Severities listed in full (every item as a checkbox) before any collapse.
_FULL_LIST_SEVERITIES = ("BLOCKER", "CRITICAL")

# Per-severity cap for the collapsed <details> sections. Above this we list
# the first N and append an "and N more" pointer to Sonar.
_DETAILS_ITEM_CAP = 80

# Cap on the number of issue keys serialised into each list in the trailing
# JSON summary. A fixer loop consumes the highest-leverage keys first, so an
# unbounded list only risks pushing the body past the GitHub size cap on a
# pathological project. When a list is truncated a ``*_keys_truncated`` count
# is emitted alongside it so a consumer can tell the list is partial.
_JSON_KEYS_CAP = 200
_HOTSPOT_ITEM_CAP = 80

_LIFECYCLE_NOTE = (
    "This thread is auto-rendered from Sonar on each scan. Resolve an item "
    "by fixing the code (the next scan drops it) or by marking it Won't Fix "
    "or resolved in Sonar. Do not hand-edit; edits are overwritten on the "
    "next sync."
)

# Public-artefact guard for the rendered body. The sweeper's
# disallowed-phrase word list is the single source of truth; it forbids the
# two long-dash code points (0x2014 / 0x2013) while leaving the plain hyphen
# legitimate in markdown tables, list bullets, and rule ids like
# ``python:S3776``. The set mirrors the project text-hygiene phrase list for
# the terms a Sonar rule label could plausibly contain.
_TRACKER_FORBIDDEN: tuple[str, ...] = _SWEEPER_FORBIDDEN


# ---------------------------------------------------------------------------
# Sonar fetch: issues, quality gate, coverage
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class QualityGateCondition:
    """One condition row from SonarQube's project quality-gate status."""

    metric_key: str
    status: str
    comparator: str | None
    error_threshold: str | None
    actual_value: str | None

    def as_json(self) -> dict[str, str | None]:
        """Return a stable JSON shape for the tracker summary."""
        return {
            "actual_value": self.actual_value,
            "comparator": self.comparator,
            "error_threshold": self.error_threshold,
            "metric_key": self.metric_key,
            "status": self.status,
        }


@dataclasses.dataclass(frozen=True)
class QualityGateResult:
    """Quality-gate status plus its condition rows."""

    status: str
    conditions: list[QualityGateCondition] = dataclasses.field(default_factory=list)


@dataclasses.dataclass(frozen=True)
class SecurityHotspot:
    """One SonarQube security hotspot row safe to render publicly."""

    key: str
    rule_key: str
    component: str
    line: int | None
    status: str
    security_category: str | None
    vulnerability_probability: str | None

    def as_json(self) -> dict[str, str | int | None]:
        """Return a stable JSON shape for the tracker summary."""
        return {
            "component": self.component,
            "key": self.key,
            "line": self.line,
            "rule_key": self.rule_key,
            "security_category": self.security_category,
            "status": self.status,
            "vulnerability_probability": self.vulnerability_probability,
        }


def fetch_all_findings(
    config: SonarConfig,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
    client: httpx.Client | None = None,
) -> list[Finding]:
    """Page through ``/api/issues/search`` for every open finding.

    Unlike the sweeper we do not pre-filter by severity: the tracker shows
    the whole open set grouped by severity, so we request all severities.
    """
    url = f"{config.host}/api/issues/search"
    findings: list[Finding] = []
    owns_client = client is None
    if owns_client:
        client = httpx.Client(timeout=DEFAULT_TIMEOUT_SECONDS, auth=_auth(config.token))
    assert client is not None
    try:
        page = 1
        while page <= MAX_PAGES:
            params: dict[str, Any] = {
                "componentKeys": config.project_key,
                "resolved": "false",
                "s": "SEVERITY",
                "asc": "false",
                "ps": str(page_size),
                "p": str(page),
            }
            resp = _request_with_retries(client, url, params)
            try:
                payload = resp.json()
            except ValueError as exc:
                raise SonarAPIError("issues search returned invalid JSON") from exc
            if not isinstance(payload, dict):
                raise SonarAPIError("issues search returned a non-object payload")
            issues = payload.get("issues") or []
            for raw in issues:
                if isinstance(raw, dict):
                    finding = _normalise_issue(raw)
                    if finding is not None:
                        findings.append(finding)
            paging = payload.get("paging") or {}
            try:
                total = int(paging.get("total", 0))
                page_idx = int(paging.get("pageIndex", page))
                size = int(paging.get("pageSize", page_size))
            except (TypeError, ValueError) as exc:
                raise SonarAPIError("issues search returned invalid paging metadata") from exc
            if size <= 0 or page_idx * size >= total:
                break
            page += 1
    finally:
        if owns_client:
            client.close()
    return findings


def fetch_security_hotspots(
    config: SonarConfig,
    *,
    page_size: int = DEFAULT_PAGE_SIZE,
    client: httpx.Client | None = None,
) -> list[SecurityHotspot]:
    """Page through SonarQube security hotspots awaiting review."""
    url = f"{config.host}/api/hotspots/search"
    hotspots: list[SecurityHotspot] = []
    owns_client = client is None
    if owns_client:
        client = httpx.Client(timeout=DEFAULT_TIMEOUT_SECONDS, auth=_auth(config.token))
    assert client is not None
    try:
        page = 1
        while page <= MAX_PAGES:
            params: dict[str, Any] = {
                "projectKey": config.project_key,
                "status": "TO_REVIEW",
                "ps": str(page_size),
                "p": str(page),
            }
            resp = _request_with_retries(client, url, params)
            try:
                payload = resp.json()
            except ValueError as exc:
                raise SonarAPIError("hotspot search returned invalid JSON") from exc
            if not isinstance(payload, dict):
                raise SonarAPIError("hotspot search returned a non-object payload")
            raw_hotspots = payload.get("hotspots") or []
            for raw in raw_hotspots:
                if isinstance(raw, dict):
                    hotspot = _normalise_hotspot(raw)
                    if hotspot is not None:
                        hotspots.append(hotspot)
            paging = payload.get("paging") or {}
            try:
                total = int(paging.get("total", 0))
                page_idx = int(paging.get("pageIndex", page))
                size = int(paging.get("pageSize", page_size))
            except (TypeError, ValueError) as exc:
                raise SonarAPIError("hotspot search returned invalid paging metadata") from exc
            if size <= 0 or page_idx * size >= total:
                break
            page += 1
    finally:
        if owns_client:
            client.close()
    hotspots.sort(key=lambda item: (_component_path(item.component), item.line or 0, item.key))
    return hotspots


def fetch_quality_gate(
    config: SonarConfig,
    *,
    client: httpx.Client | None = None,
) -> str:
    """Return the quality-gate status string (e.g. ``OK``/``ERROR``).

    Returns ``"UNKNOWN"`` when the server omits the field or the call
    fails; the tracker still renders with the issue counts.
    """
    return fetch_quality_gate_details(config, client=client).status


def fetch_quality_gate_details(
    config: SonarConfig,
    *,
    client: httpx.Client | None = None,
) -> QualityGateResult:
    """Return the project quality-gate status and condition rows."""
    url = f"{config.host}/api/qualitygates/project_status"
    params = {"projectKey": config.project_key}
    owns_client = client is None
    if owns_client:
        client = httpx.Client(timeout=DEFAULT_TIMEOUT_SECONDS, auth=_auth(config.token))
    assert client is not None
    try:
        try:
            resp = _request_with_retries(client, url, params)
        except SonarAPIError:
            return QualityGateResult("UNKNOWN")
        try:
            payload = resp.json()
        except ValueError:
            return QualityGateResult("UNKNOWN")
    finally:
        if owns_client:
            client.close()
    if not isinstance(payload, dict):
        return QualityGateResult("UNKNOWN")
    status = payload.get("projectStatus")
    if isinstance(status, dict):
        value = status.get("status")
        if isinstance(value, str) and value:
            return QualityGateResult(value, _parse_quality_gate_conditions(status.get("conditions")))
    return QualityGateResult("UNKNOWN")


def _parse_quality_gate_conditions(raw_conditions: Any) -> list[QualityGateCondition]:
    if not isinstance(raw_conditions, list):
        return []
    conditions: list[QualityGateCondition] = []
    for raw in raw_conditions:
        if not isinstance(raw, dict):
            continue
        metric_key = _opt_str(raw.get("metricKey"))
        status = _opt_str(raw.get("status"))
        if metric_key is None or status is None:
            continue
        conditions.append(
            QualityGateCondition(
                metric_key=metric_key,
                status=status,
                comparator=_opt_str(raw.get("comparator")),
                error_threshold=_opt_str(raw.get("errorThreshold")),
                actual_value=_opt_str(raw.get("actualValue")),
            )
        )
    return conditions


def _normalise_hotspot(raw: dict[str, Any]) -> SecurityHotspot | None:
    key = _opt_str(raw.get("key"))
    rule_key = _opt_str(raw.get("ruleKey") or raw.get("rule"))
    component = _opt_str(raw.get("component"))
    status = _opt_str(raw.get("status"))
    if key is None or rule_key is None or component is None or status is None:
        return None
    return SecurityHotspot(
        key=key,
        rule_key=rule_key,
        component=component,
        line=_opt_int(raw.get("line")),
        status=status,
        security_category=_opt_str(raw.get("securityCategory")),
        vulnerability_probability=_opt_str(raw.get("vulnerabilityProbability")),
    )


def fetch_coverage(
    config: SonarConfig,
    *,
    client: httpx.Client | None = None,
) -> float | None:
    """Return the project coverage percentage, or ``None`` when unavailable."""
    url = f"{config.host}/api/measures/component"
    params = {"component": config.project_key, "metricKeys": "coverage"}
    owns_client = client is None
    if owns_client:
        client = httpx.Client(timeout=DEFAULT_TIMEOUT_SECONDS, auth=_auth(config.token))
    assert client is not None
    try:
        try:
            resp = _request_with_retries(client, url, params)
        except SonarAPIError:
            return None
        try:
            payload = resp.json()
        except ValueError:
            return None
    finally:
        if owns_client:
            client.close()
    if not isinstance(payload, dict):
        return None
    component = payload.get("component")
    if not isinstance(component, dict):
        return None
    measures = component.get("measures")
    if not isinstance(measures, list):
        return None
    for item in measures:
        if isinstance(item, dict) and item.get("metric") == "coverage":
            return _opt_float(item.get("value"))
    return None


@dataclasses.dataclass(frozen=True)
class SonarSnapshot:
    """Everything the renderer needs from one Sonar poll."""

    findings: list[Finding]
    quality_gate: str
    coverage: float | None
    host: str
    project_key: str
    quality_gate_conditions: list[QualityGateCondition] = dataclasses.field(default_factory=list)
    security_hotspots: list[SecurityHotspot] = dataclasses.field(default_factory=list)


def collect_snapshot(
    config: SonarConfig,
    *,
    client: httpx.Client | None = None,
) -> SonarSnapshot:
    """Fetch findings + quality gate + coverage in one pass."""
    owns_client = client is None
    if owns_client:
        client = httpx.Client(timeout=DEFAULT_TIMEOUT_SECONDS, auth=_auth(config.token))
    assert client is not None
    try:
        findings = fetch_all_findings(config, client=client)
        quality_gate = fetch_quality_gate_details(config, client=client)
        coverage = fetch_coverage(config, client=client)
        security_hotspots = fetch_security_hotspots(config, client=client)
    finally:
        if owns_client:
            client.close()
    return SonarSnapshot(
        findings=findings,
        quality_gate=quality_gate.status,
        coverage=coverage,
        host=config.host,
        project_key=config.project_key,
        quality_gate_conditions=quality_gate.conditions,
        security_hotspots=security_hotspots,
    )


# ---------------------------------------------------------------------------
# Grouping
# ---------------------------------------------------------------------------


def group_by_severity(findings: Sequence[Finding]) -> dict[str, list[Finding]]:
    """Bucket findings into the canonical severity order.

    Within a severity, findings are sorted by component then line so the
    rendered list is stable across runs (idempotent body for a fixed input).
    """
    buckets: dict[str, list[Finding]] = {sev: [] for sev in SEVERITY_ORDER}
    extra: dict[str, list[Finding]] = {}
    for finding in findings:
        target = buckets.get(finding.severity)
        if target is None:
            target = extra.setdefault(finding.severity, [])
        target.append(finding)
    # Fold any unexpected severity label into the table under its own key so
    # counts stay honest, then sort each bucket deterministically.
    buckets.update(extra)
    for sev in buckets:
        buckets[sev].sort(key=lambda f: (_component_path(f.component), f.line or 0, f.key))
    return buckets


def _severity_sort_key(severity: str) -> int:
    return SEVERITY_RANK.get(severity, len(SEVERITY_ORDER))


# ---------------------------------------------------------------------------
# Markdown rendering
# ---------------------------------------------------------------------------


def _md_inline(value: str) -> str:
    """Escape a value interpolated into a markdown table cell or code span.

    Sonar-supplied strings (rule keys, component paths, metric keys) flow
    into the public tracker issue. A stray ``|`` breaks the surrounding
    table column and a backtick terminates the code span, so escape the
    pipe and neutralise interior backticks. The tracker is world-readable,
    so this keeps a hostile rule key or path from corrupting the layout.
    """
    return value.replace("`", "'").replace("|", "\\|")


def _issue_permalink(host: str, project_key: str, issue_key: str) -> str:
    """Build the deep link to a single issue in the Sonar UI."""
    return f"{host}/project/issues?issueStatuses=OPEN,CONFIRMED&id={project_key}&open={issue_key}"


def _finding_line(finding: Finding, host: str, project_key: str, *, checkbox: bool) -> str:
    """Render one finding as a markdown list item (public-safe)."""
    desc = safe_why(finding.rule, finding.severity, finding.component, finding.line)
    location = _component_path(finding.component)
    if finding.line:
        location = f"{location}:{finding.line}"
    link = _issue_permalink(host, project_key, finding.key)
    box = "- [ ] " if checkbox else "- "
    return f"{box}rule `{_md_inline(finding.rule)}`: {desc} `{_md_inline(location)}` ([view]({link}))"


def _coverage_text(coverage: float | None) -> str:
    if coverage is None:
        return "n/a"
    return f"{coverage:.1f}%"


def _condition_text(value: str | None) -> str:
    if value is None or value == "":
        return "n/a"
    return value


def _tldr_table(
    buckets: dict[str, list[Finding]],
    *,
    quality_gate: str,
    coverage: float | None,
    security_hotspot_count: int,
) -> list[str]:
    """Build the header summary table lines."""
    total = sum(len(v) for v in buckets.values())
    lines = [
        f"# {TRACKER_TITLE}",
        "",
        TRACKER_MARKER,
        "",
        "## TL;DR",
        "",
        f"- Quality gate: **{quality_gate}**",
        f"- Coverage: **{_coverage_text(coverage)}**",
        f"- Open findings: **{total}**",
        f"- Security hotspots to review: **{security_hotspot_count}**",
        "",
        "| Severity | Open |",
        "| --- | ---: |",
    ]
    for sev in sorted(buckets, key=_severity_sort_key):
        count = len(buckets[sev])
        if count:
            lines.append(f"| {sev} | {count} |")
    lines.append(f"| **Total** | **{total}** |")
    lines.append("")
    lines.append(f"_{_LIFECYCLE_NOTE}_")
    lines.append("")
    return lines


def _full_section(
    severity: str,
    items: list[Finding],
    host: str,
    project_key: str,
) -> list[str]:
    """Render a BLOCKER/CRITICAL section: every item as a checkbox."""
    if not items:
        return []
    lines = [f"## {severity} ({len(items)})", ""]
    lines.extend(_finding_line(f, host, project_key, checkbox=True) for f in items)
    lines.append("")
    return lines


def _details_section(
    severity: str,
    items: list[Finding],
    host: str,
    project_key: str,
    *,
    item_cap: int,
) -> list[str]:
    """Render a MAJOR/MINOR/INFO section collapsed inside <details>."""
    if not items:
        return []
    shown = items[:item_cap]
    remainder = len(items) - len(shown)
    lines = [
        "<details>",
        f"<summary>{severity} ({len(items)})</summary>",
        "",
    ]
    lines.extend(_finding_line(f, host, project_key, checkbox=False) for f in shown)
    if remainder > 0:
        lines.append(f"- and {remainder} more, see Sonar")
    lines.extend(["", "</details>", ""])
    return lines


def _counts_only_section(severity: str, items: list[Finding], host: str, project_key: str) -> list[str]:
    """Most compact section form: a one-line count + a link to Sonar."""
    if not items:
        return []
    link = f"{host}/project/issues?issueStatuses=OPEN,CONFIRMED&id={project_key}&severities={severity}"
    return [f"- **{severity}**: {len(items)} open ([view]({link}))", ""]


def _quality_gate_conditions_section(conditions: Sequence[QualityGateCondition]) -> list[str]:
    """Render Sonar quality-gate condition rows."""
    if not conditions:
        return []
    lines = [
        "## Quality Gate Conditions",
        "",
        "| Metric | Status | Actual | Comparator | Threshold |",
        "| --- | --- | ---: | --- | ---: |",
    ]
    for condition in conditions:
        lines.append(
            "| "
            f"`{_md_inline(condition.metric_key)}` | "
            f"{condition.status} | "
            f"{_condition_text(condition.actual_value)} | "
            f"{_condition_text(condition.comparator)} | "
            f"{_condition_text(condition.error_threshold)} |"
        )
    lines.append("")
    return lines


def _hotspot_permalink(host: str, project_key: str, hotspot_key: str) -> str:
    """Build the deep link to a single security hotspot in the Sonar UI."""
    return f"{host}/security_hotspots?id={project_key}&hotspots={hotspot_key}"


def _security_hotspots_section(hotspots: Sequence[SecurityHotspot], host: str, project_key: str) -> list[str]:
    """Render security hotspots that still need Sonar-side review."""
    if not hotspots:
        return []
    shown = hotspots[:_HOTSPOT_ITEM_CAP]
    remainder = len(hotspots) - len(shown)
    lines = [
        "## Security Hotspots To Review",
        "",
        "| Rule | Status | Category | Probability | Location |",
        "| --- | --- | --- | --- | --- |",
    ]
    for hotspot in shown:
        location = _component_path(hotspot.component)
        if hotspot.line is not None:
            location = f"{location}:{hotspot.line}"
        link = _hotspot_permalink(host, project_key, hotspot.key)
        lines.append(
            "| "
            f"`{_md_inline(hotspot.rule_key)}` | "
            f"{hotspot.status} | "
            f"{_condition_text(hotspot.security_category)} | "
            f"{_condition_text(hotspot.vulnerability_probability)} | "
            f"`{_md_inline(location)}` ([view]({link})) |"
        )
    if remainder > 0:
        lines.append(f"| n/a | n/a | n/a | n/a | {remainder} more, see Sonar |")
    lines.append("")
    return lines


def _json_summary_block(snapshot: SonarSnapshot, buckets: dict[str, list[Finding]], generated_at: str) -> list[str]:
    """Build the trailing machine-readable JSON summary.

    The per-severity key lists are capped at ``_JSON_KEYS_CAP`` so the JSON
    blob stays bounded even on a project with thousands of high-severity
    findings. When a list is truncated, a sibling ``*_keys_truncated`` count
    records how many keys were dropped so a consumer can tell it is partial.
    """
    by_severity = {sev: len(buckets[sev]) for sev in sorted(buckets, key=_severity_sort_key) if buckets.get(sev)}
    summary: dict[str, Any] = {
        "generated_at": generated_at,
        "quality_gate": snapshot.quality_gate,
        "quality_gate_conditions": [condition.as_json() for condition in snapshot.quality_gate_conditions],
        "security_hotspots": [hotspot.as_json() for hotspot in snapshot.security_hotspots[:_JSON_KEYS_CAP]],
        "coverage": snapshot.coverage,
        "by_severity": by_severity,
    }
    hotspot_truncated = len(snapshot.security_hotspots) - len(snapshot.security_hotspots[:_JSON_KEYS_CAP])
    if hotspot_truncated > 0:
        summary["security_hotspots_truncated"] = hotspot_truncated
    for name, severity in (("blocker", "BLOCKER"), ("critical", "CRITICAL")):
        all_keys = [f.key for f in buckets.get(severity, [])]
        summary[f"{name}_keys"] = all_keys[:_JSON_KEYS_CAP]
        truncated = len(all_keys) - len(all_keys[:_JSON_KEYS_CAP])
        if truncated > 0:
            summary[f"{name}_keys_truncated"] = truncated
    blob = json.dumps(summary, indent=2, sort_keys=True)
    return ["## Machine-readable summary", "", "```json", blob, "```", ""]


def _contains_forbidden(body: str, forbidden: str) -> bool:
    """Return true when ``forbidden`` appears as a standalone token or phrase."""
    lower = body.lower()
    needle = forbidden.lower()
    if not needle.isalnum():
        return needle in lower
    pattern = rf"(?<![A-Za-z0-9_]){re.escape(needle)}(?![A-Za-z0-9_])"
    return re.search(pattern, lower) is not None


def _assert_no_forbidden(body: str) -> None:
    """Raise if the rendered body carries any disallowed token or phrase."""
    for forbidden in _TRACKER_FORBIDDEN:
        if _contains_forbidden(body, forbidden):
            raise AssertionError(f"rendered tracker body contains forbidden token or phrase {forbidden!r}")


def render_body(snapshot: SonarSnapshot, *, generated_at: str | None = None) -> str:
    """Render the full issue body, collapsing to fit the GitHub size cap.

    Strategy, applied in order until the body fits under the limit:

    1. BLOCKER + CRITICAL listed in full (checkboxes); MAJOR/MINOR/INFO in
       <details> capped at ``_DETAILS_ITEM_CAP`` items each. The trailing
       JSON key lists are capped at ``_JSON_KEYS_CAP`` regardless of size.
    2. Shrink the per-section item cap on the <details> sections (preferred
       over dropping a whole section).
    3. Collapse the lowest-severity <details> sections to a counts-only
       line, one at a time from INFO upward.
    4. As a final guard, collapse CRITICAL (then BLOCKER) to counts-only.

    The body is never truncated mid-line: every collapse step drops whole
    list items or whole sections. If the body still exceeds the limit after
    every step, a ``ValueError`` is raised so the failure is explicit rather
    than surfacing later as a rejected ``gh`` call.
    """
    generated_at = generated_at or _dt.datetime.now(tz=_dt.UTC).isoformat(timespec="seconds")
    buckets = group_by_severity(snapshot.findings)
    host = snapshot.host
    project_key = snapshot.project_key

    # Severities that get a collapsible section, lowest-priority last.
    details_order = [s for s in SEVERITY_ORDER if s not in _FULL_LIST_SEVERITIES]
    # Any unexpected severity labels render as counts-only at the very end.
    extra_severities = [s for s in buckets if s not in SEVERITY_ORDER]

    # Render mode per severity: "full", "details", or "counts".
    mode: dict[str, str] = {}
    for sev in _FULL_LIST_SEVERITIES:
        mode[sev] = "full"
    for sev in details_order:
        mode[sev] = "details"
    for sev in extra_severities:
        mode[sev] = "counts"

    item_cap = _DETAILS_ITEM_CAP

    def _build() -> str:
        parts: list[str] = []
        parts.extend(
            _tldr_table(
                buckets,
                quality_gate=snapshot.quality_gate,
                coverage=snapshot.coverage,
                security_hotspot_count=len(snapshot.security_hotspots),
            )
        )
        parts.extend(_quality_gate_conditions_section(snapshot.quality_gate_conditions))
        parts.extend(_security_hotspots_section(snapshot.security_hotspots, host, project_key))
        for sev in SEVERITY_ORDER + tuple(extra_severities):
            items = buckets.get(sev, [])
            if not items:
                continue
            current = mode.get(sev, "details")
            if current == "full":
                parts.extend(_full_section(sev, items, host, project_key))
            elif current == "details":
                parts.extend(
                    _details_section(
                        sev,
                        items,
                        host,
                        project_key,
                        item_cap=item_cap,
                    )
                )
            else:
                parts.extend(_counts_only_section(sev, items, host, project_key))
        parts.extend(_json_summary_block(snapshot, buckets, generated_at))
        return "\n".join(parts).rstrip() + "\n"

    body = _build()
    budget = GITHUB_BODY_LIMIT - _BODY_SAFETY_MARGIN

    # Step 2: shrink the per-section item cap while <details> sections still
    # exist. Reducing items inside a section is preferred over dropping the
    # whole section, so this runs before the counts-only collapse below.
    while len(body) > budget and item_cap > 1:
        item_cap = max(1, item_cap // 2)
        body = _build()

    # Step 3: collapse <details> sections to counts-only, lowest severity first.
    collapse_queue = list(reversed(details_order))
    qi = 0
    while len(body) > budget and qi < len(collapse_queue):
        mode[collapse_queue[qi]] = "counts"
        qi += 1
        body = _build()

    # Step 4: collapse CRITICAL then BLOCKER to counts-only as a last resort.
    for sev in ("CRITICAL", "BLOCKER"):
        if len(body) <= budget:
            break
        mode[sev] = "counts"
        body = _build()

    # Final guard: every section is now counts-only and the JSON key lists are
    # capped, so the body should fit. If it still does not, fail loudly here
    # rather than letting `gh issue create/edit` reject an oversized body.
    if len(body) > budget:
        raise ValueError("rendered tracker body still exceeds GitHub's issue body limit")

    _assert_no_forbidden(body)
    return body


# ---------------------------------------------------------------------------
# GitHub issue sync (via gh CLI)
# ---------------------------------------------------------------------------


class GitHubSyncError(RuntimeError):
    """Raised when a ``gh`` operation fails."""


def _run_gh(
    args: Sequence[str],
    *,
    runner: RunnerFn = subprocess.run,
    input_text: str | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a ``gh`` command, returning the completed process."""
    return runner(
        ["gh", *args],
        capture_output=True,
        text=True,
        check=False,
        input=input_text,
    )


def _repo_args(repo: str | None) -> list[str]:
    return ["--repo", repo] if repo else []


def find_tracker_issue(repo: str | None, *, runner: RunnerFn = subprocess.run) -> int | None:
    """Return the number of the existing tracker issue, or ``None``.

    Searches open issues carrying the ``sonar-tracker`` label and confirms
    the hidden marker is present in the body before claiming a match.
    """
    args = [
        "issue",
        "list",
        *_repo_args(repo),
        "--label",
        PRIMARY_LABEL,
        "--state",
        "open",
        "--json",
        "number,body",
        "--limit",
        "50",
    ]
    result = _run_gh(args, runner=runner)
    if result.returncode != 0:
        raise GitHubSyncError(f"gh issue list failed: {result.stderr.strip()[:200]}")
    try:
        payload = json.loads(result.stdout or "[]")
    except json.JSONDecodeError as exc:
        raise GitHubSyncError(f"gh issue list returned invalid JSON: {exc}") from exc
    if not isinstance(payload, list):
        return None
    for entry in payload:
        if not isinstance(entry, dict):
            continue
        body = entry.get("body")
        number = entry.get("number")
        if isinstance(body, str) and TRACKER_MARKER in body and isinstance(number, int):
            return number
    return None


def _ensure_labels_exist(repo: str | None, *, runner: RunnerFn = subprocess.run) -> None:
    """Best-effort create the tracker labels; ignore 'already exists'."""
    colours = {"sonar-tracker": "1d76db", "automated": "ededed"}
    for label in TRACKER_LABELS:
        _run_gh(
            [
                "label",
                "create",
                label,
                *_repo_args(repo),
                "--color",
                colours.get(label, "ededed"),
                "--force",
            ],
            runner=runner,
        )


def create_tracker_issue(
    body: str,
    repo: str | None,
    *,
    runner: RunnerFn = subprocess.run,
) -> int:
    """Create the tracker issue and return its number."""
    _ensure_labels_exist(repo, runner=runner)
    args = [
        "issue",
        "create",
        *_repo_args(repo),
        "--title",
        TRACKER_TITLE,
        "--body-file",
        "-",
        "--label",
        ",".join(TRACKER_LABELS),
    ]
    result = _run_gh(args, runner=runner, input_text=body)
    if result.returncode != 0:
        raise GitHubSyncError(f"gh issue create failed: {result.stderr.strip()[:300]}")
    url = (result.stdout or "").strip().splitlines()
    if not url:
        raise GitHubSyncError("gh issue create returned no URL")
    return _issue_number_from_url(url[-1])


def update_tracker_issue(
    number: int,
    body: str,
    repo: str | None,
    *,
    runner: RunnerFn = subprocess.run,
) -> None:
    """Edit the existing tracker issue body and ensure the label is set."""
    _ensure_labels_exist(repo, runner=runner)
    args = [
        "issue",
        "edit",
        str(number),
        *_repo_args(repo),
        "--body-file",
        "-",
        "--add-label",
        PRIMARY_LABEL,
    ]
    result = _run_gh(args, runner=runner, input_text=body)
    if result.returncode != 0:
        raise GitHubSyncError(f"gh issue edit failed: {result.stderr.strip()[:300]}")


def _issue_number_from_url(url: str) -> int:
    """Extract the trailing issue number from a GitHub issue URL."""
    tail = url.rstrip("/").rsplit("/", 1)[-1]
    try:
        return int(tail)
    except ValueError as exc:
        raise GitHubSyncError(f"could not parse issue number from {url!r}") from exc


def sync_issue(
    body: str,
    repo: str | None,
    *,
    runner: RunnerFn = subprocess.run,
) -> tuple[int, str]:
    """Create or update the tracker issue. Returns ``(number, action)``."""
    existing = find_tracker_issue(repo, runner=runner)
    if existing is not None:
        update_tracker_issue(existing, body, repo, runner=runner)
        return existing, "updated"
    number = create_tracker_issue(body, repo, runner=runner)
    return number, "created"


# ---------------------------------------------------------------------------
# Entrypoint
# ---------------------------------------------------------------------------


def _load_fixture(path: Path) -> SonarSnapshot:
    """Build a snapshot from a saved JSON fixture (tests + offline dry-run).

    The fixture may be a bare ``/api/issues/search`` payload (an object with
    an ``issues`` list) or an extended object that also carries
    ``quality_gate``, ``coverage``, ``host`` and ``project_key`` keys.
    """
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        return SonarSnapshot([], "UNKNOWN", None, "https://sonar.bernstein.run", "bernstein")
    issues = payload.get("issues")
    findings: list[Finding] = []
    if isinstance(issues, list):
        for raw in issues:
            if isinstance(raw, dict):
                finding = _normalise_issue(raw)
                if finding is not None:
                    findings.append(finding)
    return SonarSnapshot(
        findings=findings,
        quality_gate=str(payload.get("quality_gate", "UNKNOWN")),
        coverage=_opt_float(payload.get("coverage")),
        host=str(payload.get("host", "https://sonar.bernstein.run")),
        project_key=str(payload.get("project_key", "bernstein")),
        quality_gate_conditions=_parse_quality_gate_conditions(payload.get("quality_gate_conditions")),
        security_hotspots=_parse_security_hotspots(payload.get("security_hotspots")),
    )


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    return str(value)


def _opt_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _opt_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _parse_security_hotspots(raw_hotspots: Any) -> list[SecurityHotspot]:
    if not isinstance(raw_hotspots, list):
        return []
    hotspots: list[SecurityHotspot] = []
    for raw in raw_hotspots:
        if isinstance(raw, dict):
            hotspot = _normalise_hotspot(raw)
            if hotspot is not None:
                hotspots.append(hotspot)
    hotspots.sort(key=lambda item: (_component_path(item.component), item.line or 0, item.key))
    return hotspots


def run(args: argparse.Namespace) -> int:
    """Fetch the snapshot, render the body, and sync the GitHub issue."""
    # 1) Build the snapshot.
    if args.fixture:
        try:
            snapshot = _load_fixture(Path(args.fixture))
        except (OSError, json.JSONDecodeError) as exc:
            print(f"error: failed to load fixture: {exc}", file=sys.stderr)
            return 2
    else:
        config = load_config()
        if config is None:
            print("error: SONAR_HOST_URL and SONAR_TOKEN must be set", file=sys.stderr)
            return 2
        try:
            snapshot = collect_snapshot(config)
        except SonarAPIError as exc:
            print(f"error: sonar fetch failed: {exc}", file=sys.stderr)
            return 1

    # 2) Render the body.
    body = render_body(snapshot)
    print(
        f"sonar-tracker: findings={len(snapshot.findings)} "
        f"quality_gate={snapshot.quality_gate} "
        f"coverage={_coverage_text(snapshot.coverage)} "
        f"body_chars={len(body)}/{GITHUB_BODY_LIMIT}",
    )

    if args.output_body:
        Path(args.output_body).write_text(body, encoding="utf-8")
        print(f"sonar-tracker: wrote rendered body to {args.output_body}")

    if args.dry_run:
        print("sonar-tracker: dry-run; not syncing GitHub issue")
        return 0

    # 3) Sync the GitHub issue.
    repo = args.repo or _default_repo()
    try:
        number, action = sync_issue(body, repo)
    except GitHubSyncError as exc:
        print(f"error: github sync failed: {exc}", file=sys.stderr)
        return 1
    print(f"sonar-tracker: {action} issue #{number}")
    return 0


def _default_repo() -> str | None:
    """Resolve ``owner/name`` from env; let gh fall back to the checkout."""
    import os

    repo = (os.environ.get("GITHUB_REPOSITORY") or "").strip()
    return repo or None


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Render the body but do not create or edit any GitHub issue.",
    )
    p.add_argument(
        "--fixture",
        default=None,
        help="Read the snapshot from a saved JSON fixture instead of Sonar.",
    )
    p.add_argument(
        "--output-body",
        default=None,
        help="Also write the rendered body to this path (debugging aid).",
    )
    p.add_argument(
        "--repo",
        default=None,
        help="Target GitHub repo as owner/name. Defaults to $GITHUB_REPOSITORY.",
    )
    return p.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
