"""Automated post-mortem report generation for failed orchestration runs.

Reads the ``.sdd/`` archive for a given run and produces a structured
:class:`PostMortem` report covering:

- Chronological event timeline reconstructed from replay logs and metrics
- Detected failure patterns (repeated failures, cascades, timeouts, budget)
- Actionable recommendations keyed to each pattern
- Markdown rendering for human consumption

Data sources (read-only):

- ``.sdd/runs/{run_id}/summary.json`` -- high-level run summary
- ``.sdd/runs/{run_id}/replay.jsonl`` -- deterministic replay event log
- ``.sdd/metrics/task_*.json`` / ``*.jsonl`` -- per-task metrics
"""

from __future__ import annotations

import json
import logging
import time
from collections import Counter
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data classes (all frozen)
# ---------------------------------------------------------------------------


# Shared cast-type constants to avoid string duplication (Sonar S1192).
_CAST_DICT_STR_ANY = "dict[str, Any]"


@dataclass(frozen=True)
class TimelineEvent:
    """A single event in the chronological run timeline.

    Attributes:
        timestamp: Unix epoch seconds when the event occurred.
        event_type: Category string such as ``"task_started"``,
            ``"task_completed"``, ``"task_failed"``, ``"agent_spawned"``.
        description: Human-readable description of the event.
        agent_id: Identifier of the agent involved, if applicable.
        task_id: Identifier of the task involved, if applicable.
    """

    timestamp: float
    event_type: str
    description: str
    agent_id: str | None = None
    task_id: str | None = None


@dataclass(frozen=True)
class FailurePattern:
    """A detected failure pattern across the run.

    Attributes:
        pattern_name: Machine-readable pattern identifier (e.g.
            ``"repeated_file_failure"``).
        description: Human-readable explanation of the pattern.
        occurrences: Number of times the pattern was observed.
        affected_tasks: Task identifiers affected by this pattern.
    """

    pattern_name: str
    description: str
    occurrences: int
    affected_tasks: tuple[str, ...]


@dataclass(frozen=True)
class PostMortem:
    """Full post-mortem report for a failed orchestration run.

    Attributes:
        run_id: Orchestrator run identifier.
        start_time: Unix epoch when the run started.
        end_time: Unix epoch when the run ended.
        timeline: Chronological events ordered by timestamp.
        root_causes: Detected failure patterns that contributed to the
            run failure.
        contributing_factors: Additional contextual factors (e.g.
            ``"High agent concurrency"``, ``"Large task scope"``).
        recommendations: Actionable steps to prevent recurrence.
        summary: One-paragraph natural-language summary of the failure.
    """

    run_id: str
    start_time: float
    end_time: float
    timeline: tuple[TimelineEvent, ...]
    root_causes: tuple[FailurePattern, ...]
    contributing_factors: tuple[str, ...]
    recommendations: tuple[str, ...]
    summary: str


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read all JSON records from a JSONL file.

    Args:
        path: Path to the JSONL file.

    Returns:
        List of parsed dicts; malformed lines are silently skipped.
    """
    records: list[dict[str, Any]] = []
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                parsed: Any = json.loads(line)
                if isinstance(parsed, dict):
                    records.append(cast(_CAST_DICT_STR_ANY, parsed))
            except json.JSONDecodeError:
                continue
    except OSError:
        pass
    return records


def _load_summary(archive_path: Path, run_id: str) -> dict[str, Any]:
    """Load summary.json for the given run.

    Args:
        archive_path: Path to the ``.sdd`` directory.
        run_id: Run identifier.

    Returns:
        Parsed summary dict, or empty dict on failure.
    """
    summary_path = archive_path / "runs" / run_id / "summary.json"
    if not summary_path.exists():
        return {}
    try:
        raw: Any = json.loads(summary_path.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            return cast(_CAST_DICT_STR_ANY, raw)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to load summary.json for run %s: %s", run_id, exc)
    return {}


def _load_replay_events(archive_path: Path, run_id: str) -> list[dict[str, Any]]:
    """Load replay events from replay.jsonl for the given run.

    Args:
        archive_path: Path to the ``.sdd`` directory.
        run_id: Run identifier.

    Returns:
        List of replay event dicts, in file order.
    """
    replay_path = archive_path / "runs" / run_id / "replay.jsonl"
    return _read_jsonl(replay_path)


def _load_task_metrics(archive_path: Path) -> list[dict[str, Any]]:
    """Load per-task metrics from ``.sdd/metrics/task_*.json``.

    Falls back to scanning JSONL files for ``task_completion_time`` entries
    when no dedicated JSON files exist.

    Args:
        archive_path: Path to the ``.sdd`` directory.

    Returns:
        List of task metric dicts.
    """
    metrics_dir = archive_path / "metrics"
    if not metrics_dir.is_dir():
        return []

    results: list[dict[str, Any]] = []
    for f in sorted(metrics_dir.glob("task_*.json")):
        try:
            raw: Any = json.loads(f.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                results.append(cast(_CAST_DICT_STR_ANY, raw))
        except (OSError, json.JSONDecodeError):
            continue
    if results:
        return results

    # Fallback: scan JSONL for task_completion_time entries
    for f in sorted(metrics_dir.glob("*.jsonl")):
        try:
            for raw_line in f.read_text(encoding="utf-8").splitlines():
                stripped = raw_line.strip()
                if not stripped:
                    continue
                entry_raw: Any = json.loads(stripped)
                if not isinstance(entry_raw, dict):
                    continue
                entry = cast(_CAST_DICT_STR_ANY, entry_raw)
                if entry.get("metric_type") != "task_completion_time":
                    continue
                labels: dict[str, Any] = entry.get("labels") or {}
                results.append(
                    {
                        "task_id": str(labels.get("task_id", "")),
                        "role": str(labels.get("role", "")),
                        "model": str(labels.get("model", "")),
                        "success": str(labels.get("success", "false")).lower() == "true",
                        "session_id": str(labels.get("session_id", "")),
                        "start_time": 0.0,
                        "end_time": float(entry.get("timestamp", 0.0)),
                        "cost_usd": 0.0,
                    }
                )
        except (OSError, json.JSONDecodeError):
            continue
    return results


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def build_timeline(archive_path: Path, run_id: str) -> tuple[TimelineEvent, ...]:
    """Reconstruct the event timeline for a run from archive data.

    Reads both the replay log (``replay.jsonl``) and per-task metric files
    to assemble a chronological sequence of events.

    Args:
        archive_path: Path to the ``.sdd`` directory.
        run_id: Run identifier.

    Returns:
        Tuple of :class:`TimelineEvent` ordered by timestamp.
    """
    events: list[TimelineEvent] = []

    # -- replay events -------------------------------------------------------
    for rec in _load_replay_events(archive_path, run_id):
        ts = float(rec.get("ts", 0.0))
        event_name = str(rec.get("event", ""))
        if not event_name:
            continue

        agent_id = rec.get("agent_id")
        task_id = rec.get("task_id")

        description = _describe_replay_event(event_name, rec)

        events.append(
            TimelineEvent(
                timestamp=ts,
                event_type=event_name,
                description=description,
                agent_id=str(agent_id) if agent_id is not None else None,
                task_id=str(task_id) if task_id is not None else None,
            )
        )

    # -- task metric events --------------------------------------------------
    for tm in _load_task_metrics(archive_path):
        task_id = str(tm.get("task_id", ""))
        start = float(tm.get("start_time", 0.0))
        end = float(tm.get("end_time", 0.0))
        success = bool(tm.get("success", False))

        if start > 0:
            events.append(
                TimelineEvent(
                    timestamp=start,
                    event_type="task_started",
                    description=f"Task {task_id} started",
                    task_id=task_id,
                )
            )
        if end > 0:
            etype = "task_completed" if success else "task_failed"
            label = "completed" if success else "failed"
            events.append(
                TimelineEvent(
                    timestamp=end,
                    event_type=etype,
                    description=f"Task {task_id} {label}",
                    task_id=task_id,
                )
            )

    events.sort(key=lambda e: e.timestamp)
    return tuple(events)


def _describe_replay_event(event_name: str, rec: dict[str, Any]) -> str:
    """Build a human-readable description for a replay event.

    Args:
        event_name: The event type string.
        rec: Full replay record dict.

    Returns:
        A short description string.
    """
    task_id = rec.get("task_id", "")
    agent_id = rec.get("agent_id", "")

    descriptions: dict[str, str] = {
        "task_claimed": f"Agent {agent_id} claimed task {task_id}",
        "task_completed": f"Task {task_id} completed",
        "task_failed": f"Task {task_id} failed",
        "agent_spawned": f"Agent {agent_id} spawned",
        "agent_exited": f"Agent {agent_id} exited",
        "run_started": "Orchestration run started",
        "run_completed": "Orchestration run completed",
        "budget_exceeded": "Cost budget exceeded",
        "timeout": f"Task {task_id} timed out",
    }
    return descriptions.get(event_name, f"Event: {event_name}")


def detect_failure_patterns(
    timeline: tuple[TimelineEvent, ...],
) -> tuple[FailurePattern, ...]:
    """Identify common failure patterns in the event timeline.

    Detects:

    - **repeated_file_failure**: Multiple tasks failing that touch the same
      files or share characteristics.
    - **cascade_failure**: A burst of failures occurring in rapid succession
      after an initial failure.
    - **timeout_spiral**: Multiple timeout events indicating resource
      starvation or overly broad task scoping.
    - **budget_exhaustion**: The run hit its cost budget, preventing
      remaining tasks from executing.

    Args:
        timeline: Tuple of timeline events to analyse.

    Returns:
        Tuple of detected :class:`FailurePattern` instances.
    """
    patterns: list[FailurePattern] = []

    failed_events = [e for e in timeline if e.event_type == "task_failed"]
    timeout_events = [e for e in timeline if e.event_type == "timeout"]
    budget_events = [e for e in timeline if e.event_type == "budget_exceeded"]

    # -- Pattern 1: Repeated task failures -----------------------------------
    patterns.extend(_detect_repeated_failures(failed_events))

    # -- Pattern 2: Cascade failures -----------------------------------------
    patterns.extend(_detect_cascade_failures(failed_events))

    # -- Pattern 3: Timeout spirals ------------------------------------------
    patterns.extend(_detect_timeout_spiral(timeout_events, failed_events))

    # -- Pattern 4: Budget exhaustion ----------------------------------------
    patterns.extend(_detect_budget_exhaustion(budget_events, timeline))

    return tuple(patterns)


def _detect_repeated_failures(
    failed_events: list[TimelineEvent],
) -> list[FailurePattern]:
    """Detect repeated failures on the same task or by the same agent.

    Args:
        failed_events: Timeline events where ``event_type == "task_failed"``.

    Returns:
        List of detected patterns (may be empty).
    """
    patterns: list[FailurePattern] = []

    # Count failures per task_id
    task_failures: Counter[str] = Counter()
    for e in failed_events:
        if e.task_id:
            task_failures[e.task_id] += 1

    repeated = {tid: count for tid, count in task_failures.items() if count >= 2}
    if repeated:
        affected = tuple(sorted(repeated.keys()))
        total = sum(repeated.values())
        patterns.append(
            FailurePattern(
                pattern_name="repeated_file_failure",
                description=(
                    f"{len(repeated)} task(s) failed multiple times, "
                    f"totalling {total} failures. This suggests persistent "
                    "issues such as flaky tests, merge conflicts, or "
                    "fundamentally incorrect approach."
                ),
                occurrences=total,
                affected_tasks=affected,
            )
        )

    return patterns


def _detect_cascade_failures(
    failed_events: list[TimelineEvent],
) -> list[FailurePattern]:
    """Detect cascade failures -- a burst of failures in rapid succession.

    A cascade is defined as 3+ failures within a 60-second window.

    Args:
        failed_events: Timeline events where ``event_type == "task_failed"``.

    Returns:
        List of detected patterns (may be empty).
    """
    patterns: list[FailurePattern] = []
    if len(failed_events) < 3:
        return patterns

    cascade_window_s = 60.0
    sorted_failures = sorted(failed_events, key=lambda e: e.timestamp)

    # Sliding window: find bursts of 3+ failures within the window
    cascade_tasks: set[str] = set()
    cascade_count = 0

    for i, event in enumerate(sorted_failures):
        window_start = event.timestamp
        window_events = [e for e in sorted_failures[i:] if e.timestamp - window_start <= cascade_window_s]
        if len(window_events) >= 3 and len(window_events) > cascade_count:
            cascade_count = len(window_events)
            cascade_tasks = {e.task_id for e in window_events if e.task_id is not None}

    if cascade_count >= 3:
        patterns.append(
            FailurePattern(
                pattern_name="cascade_failure",
                description=(
                    f"{cascade_count} tasks failed within a 60-second window, "
                    "indicating a cascading failure. An upstream dependency "
                    "or shared resource likely caused a chain reaction."
                ),
                occurrences=cascade_count,
                affected_tasks=tuple(sorted(cascade_tasks)),
            )
        )

    return patterns


def _detect_timeout_spiral(
    timeout_events: list[TimelineEvent],
    failed_events: list[TimelineEvent],
) -> list[FailurePattern]:
    """Detect timeout spirals -- multiple timeouts suggesting resource starvation.

    Args:
        timeout_events: Timeline events where ``event_type == "timeout"``.
        failed_events: Timeline events where ``event_type == "task_failed"``.

    Returns:
        List of detected patterns (may be empty).
    """
    patterns: list[FailurePattern] = []
    if len(timeout_events) < 2:
        return patterns

    affected = {e.task_id for e in timeout_events if e.task_id is not None}
    # Also include failed tasks that mention timeout in their description
    for e in failed_events:
        if "timeout" in e.description.lower() and e.task_id is not None:
            affected.add(e.task_id)

    patterns.append(
        FailurePattern(
            pattern_name="timeout_spiral",
            description=(
                f"{len(timeout_events)} tasks timed out, suggesting resource "
                "starvation or tasks scoped too broadly for the configured "
                "time budget."
            ),
            occurrences=len(timeout_events),
            affected_tasks=tuple(sorted(affected)),
        )
    )

    return patterns


def _detect_budget_exhaustion(
    budget_events: list[TimelineEvent],
    timeline: tuple[TimelineEvent, ...],
) -> list[FailurePattern]:
    """Detect budget exhaustion -- the run hit its cost ceiling.

    Args:
        budget_events: Timeline events where
            ``event_type == "budget_exceeded"``.
        timeline: The full event timeline.

    Returns:
        List of detected patterns (may be empty).
    """
    patterns: list[FailurePattern] = []
    if not budget_events:
        return patterns

    # Find tasks that were still pending after budget was hit
    budget_ts = budget_events[0].timestamp
    pending_after: set[str] = set()
    started_tasks: set[str] = set()
    completed_tasks: set[str] = set()

    for e in timeline:
        if e.task_id is None:
            continue
        if e.event_type in ("task_started", "task_claimed"):
            started_tasks.add(e.task_id)
        elif e.event_type in ("task_completed", "task_failed"):
            completed_tasks.add(e.task_id)

    # Tasks started but not completed before budget hit
    for e in timeline:
        if (
            e.timestamp > budget_ts
            and e.task_id is not None
            and e.task_id in started_tasks
            and e.task_id not in completed_tasks
        ):
            pending_after.add(e.task_id)

    # Also count tasks that never started
    all_affected = pending_after | (started_tasks - completed_tasks)

    patterns.append(
        FailurePattern(
            pattern_name="budget_exhaustion",
            description=(
                "The run exhausted its cost budget, preventing "
                f"{len(all_affected)} remaining task(s) from completing. "
                "Consider increasing the budget or reducing task scope."
            ),
            occurrences=len(budget_events),
            affected_tasks=tuple(sorted(all_affected)),
        )
    )

    return patterns


def generate_recommendations(
    patterns: tuple[FailurePattern, ...],
) -> tuple[str, ...]:
    """Generate actionable recommendations based on detected failure patterns.

    Each pattern maps to one or more concrete recommendations for
    preventing recurrence.

    Args:
        patterns: Detected failure patterns from :func:`detect_failure_patterns`.

    Returns:
        Tuple of recommendation strings, deduplicated and ordered by
        pattern priority.
    """
    recommendation_map: dict[str, list[str]] = {
        "repeated_file_failure": [
            "Review repeatedly failing tasks for shared dependencies or "
            "flaky tests and fix root causes before re-running.",
            "Consider decomposing complex tasks into smaller, more focused subtasks to isolate failure points.",
        ],
        "cascade_failure": [
            "Add circuit-breaker logic to halt spawning when failures exceed a threshold within a time window.",
            "Identify the initial failure in the cascade and address it "
            "first -- downstream tasks likely depend on its output.",
        ],
        "timeout_spiral": [
            "Increase per-task timeout or reduce task scope to prevent resource starvation.",
            "Lower agent concurrency to reduce contention on shared resources (CPU, file locks, API rate limits).",
        ],
        "budget_exhaustion": [
            "Increase the cost budget ceiling or use cheaper models for "
            "low-complexity tasks to stretch the budget further.",
            "Enable cost-aware task prioritisation so high-value tasks run first before the budget is consumed.",
        ],
    }

    seen: set[str] = set()
    recommendations: list[str] = []
    for pattern in patterns:
        for rec in recommendation_map.get(pattern.pattern_name, []):
            if rec not in seen:
                seen.add(rec)
                recommendations.append(rec)

    if not recommendations and patterns:
        recommendations.append(
            "Review the failure timeline and agent logs to identify the root cause of the detected failure patterns."
        )

    return tuple(recommendations)


def _build_summary(
    run_id: str,
    summary: dict[str, Any],
    timeline: tuple[TimelineEvent, ...],
    patterns: tuple[FailurePattern, ...],
) -> str:
    """Build a one-paragraph natural-language summary of the run failure.

    Args:
        run_id: Run identifier.
        summary: Parsed ``summary.json`` data.
        timeline: Event timeline.
        patterns: Detected failure patterns.

    Returns:
        Summary paragraph string.
    """
    total = int(summary.get("tasks_total", 0))
    failed = int(summary.get("tasks_failed", 0))
    completed = int(summary.get("tasks_completed", 0))

    if total == 0:
        # Fall back to counting from timeline
        started = {e.task_id for e in timeline if e.event_type == "task_started" and e.task_id}
        failed_set = {e.task_id for e in timeline if e.event_type == "task_failed" and e.task_id}
        completed_set = {e.task_id for e in timeline if e.event_type == "task_completed" and e.task_id}
        total = len(started | failed_set | completed_set)
        failed = len(failed_set)
        completed = len(completed_set)

    parts: list[str] = [f"Run {run_id} processed {total} task(s): {completed} completed, {failed} failed."]

    if patterns:
        pattern_names = [p.pattern_name.replace("_", " ") for p in patterns]
        parts.append(f"Detected failure pattern(s): {', '.join(pattern_names)}.")

    wall_clock = float(summary.get("wall_clock_seconds", 0.0))
    if wall_clock > 0:
        minutes = wall_clock / 60
        parts.append(f"Total wall-clock time: {minutes:.1f} minutes.")

    cost = float(summary.get("total_cost_usd", 0.0))
    if cost > 0:
        parts.append(f"Total cost: ${cost:.4f}.")

    return " ".join(parts)


def generate_postmortem(archive_path: Path, run_id: str) -> PostMortem:
    """Generate a full post-mortem report for a run.

    Orchestrates the full pipeline: loads data from the ``.sdd/`` archive,
    builds the timeline, detects failure patterns, generates
    recommendations, and assembles the :class:`PostMortem`.

    Args:
        archive_path: Path to the ``.sdd`` directory.
        run_id: Run identifier.

    Returns:
        A fully populated :class:`PostMortem`.
    """
    summary = _load_summary(archive_path, run_id)

    timeline = build_timeline(archive_path, run_id)
    patterns = detect_failure_patterns(timeline)
    recommendations = generate_recommendations(patterns)

    # Determine start/end times
    start_time = float(summary.get("timestamp", 0.0))
    end_time = 0.0
    wall_clock = float(summary.get("wall_clock_seconds", 0.0))

    if start_time > 0 and wall_clock > 0:
        end_time = start_time + wall_clock
    elif timeline:
        if start_time < 1e-15:
            start_time = timeline[0].timestamp
        end_time = timeline[-1].timestamp

    if end_time < 1e-15:
        end_time = time.time()
    if start_time < 1e-15:
        start_time = end_time

    # Build contributing factors from summary data
    contributing_factors = _extract_contributing_factors(summary, timeline)

    pm_summary = _build_summary(run_id, summary, timeline, patterns)

    return PostMortem(
        run_id=run_id,
        start_time=start_time,
        end_time=end_time,
        timeline=timeline,
        root_causes=patterns,
        contributing_factors=contributing_factors,
        recommendations=recommendations,
        summary=pm_summary,
    )


def _extract_contributing_factors(
    summary: dict[str, Any],
    timeline: tuple[TimelineEvent, ...],
) -> tuple[str, ...]:
    """Extract contributing factors from summary and timeline data.

    Args:
        summary: Parsed ``summary.json`` data.
        timeline: Event timeline.

    Returns:
        Tuple of contributing factor description strings.
    """
    factors: list[str] = []

    total = int(summary.get("tasks_total", 0))
    failed = int(summary.get("tasks_failed", 0))
    if total > 0 and failed > 0:
        rate = (failed / total) * 100
        if rate >= 50:
            factors.append(f"High failure rate: {rate:.0f}% of tasks failed")

    cost = float(summary.get("total_cost_usd", 0.0))
    if cost > 5.0:
        factors.append(f"High cost: ${cost:.2f} total spend")

    # Check for agent spawn density
    spawns = [e for e in timeline if e.event_type == "agent_spawned"]
    if len(spawns) > 10:
        factors.append(f"High agent concurrency: {len(spawns)} agents spawned")

    return tuple(factors)


def render_postmortem_markdown(pm: PostMortem) -> str:
    """Render a :class:`PostMortem` as a Markdown report.

    Produces a self-contained Markdown document with sections for the
    summary, timeline, root causes, contributing factors, and
    recommendations.

    Args:
        pm: The post-mortem to render.

    Returns:
        Multi-line Markdown string.
    """
    import datetime

    lines: list[str] = []

    # -- Header --------------------------------------------------------------
    lines.append(f"# Post-Mortem Report: Run `{pm.run_id}`")
    lines.append("")

    start_str = _format_timestamp(pm.start_time)
    end_str = _format_timestamp(pm.end_time)
    duration = pm.end_time - pm.start_time
    duration_str = _format_duration(duration) if duration > 0 else "N/A"

    lines.append(f"**Start:** {start_str}")
    lines.append(f"**End:** {end_str}")
    lines.append(f"**Duration:** {duration_str}")
    lines.append(f"**Generated:** {datetime.datetime.now(datetime.UTC).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    lines.append("")

    # -- Summary -------------------------------------------------------------
    lines.append("## Summary")
    lines.append("")
    lines.append(pm.summary)
    lines.append("")

    # -- Timeline ------------------------------------------------------------
    lines.append("## Event Timeline")
    lines.append("")
    if pm.timeline:
        lines.append("| Time | Event Type | Description | Agent | Task |")
        lines.append("|------|-----------|-------------|-------|------|")
        for ev in pm.timeline:
            t_str = _format_timestamp(ev.timestamp) if ev.timestamp > 0 else "--"
            lines.append(
                f"| {t_str} | `{ev.event_type}` | {ev.description} | {ev.agent_id or '--'} | {ev.task_id or '--'} |"
            )
    else:
        lines.append("No timeline events recorded.")
    lines.append("")

    # -- Root causes ---------------------------------------------------------
    lines.append("## Root Causes")
    lines.append("")
    if pm.root_causes:
        for pattern in pm.root_causes:
            lines.append(f"### {pattern.pattern_name.replace('_', ' ').title()}")
            lines.append("")
            lines.append(pattern.description)
            lines.append("")
            lines.append(f"- **Occurrences:** {pattern.occurrences}")
            if pattern.affected_tasks:
                task_list = ", ".join(f"`{t}`" for t in pattern.affected_tasks)
                lines.append(f"- **Affected tasks:** {task_list}")
            lines.append("")
    else:
        lines.append("No specific failure patterns detected.")
    lines.append("")

    # -- Contributing factors ------------------------------------------------
    lines.append("## Contributing Factors")
    lines.append("")
    if pm.contributing_factors:
        for factor in pm.contributing_factors:
            lines.append(f"- {factor}")
    else:
        lines.append("No additional contributing factors identified.")
    lines.append("")

    # -- Recommendations -----------------------------------------------------
    lines.append("## Recommendations")
    lines.append("")
    if pm.recommendations:
        for i, rec in enumerate(pm.recommendations, 1):
            lines.append(f"{i}. {rec}")
    else:
        lines.append("No specific recommendations at this time.")
    lines.append("")

    return "\n".join(lines)


def _format_timestamp(ts: float) -> str:
    """Format a Unix timestamp as a human-readable string.

    Args:
        ts: Unix epoch seconds.

    Returns:
        Formatted datetime string, or ``"--"`` if *ts* is zero.
    """
    import datetime

    if ts <= 0:
        return "--"
    return datetime.datetime.fromtimestamp(ts, tz=datetime.UTC).strftime("%Y-%m-%d %H:%M:%S UTC")


def _format_duration(seconds: float) -> str:
    """Format a duration in seconds as a human-readable string.

    Args:
        seconds: Duration in seconds.

    Returns:
        E.g. ``"2h 15m 30s"`` or ``"45s"``.
    """
    s = int(seconds)
    hours, rem = divmod(s, 3600)
    minutes, secs = divmod(rem, 60)
    if hours:
        return f"{hours}h {minutes}m {secs}s"
    if minutes:
        return f"{minutes}m {secs}s"
    return f"{secs}s"
