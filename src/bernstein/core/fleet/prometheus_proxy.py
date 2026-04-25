"""Prometheus aggregation for the fleet view.

Each project's task server already exposes a ``/metrics`` endpoint via
:mod:`bernstein.core.observability.prometheus`. The fleet view scrapes
those endpoints concurrently and rewrites every metric line to add a
``project="<name>"`` label so a single Grafana dashboard can chart the
fleet without per-project boards.

The merge is text-only — we never deserialise into prometheus_client
because injecting labels through that API requires the original metric
definitions, which the fleet aggregator does not have.
"""

from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import httpx

if TYPE_CHECKING:
    from bernstein.core.fleet.config import ProjectConfig

logger = logging.getLogger(__name__)

_HELP_RE = re.compile(r"^# HELP ([^ ]+) (.*)$")
_TYPE_RE = re.compile(r"^# TYPE ([^ ]+) (.*)$")
_METRIC_RE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)(\{[^}]*\})?\s+(.+)$")


@dataclass(slots=True)
class MergeResult:
    """Outcome of merging Prometheus exports for one fleet snapshot.

    Attributes:
        body: The aggregated text/plain Prometheus exposition.
        ok_projects: Names of projects whose scrape succeeded.
        failed_projects: Mapping of name -> error message for failures.
    """

    body: str = ""
    ok_projects: list[str] = field(default_factory=list[str])
    failed_projects: dict[str, str] = field(default_factory=dict)


def _inject_label(metric_line: str, project: str) -> str:
    """Insert ``project="<name>"`` into a single metric line."""
    match = _METRIC_RE.match(metric_line)
    if match is None:
        return metric_line
    name, labels, value = match.group(1), match.group(2) or "", match.group(3)
    if labels:
        # ``{a="1"}`` -> ``{project="x",a="1"}``
        inner = labels[1:-1].strip()
        new_labels = (
            "{" + f'project="{project}",' + inner + "}"
            if inner
            else "{" + f'project="{project}"' + "}"
        )
    else:
        new_labels = "{" + f'project="{project}"' + "}"
    return f"{name}{new_labels} {value}"


def merge_text(project: str, body: str) -> str:
    """Re-label one project's exposition. Public so tests can call it."""
    out: list[str] = []
    for raw in body.splitlines():
        line = raw.rstrip()
        if not line:
            out.append("")
            continue
        if line.startswith("#"):
            # Help/type lines are kept verbatim (Prometheus tolerates duplicates
            # if the type matches; the rewrite preserves the same metric name).
            out.append(line)
            continue
        out.append(_inject_label(line, project))
    return "\n".join(out)


async def _scrape_one(
    client: httpx.AsyncClient, project: ProjectConfig, timeout_s: float
) -> tuple[ProjectConfig, str | None, str | None]:
    try:
        response = await client.get(project.metrics_url, timeout=timeout_s)
        response.raise_for_status()
        return project, response.text, None
    except (httpx.HTTPError, ValueError) as exc:
        return project, None, f"{type(exc).__name__}: {exc}"


async def merge_prometheus_metrics(
    projects: list[ProjectConfig],
    *,
    client: httpx.AsyncClient | None = None,
    timeout_s: float = 5.0,
) -> MergeResult:
    """Scrape each project's ``/metrics`` endpoint and merge into one body.

    Args:
        projects: Project configs to scrape.
        client: Optional pre-built ``httpx.AsyncClient``. When omitted a
            new one is created and closed by this call.
        timeout_s: Per-scrape timeout. Total wall-clock time is bounded by
            ``timeout_s`` because scrapes run concurrently.

    Returns:
        :class:`MergeResult` with the aggregated body plus per-project
        success/failure info.
    """
    owned = client is None
    if client is None:
        client = httpx.AsyncClient(timeout=timeout_s)

    try:
        tasks = [
            _scrape_one(client, project, timeout_s)
            for project in projects
        ]
        results = await asyncio.gather(*tasks, return_exceptions=False)
    finally:
        if owned:
            await client.aclose()

    body_parts: list[str] = [
        "# Bernstein fleet aggregated metrics",
        f"# fleet_project_count {len(projects)}",
    ]
    merge = MergeResult()
    for project, text, error in results:
        if error or text is None:
            merge.failed_projects[project.name] = error or "unknown error"
            body_parts.append(f"# fleet_project_offline {{project=\"{project.name}\"}} 1")
            continue
        merge.ok_projects.append(project.name)
        body_parts.append(f"# === project: {project.name} ===")
        body_parts.append(merge_text(project.name, text))
    merge.body = "\n".join(body_parts) + "\n"
    return merge
