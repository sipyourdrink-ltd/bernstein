"""Incident timeline: correlate incidents with metrics and traces.

Reconstructs a chronological event stream from logs, metrics, and traces
to provide full context for incident investigation and post-mortems.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, cast

if TYPE_CHECKING:
    from pathlib import Path

    pass

logger = logging.getLogger(__name__)


@dataclass
class TimelineEvent:
    """A single event on an incident timeline."""

    timestamp: float
    kind: Literal[
        "error",
        "task_completed",
        "task_failed",
        "agent_spawned",
        "agent_crashed",
        "slo_breach",
        "incident_created",
        "incident_mitigated",
        "incident_resolved",
        "trace_step",
        "metric_anomaly",
    ]
    source: str  # e.g. "metrics", "traces", "incident", "slo"
    summary: str
    details: dict[str, Any] = field(default_factory=lambda: {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "timestamp": self.timestamp,
            "time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.timestamp)),
            "kind": self.kind,
            "source": self.source,
            "summary": self.summary,
            "details": self.details,
        }


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read all records from a JSONL file."""
    records: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line:
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return records


def _load_incident_json(incident_id: str, runtime_dir: Path) -> dict[str, Any] | None:
    """Load an incident JSON file."""
    path = runtime_dir / "incidents" / f"{incident_id}.json"
    try:
        return cast_to_dict(json.loads(path.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError):
        return None


def cast_to_dict(obj: Any) -> dict[str, Any]:
    """Cast an object to dict, returning empty dict on failure."""
    if isinstance(obj, dict):
        return obj  # type: ignore[return-value]
    return {}


def _collect_error_events(metrics_dir: Path, start_ts: float, end_ts: float) -> list[TimelineEvent]:
    """Collect error-rate metric events in the time window."""
    events: list[TimelineEvent] = []
    for path in sorted(metrics_dir.glob("error_rate_*.jsonl")):
        for rec in _read_jsonl(path):
            ts = rec.get("timestamp", 0)
            if start_ts <= ts <= end_ts:
                labels = rec.get("labels", {})
                error_type = labels.get("error_type", "unknown")
                provider = labels.get("provider", "")
                role = labels.get("role", "")
                summary = f"Error: {error_type}"
                if provider:
                    summary += f" ({provider})"
                if role:
                    summary += f" [{role}]"
                events.append(
                    TimelineEvent(
                        timestamp=ts,
                        kind="error",
                        source="metrics",
                        summary=summary,
                        details={
                            "error_type": error_type,
                            "provider": provider,
                            "role": role,
                            "value": rec.get("value"),
                        },
                    )
                )
    return events


def _collect_task_completion_events(metrics_dir: Path, start_ts: float, end_ts: float) -> list[TimelineEvent]:
    """Collect task completion metric events in the time window."""
    events: list[TimelineEvent] = []
    for path in sorted(metrics_dir.glob("task_completion_time_*.jsonl")):
        for rec in _read_jsonl(path):
            ts = rec.get("timestamp", 0)
            if start_ts <= ts <= end_ts:
                labels = rec.get("labels", {})
                task_id = labels.get("task_id", "?")
                role = labels.get("role", "?")
                success = labels.get("success", True)
                duration = rec.get("value", 0)
                kind = "task_completed" if success else "task_failed"
                summary = f"Task {task_id} ({role}) {'completed' if success else 'failed'} in {duration:.1f}s"
                events.append(
                    TimelineEvent(
                        timestamp=ts,
                        kind=kind,
                        source="metrics",
                        summary=summary,
                        details={"task_id": task_id, "role": role, "success": success, "duration_s": duration},
                    )
                )
    return events


def _collect_trace_events(traces_dir: Path, start_ts: float, end_ts: float) -> list[TimelineEvent]:
    """Collect trace step events in the time window."""
    events: list[TimelineEvent] = []
    for path in sorted(traces_dir.glob("*.json")):
        try:
            trace_data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        session_id = trace_data.get("session_id", "?")
        agent_role = trace_data.get("agent_role", "?")
        model = trace_data.get("model", "?")
        for step in trace_data.get("steps", []):
            ts = step.get("timestamp", 0)
            if start_ts <= ts <= end_ts:
                step_type = step.get("type", "?")
                detail = step.get("detail", "")
                kind = "trace_step"
                if step_type == "spawn":
                    kind = "agent_spawned"
                elif step_type == "fail":
                    kind = "agent_crashed"
                summary = (
                    f"[{agent_role}/{model}] {step_type}: {detail[:120]}"
                    if detail
                    else f"[{agent_role}/{model}] {step_type}"
                )
                events.append(
                    TimelineEvent(
                        timestamp=ts,
                        kind=kind,
                        source="traces",
                        summary=summary,
                        details={
                            "session_id": session_id,
                            "agent_role": agent_role,
                            "model": model,
                            "step_type": step_type,
                            "detail": detail,
                            "files": step.get("files", []),
                            "tokens": step.get("tokens", 0),
                        },
                    )
                )
    return events


def _collect_incident_lifecycle_events(incident: dict[str, Any]) -> list[TimelineEvent]:
    """Extract lifecycle events from an incident record."""
    events: list[TimelineEvent] = []
    inc_id = incident.get("id", "?")
    severity = incident.get("severity", "?")
    title = incident.get("title", "?")
    created_at = incident.get("created_at", 0)
    events.append(
        TimelineEvent(
            timestamp=created_at,
            kind="incident_created",
            source="incident",
            summary=f"Incident {inc_id} [{severity.upper()}]: {title}",
            details={"incident_id": inc_id, "severity": severity, "title": title},
        )
    )
    if incident.get("mitigated_at"):
        events.append(
            TimelineEvent(
                timestamp=incident["mitigated_at"],
                kind="incident_mitigated",
                source="incident",
                summary=f"Incident {inc_id} mitigated",
                details={"incident_id": inc_id, "remediation": incident.get("remediation", "")},
            )
        )
    if incident.get("resolved_at"):
        events.append(
            TimelineEvent(
                timestamp=incident["resolved_at"],
                kind="incident_resolved",
                source="incident",
                summary=f"Incident {inc_id} resolved",
                details={"incident_id": inc_id, "root_cause": incident.get("root_cause", "")},
            )
        )
    return events


def _collect_api_usage_events(metrics_dir: Path, start_ts: float, end_ts: float) -> list[TimelineEvent]:
    """Collect API usage anomalies (failures, high latency) in the time window."""
    events: list[TimelineEvent] = []
    for path in sorted(metrics_dir.glob("api_usage_*.jsonl")):
        for rec in _read_jsonl(path):
            ts = rec.get("timestamp", 0)
            if start_ts <= ts <= end_ts:
                labels = rec.get("labels", {})
                success = labels.get("success", True)
                if not success:
                    provider = labels.get("provider", "?")
                    model = labels.get("model", "?")
                    events.append(
                        TimelineEvent(
                            timestamp=ts,
                            kind="metric_anomaly",
                            source="metrics",
                            summary=f"API failure: {provider}/{model}",
                            details={"provider": provider, "model": model, "latency_ms": labels.get("latency_ms")},
                        )
                    )
    return events


def build_incident_timeline(
    incident_id: str,
    workdir: Path,
    window_before_s: float = 600,
    window_after_s: float = 300,
) -> dict[str, Any]:
    """Build a correlated incident timeline.

    Args:
        incident_id: The incident ID to build a timeline for.
        workdir: The project root (contains .sdd/).
        window_before_s: Seconds before incident creation to include.
        window_after_s: Seconds after incident creation to include.

    Returns:
        Dict with incident metadata and sorted timeline events.
    """
    sdd_dir = workdir / ".sdd"
    runtime_dir = sdd_dir / "runtime"
    metrics_dir = sdd_dir / "metrics"
    traces_dir = sdd_dir / "traces"

    incident = _load_incident_json(incident_id, runtime_dir)
    if incident is None:
        return {"error": f"Incident {incident_id} not found", "incident_id": incident_id}

    created_at = incident.get("created_at", time.time())
    start_ts = created_at - window_before_s
    end_ts = created_at + window_after_s

    # If incident was resolved, extend the window to cover resolution
    resolved_at = incident.get("resolved_at")
    if resolved_at and resolved_at > end_ts:
        end_ts = resolved_at + 60

    # Collect events from all sources
    events: list[TimelineEvent] = []
    events.extend(_collect_incident_lifecycle_events(incident))
    events.extend(_collect_error_events(metrics_dir, start_ts, end_ts))
    events.extend(_collect_task_completion_events(metrics_dir, start_ts, end_ts))
    events.extend(_collect_trace_events(traces_dir, start_ts, end_ts))
    events.extend(_collect_api_usage_events(metrics_dir, start_ts, end_ts))

    # Sort by timestamp
    events.sort(key=lambda e: e.timestamp)

    # Build the timeline report
    timeline_data = {
        "incident_id": incident_id,
        "severity": incident.get("severity"),
        "title": incident.get("title"),
        "status": incident.get("status"),
        "created_at": created_at,
        "window": {"start": start_ts, "end": end_ts, "before_s": window_before_s, "after_s": window_after_s},
        "event_count": len(events),
        "events": [e.to_dict() for e in events],
        "blast_radius": incident.get("blast_radius", []),
        "root_cause": incident.get("root_cause", ""),
        "remediation": incident.get("remediation", ""),
    }

    # Persist the timeline alongside the incident
    timeline_path = runtime_dir / "incidents" / f"{incident_id}-timeline.json"
    try:
        timeline_path.write_text(json.dumps(timeline_data, indent=2), encoding="utf-8")
    except OSError as exc:
        safe_id = incident_id.replace("\n", "").replace("\r", "")[:100]
        logger.warning("Failed to save incident timeline %s: %s", safe_id, exc)

    return timeline_data


def list_incidents(workdir: Path) -> list[dict[str, Any]]:
    """List all known incidents from the runtime directory."""
    incidents_dir = workdir / ".sdd" / "runtime" / "incidents"
    incidents: list[dict[str, Any]] = []
    if not incidents_dir.is_dir():
        return incidents
    for path in sorted(incidents_dir.glob("*.json")):
        # Skip timeline files
        if path.stem.endswith("-timeline"):
            continue
        try:
            parsed: Any = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                data: dict[str, Any] = cast("dict[str, Any]", parsed)
                incidents.append(
                    {
                        "id": data.get("id", path.stem),
                        "severity": data.get("severity", "?"),
                        "status": data.get("status", "?"),
                        "title": data.get("title", "?"),
                        "created_at": data.get("created_at", 0),
                    }
                )
        except (OSError, json.JSONDecodeError):
            continue
    return incidents
