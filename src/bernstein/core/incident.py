"""Incident response for agent systems.

When critical failures occur:
1. Auto-pause orchestration
2. Capture full state snapshot
3. Generate incident report
4. Notify via configured channels
5. Create post-mortem task for next run
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


class IncidentSeverity(StrEnum):
    """Incident severity levels."""

    SEV1 = "sev1"  # Critical: data loss, security breach, total failure
    SEV2 = "sev2"  # Major: >50% agents failing, SLO breach
    SEV3 = "sev3"  # Minor: single agent crash, recoverable error


class IncidentStatus(StrEnum):
    """Incident lifecycle status."""

    OPEN = "open"
    MITIGATED = "mitigated"
    RESOLVED = "resolved"
    POST_MORTEM = "post_mortem"


@dataclass
class StateSnapshot:
    """Full state capture at time of incident."""

    timestamp: float
    active_agents: list[dict[str, Any]]
    open_tasks: list[dict[str, Any]]
    failed_tasks: list[dict[str, Any]]
    error_budget_state: dict[str, Any]
    slo_dashboard: dict[str, Any]
    recent_errors: list[str]

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "active_agents": self.active_agents,
            "open_tasks": self.open_tasks,
            "failed_tasks": self.failed_tasks,
            "error_budget_state": self.error_budget_state,
            "slo_dashboard": self.slo_dashboard,
            "recent_errors": self.recent_errors,
        }


@dataclass
class Incident:
    """A tracked incident with full context."""

    id: str
    severity: IncidentSeverity
    title: str
    description: str
    status: IncidentStatus = IncidentStatus.OPEN
    created_at: float = field(default_factory=time.time)
    mitigated_at: float | None = None
    resolved_at: float | None = None
    snapshot: StateSnapshot | None = None
    blast_radius: list[str] = field(default_factory=list)  # Affected task IDs
    root_cause: str = ""
    remediation: str = ""
    post_mortem_task_id: str | None = None

    def mitigate(self, remediation: str = "") -> None:
        self.status = IncidentStatus.MITIGATED
        self.mitigated_at = time.time()
        if remediation:
            self.remediation = remediation

    def resolve(self, root_cause: str = "") -> None:
        self.status = IncidentStatus.RESOLVED
        self.resolved_at = time.time()
        if root_cause:
            self.root_cause = root_cause

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "severity": self.severity.value,
            "title": self.title,
            "description": self.description,
            "status": self.status.value,
            "created_at": self.created_at,
            "mitigated_at": self.mitigated_at,
            "resolved_at": self.resolved_at,
            "blast_radius": self.blast_radius,
            "root_cause": self.root_cause,
            "remediation": self.remediation,
            "post_mortem_task_id": self.post_mortem_task_id,
            "snapshot": self.snapshot.to_dict() if self.snapshot else None,
        }

    def to_markdown(self) -> str:
        """Generate a markdown incident report."""
        lines = [
            f"# Incident Report: {self.id}",
            "",
            f"**Severity:** {self.severity.value.upper()}",
            f"**Status:** {self.status.value}",
            f"**Created:** {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.created_at))}",
        ]
        if self.mitigated_at:
            lines.append(f"**Mitigated:** {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.mitigated_at))}")
        if self.resolved_at:
            lines.append(f"**Resolved:** {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(self.resolved_at))}")

        lines.extend(["", "## Description", "", self.description])

        if self.blast_radius:
            lines.extend(["", "## Blast Radius", ""])
            for task_id in self.blast_radius:
                lines.append(f"- Task: {task_id}")

        if self.root_cause:
            lines.extend(["", "## Root Cause", "", self.root_cause])

        if self.remediation:
            lines.extend(["", "## Remediation", "", self.remediation])

        if self.snapshot:
            lines.extend(
                [
                    "",
                    "## State Snapshot",
                    "",
                    f"- Active agents: {len(self.snapshot.active_agents)}",
                    f"- Open tasks: {len(self.snapshot.open_tasks)}",
                    f"- Failed tasks: {len(self.snapshot.failed_tasks)}",
                ]
            )
            if self.snapshot.recent_errors:
                lines.extend(["", "### Recent Errors", ""])
                for err in self.snapshot.recent_errors[:10]:
                    lines.append(f"- `{err[:200]}`")

        return "\n".join(lines)


@dataclass
class IncidentManager:
    """Manages incident lifecycle: detection, response, post-mortem."""

    incidents: list[Incident] = field(default_factory=list)
    auto_pause: bool = True
    _pause_requested: bool = False

    @property
    def should_pause(self) -> bool:
        """Whether orchestration should be paused due to incidents."""
        return self._pause_requested

    @property
    def open_incidents(self) -> list[Incident]:
        return [i for i in self.incidents if i.status == IncidentStatus.OPEN]

    def create_incident(
        self,
        severity: IncidentSeverity,
        title: str,
        description: str,
        blast_radius: list[str] | None = None,
        snapshot: StateSnapshot | None = None,
    ) -> Incident:
        """Create and register a new incident."""
        incident_id = f"INC-{int(time.time())}-{len(self.incidents) + 1:03d}"
        incident = Incident(
            id=incident_id,
            severity=severity,
            title=title,
            description=description,
            blast_radius=blast_radius or [],
            snapshot=snapshot,
        )
        self.incidents.append(incident)

        if self.auto_pause and severity in (IncidentSeverity.SEV1, IncidentSeverity.SEV2):
            self._pause_requested = True
            logger.critical(
                "INCIDENT %s [%s]: %s — orchestration pause requested",
                incident_id,
                severity.value,
                title,
            )
        else:
            logger.warning("INCIDENT %s [%s]: %s", incident_id, severity.value, title)

        return incident

    def clear_pause(self) -> None:
        """Resume orchestration after incidents are mitigated."""
        self._pause_requested = False

    def check_for_incidents(
        self,
        failed_task_count: int,
        total_task_count: int,
        consecutive_failures: int,
        error_budget_depleted: bool,
        recent_errors: list[str] | None = None,
    ) -> Incident | None:
        """Auto-detect incident conditions and create incidents."""
        # SEV1: >75% failure rate with 10+ tasks
        if total_task_count >= 10 and failed_task_count / total_task_count > 0.75:
            return self.create_incident(
                severity=IncidentSeverity.SEV1,
                title="Critical failure rate: >75% of tasks failing",
                description=(
                    f"{failed_task_count}/{total_task_count} tasks have failed. System may be in a degraded state."
                ),
            )

        # SEV2: Error budget depleted
        if error_budget_depleted and total_task_count >= 5:
            return self.create_incident(
                severity=IncidentSeverity.SEV2,
                title="Error budget depleted",
                description=(
                    f"Error budget exhausted with {failed_task_count} failures "
                    f"out of {total_task_count} tasks. Automatic remediation active."
                ),
            )

        # SEV3: 5+ consecutive failures
        if consecutive_failures >= 5:
            return self.create_incident(
                severity=IncidentSeverity.SEV3,
                title=f"{consecutive_failures} consecutive task failures",
                description=(f"Last {consecutive_failures} tasks have all failed. Possible systemic issue."),
            )

        return None

    def generate_post_mortem_task(self, incident: Incident) -> dict[str, str]:
        """Generate a task definition for post-mortem analysis."""
        return {
            "title": f"Post-mortem: {incident.title}",
            "description": (
                f"Investigate incident {incident.id} ({incident.severity.value}).\n\n"
                f"## What happened\n{incident.description}\n\n"
                f"## Root cause\n{incident.root_cause or 'TBD — needs investigation'}\n\n"
                f"## Blast radius\n{len(incident.blast_radius)} tasks affected\n\n"
                "## Action items\n"
                "- [ ] Identify root cause\n"
                "- [ ] Document timeline\n"
                "- [ ] Propose prevention measures\n"
                "- [ ] Update runbooks if applicable\n"
            ),
            "role": "qa",
            "priority": "1",
        }

    def save(self, runtime_dir: Path) -> None:
        """Persist incidents to disk."""
        incidents_dir = runtime_dir / "incidents"
        incidents_dir.mkdir(parents=True, exist_ok=True)

        for incident in self.incidents:
            # Write JSON for machine consumption
            json_path = incidents_dir / f"{incident.id}.json"
            try:
                json_path.write_text(json.dumps(incident.to_dict(), indent=2))
            except OSError as exc:
                logger.warning("Failed to save incident %s: %s", incident.id, exc)

            # Write markdown report for human consumption
            md_path = incidents_dir / f"{incident.id}.md"
            try:
                md_path.write_text(incident.to_markdown())
            except OSError as exc:
                logger.warning("Failed to save incident report %s: %s", incident.id, exc)

        # Clean up old incident files to prevent unbounded growth
        cleanup_old_incidents(incidents_dir)

    def get_summary(self) -> dict[str, Any]:
        """Return incident summary for dashboard display."""
        by_severity: dict[str, int] = {}
        by_status: dict[str, int] = {}
        for inc in self.incidents:
            by_severity[inc.severity.value] = by_severity.get(inc.severity.value, 0) + 1
            by_status[inc.status.value] = by_status.get(inc.status.value, 0) + 1
        return {
            "total": len(self.incidents),
            "open": len(self.open_incidents),
            "by_severity": by_severity,
            "by_status": by_status,
            "pause_active": self._pause_requested,
        }


_INCIDENT_MAX_AGE_SECONDS = 7 * 24 * 60 * 60  # 7 days


def cleanup_old_incidents(
    incidents_dir: Path,
    *,
    max_age_seconds: float = _INCIDENT_MAX_AGE_SECONDS,
) -> int:
    """Delete incident files older than *max_age_seconds*.

    Args:
        incidents_dir: Directory containing incident ``.json`` and ``.md`` files.
        max_age_seconds: Age threshold in seconds (default 7 days).

    Returns:
        Number of files deleted.
    """
    if not incidents_dir.is_dir():
        return 0
    cutoff = time.time() - max_age_seconds
    deleted = 0
    for path in list(incidents_dir.iterdir()):
        if path.suffix not in (".json", ".md"):
            continue
        try:
            if path.stat().st_mtime < cutoff:
                path.unlink()
                deleted += 1
        except OSError as exc:
            logger.debug("Failed to remove old incident %s: %s", path.name, exc)
    if deleted:
        logger.info("Cleaned up %d incident files older than %d days", deleted, int(max_age_seconds / 86400))
    return deleted
