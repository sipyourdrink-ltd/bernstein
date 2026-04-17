"""Scheduled dependency-scan helpers for the orchestrator.

Extracted from the retired ``orchestrator_tick`` module as part of
audit-002 (orchestrator_tick zombie). Only these three helpers were ever
imported from the outside; the rest of ``orchestrator_tick`` duplicated
``Orchestrator.tick`` without being called.

The helpers take an orchestrator-like instance so they can be unit
tested without constructing a full :class:`Orchestrator`. They use only
the public-ish attributes ``_client``, ``_config.server_url``,
``_dependency_scanner``, ``_audit_log`` and ``_post_bulletin``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from bernstein.core.dependency_scan import (
    DependencyScanStatus,
)

if TYPE_CHECKING:
    from bernstein.core.dependency_scan import (
        DependencyVulnerabilityFinding,
    )

logger = logging.getLogger(__name__)


def run_scheduled_dependency_scan(orch: Any) -> None:
    """Run the weekly dependency scan and enqueue remediation tasks.

    Args:
        orch: The orchestrator instance.
    """
    try:
        existing_titles = load_existing_dependency_scan_task_titles(orch)
        result = orch._dependency_scanner.run_if_due(
            create_fix_task=lambda finding: create_dependency_fix_task(orch, finding, existing_titles),
            audit_log=orch._audit_log,
        )
    except Exception as exc:
        logger.warning("Dependency scan failed: %s", exc)
        return

    if result is None:
        return

    log_level = logging.WARNING if result.status == DependencyScanStatus.VULNERABLE else logging.INFO
    logger.log(
        log_level,
        "Dependency scan completed: %s (%d findings)",
        result.status.value,
        len(result.findings),
    )
    orch._post_bulletin("status", f"dependency_scan: {result.summary}")


def load_existing_dependency_scan_task_titles(orch: Any) -> set[str]:
    """Load open remediation task titles so weekly scans do not duplicate them.

    Args:
        orch: The orchestrator instance.

    Returns:
        Set of existing task titles with status in
        ``{open, claimed, in_progress, pending_approval}``.
    """
    try:
        response = orch._client.get(f"{orch._config.server_url}/tasks")
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return set()

    if not isinstance(payload, list):
        return set()
    return {
        str(item.get("title", ""))
        for item in payload
        if isinstance(item, dict)
        and str(item.get("status", "")) in {"open", "claimed", "in_progress", "pending_approval"}
    }


def create_dependency_fix_task(
    orch: Any,
    finding: DependencyVulnerabilityFinding,
    existing_titles: set[str],
) -> str | None:
    """Create one remediation task per vulnerable package.

    Skips silently when a task with the same title is already open to
    avoid duplicate backlog entries across weekly scans.

    Args:
        orch: The orchestrator instance.
        finding: The vulnerability finding.
        existing_titles: Set of existing task titles for dedup. Updated
            in-place with the created title so later findings in the
            same scan do not re-create it.

    Returns:
        The title of the created task, or ``None`` if it was skipped or
        the POST failed.
    """
    title = f"Upgrade vulnerable dependency: {finding.package}"
    if title in existing_titles:
        return None

    description = (
        f"{finding.source} reported {finding.package} {finding.installed_version} as vulnerable.\n\n"
        f"Advisory: {finding.advisory_id}\n"
        f"Summary: {finding.summary or 'No summary provided.'}"
    )
    if finding.fix_versions:
        description += f"\nRecommended fix versions: {', '.join(finding.fix_versions)}"

    try:
        response = orch._client.post(
            f"{orch._config.server_url}/tasks",
            json={
                "title": title,
                "description": description,
                "role": "security",
                "priority": 2,
                "task_type": "fix",
            },
        )
        response.raise_for_status()
    except Exception as exc:
        logger.warning("Failed to create dependency fix task for %s: %s", finding.package, exc)
        return None

    existing_titles.add(title)
    return title
