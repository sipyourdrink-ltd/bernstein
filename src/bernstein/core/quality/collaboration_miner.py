"""Agent collaboration pattern mining from successful multi-agent runs.

Analyses the task archive (``.sdd/archive/tasks.jsonl``) to discover which
role-pair orderings correlate with higher success rates and fewer rework
cycles.  The mined patterns can feed back into the planner so future runs
schedule roles in empirically optimal order.

Typical usage::

    from pathlib import Path
    from bernstein.core.quality.collaboration_miner import (
        extract_collaborations,
        generate_recommendations,
        mine_patterns,
        render_patterns_report,
    )

    collabs = extract_collaborations(Path(".sdd/archive/tasks.jsonl"))
    result = mine_patterns(collabs)
    report = render_patterns_report(result)
"""

from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass
from itertools import combinations
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

_RETRY_RE = re.compile(r"^\[RETRY\s+\d+\]")


@dataclass(frozen=True)
class CollaborationPattern:
    """A mined collaboration pattern between two or more roles.

    Attributes:
        roles: Participating roles in canonical (sorted) order.
        ordering: How the roles executed -- ``"sequential"`` or ``"parallel"``.
        success_rate: Fraction of runs using this pattern that succeeded (0.0-1.0).
        avg_rework_cycles: Mean number of retry/fix tasks observed in these runs.
        sample_size: Number of runs from which this pattern was observed.
        description: Human-readable summary of the pattern.
    """

    roles: tuple[str, ...]
    ordering: str  # "sequential" | "parallel"
    success_rate: float
    avg_rework_cycles: float
    sample_size: int
    description: str


@dataclass(frozen=True)
class RunCollaboration:
    """Extracted collaboration data from a single orchestrator run.

    Attributes:
        run_id: Identifier for the run (e.g. session or tenant ID).
        role_sequence: Roles in the order they completed tasks.
        success: Whether all tasks in the run succeeded.
        rework_count: Number of retry / fix tasks observed.
        duration_s: Wall-clock duration of the run in seconds.
    """

    run_id: str
    role_sequence: tuple[str, ...]
    success: bool
    rework_count: int
    duration_s: float


@dataclass(frozen=True)
class MiningResult:
    """Aggregated output of the pattern mining pipeline.

    Attributes:
        patterns: Discovered collaboration patterns sorted by success rate.
        total_runs_analyzed: Number of distinct runs processed.
        recommendations: Actionable suggestions derived from the patterns.
    """

    patterns: tuple[CollaborationPattern, ...]
    total_runs_analyzed: int
    recommendations: tuple[str, ...]


# ---------------------------------------------------------------------------
# Archive reading
# ---------------------------------------------------------------------------


def _read_archive(archive_path: Path) -> list[dict[str, object]]:
    """Read all records from an archive JSONL file.

    Args:
        archive_path: Path to ``.sdd/archive/tasks.jsonl``.

    Returns:
        List of parsed JSON dicts.  Malformed lines are silently skipped.
    """
    if not archive_path.exists():
        return []

    records: list[dict[str, object]] = []
    try:
        with archive_path.open(encoding="utf-8") as fh:
            for line_num, raw_line in enumerate(fh, 1):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    data: dict[str, object] = json.loads(line)
                    records.append(data)
                except json.JSONDecodeError:
                    logger.warning(
                        "Skipping malformed archive line %d in %s",
                        line_num,
                        archive_path,
                    )
    except OSError as exc:
        logger.warning("Cannot read archive at %s: %s", archive_path, exc)
    return records


def _is_rework_task(title: str) -> bool:
    """Return ``True`` if *title* indicates a retry or fix task.

    Args:
        title: Task title string.

    Returns:
        Whether the title matches retry/fix naming conventions.
    """
    return bool(_RETRY_RE.match(title)) or title.startswith("Fix: ")


# ---------------------------------------------------------------------------
# Collaboration extraction
# ---------------------------------------------------------------------------


def extract_collaborations(archive_path: Path) -> list[RunCollaboration]:
    """Reconstruct per-run role ordering from the task archive.

    Groups archive records by ``claimed_by_session`` (treating each unique
    session as a run).  Within each run, tasks are sorted by
    ``completed_at`` to determine the role execution sequence.

    Args:
        archive_path: Path to ``.sdd/archive/tasks.jsonl``.

    Returns:
        List of ``RunCollaboration`` objects, one per run.
    """
    records = _read_archive(archive_path)
    if not records:
        return []

    # Group by session (run).  Use claimed_by_session when available,
    # otherwise fall back to tenant_id.
    runs: dict[str, list[dict[str, object]]] = defaultdict(list)
    for rec in records:
        run_key = str(rec.get("claimed_by_session") or rec.get("tenant_id") or "unknown")
        if run_key == "unknown":
            continue
        runs[run_key].append(rec)

    collaborations: list[RunCollaboration] = []
    for run_id, tasks in runs.items():
        if len(tasks) < 2:
            # Need at least two tasks to form a collaboration
            continue

        # Sort by completion time to get execution ordering
        sorted_tasks = sorted(tasks, key=lambda t: float(str(t.get("completed_at", 0))))

        role_sequence = tuple(str(t.get("role", "unknown")) for t in sorted_tasks)

        all_succeeded = all(str(t.get("status", "")) == "done" for t in sorted_tasks)

        rework_count = sum(1 for t in sorted_tasks if _is_rework_task(str(t.get("title", ""))))

        # Duration: earliest created_at to latest completed_at
        created_times = [float(str(t.get("created_at", 0))) for t in sorted_tasks]
        completed_times = [float(str(t.get("completed_at", 0))) for t in sorted_tasks]
        earliest = min(created_times) if created_times else 0.0
        latest = max(completed_times) if completed_times else 0.0
        duration_s = max(0.0, latest - earliest)

        collaborations.append(
            RunCollaboration(
                run_id=run_id,
                role_sequence=role_sequence,
                success=all_succeeded,
                rework_count=rework_count,
                duration_s=duration_s,
            )
        )

    return collaborations


# ---------------------------------------------------------------------------
# Pattern mining
# ---------------------------------------------------------------------------


def _detect_ordering(
    role_a: str,
    role_b: str,
    role_sequence: tuple[str, ...],
) -> str:
    """Determine whether *role_a* and *role_b* ran sequentially or in parallel.

    Sequential: all occurrences of role_a come before all occurrences of role_b
    (or vice versa).  Otherwise: parallel.

    Args:
        role_a: First role name.
        role_b: Second role name.
        role_sequence: Ordered tuple of role names from the run.

    Returns:
        ``"sequential"`` or ``"parallel"``.
    """
    indices_a = [i for i, r in enumerate(role_sequence) if r == role_a]
    indices_b = [i for i, r in enumerate(role_sequence) if r == role_b]

    if not indices_a or not indices_b:
        return "sequential"

    # Sequential if max(a) < min(b) or max(b) < min(a)
    if max(indices_a) < min(indices_b) or max(indices_b) < min(indices_a):
        return "sequential"
    return "parallel"


def mine_patterns(
    collaborations: list[RunCollaboration],
    min_support: int = 3,
) -> MiningResult:
    """Find frequent role-pair patterns and compute their success metrics.

    For each pair of roles that co-occur in at least ``min_support`` runs,
    computes success rate, average rework cycles, and ordering mode.

    Args:
        collaborations: Output of :func:`extract_collaborations`.
        min_support: Minimum number of runs a pair must appear in to be
            reported as a pattern.

    Returns:
        A ``MiningResult`` containing discovered patterns and recommendations.
    """
    if not collaborations:
        return MiningResult(patterns=(), total_runs_analyzed=0, recommendations=())

    # Accumulate per-pair statistics
    pair_data: dict[tuple[str, str], list[tuple[bool, int, str]]] = defaultdict(list)

    for collab in collaborations:
        unique_roles = sorted(set(collab.role_sequence))
        for role_a, role_b in combinations(unique_roles, 2):
            pair_key = (role_a, role_b)  # already sorted by combinations
            ordering = _detect_ordering(role_a, role_b, collab.role_sequence)
            pair_data[pair_key].append((collab.success, collab.rework_count, ordering))

    # Build patterns from pairs meeting minimum support
    patterns: list[CollaborationPattern] = []
    for (role_a, role_b), entries in pair_data.items():
        if len(entries) < min_support:
            continue

        successes = sum(1 for success, _, _ in entries if success)
        success_rate = successes / len(entries)
        avg_rework = sum(rework for _, rework, _ in entries) / len(entries)

        # Dominant ordering
        seq_count = sum(1 for _, _, o in entries if o == "sequential")
        par_count = len(entries) - seq_count
        ordering = "sequential" if seq_count >= par_count else "parallel"

        description = (
            f"{role_a} + {role_b} ({ordering}): "
            f"{success_rate:.0%} success over {len(entries)} runs, "
            f"{avg_rework:.1f} avg rework cycles"
        )

        patterns.append(
            CollaborationPattern(
                roles=(role_a, role_b),
                ordering=ordering,
                success_rate=success_rate,
                avg_rework_cycles=round(avg_rework, 2),
                sample_size=len(entries),
                description=description,
            )
        )

    # Sort by success rate descending, then sample size descending
    patterns.sort(key=lambda p: (-p.success_rate, -p.sample_size))

    recommendations = generate_recommendations(tuple(patterns))

    return MiningResult(
        patterns=tuple(patterns),
        total_runs_analyzed=len(collaborations),
        recommendations=recommendations,
    )


# ---------------------------------------------------------------------------
# Recommendation generation
# ---------------------------------------------------------------------------


def generate_recommendations(patterns: tuple[CollaborationPattern, ...]) -> tuple[str, ...]:
    """Produce actionable suggestions from mined collaboration patterns.

    Identifies high-performing sequential pairs and contrasts them with
    lower-performing alternatives to suggest ordering improvements.

    Args:
        patterns: Discovered collaboration patterns.

    Returns:
        Tuple of recommendation strings.
    """
    if not patterns:
        return ()

    recs: list[str] = []
    _recommend_high_success_sequential(patterns, recs)
    _recommend_low_rework(patterns, recs)
    _recommend_high_rework(patterns, recs)
    _recommend_ordering_comparison(patterns, recs)
    _recommend_qa_involvement(patterns, recs)
    return tuple(recs)


def _recommend_high_success_sequential(
    patterns: tuple[CollaborationPattern, ...], recs: list[str]
) -> None:
    """Add recommendations for high-success sequential patterns."""
    for pat in patterns:
        if pat.ordering == "sequential" and pat.sample_size >= 3 and pat.success_rate >= 0.8:
            recs.append(
                f"Running {pat.roles[0]} before {pat.roles[1]} (sequential) "
                f"achieves {pat.success_rate:.0%} success rate over "
                f"{pat.sample_size} runs."
            )


def _recommend_low_rework(
    patterns: tuple[CollaborationPattern, ...], recs: list[str]
) -> None:
    """Add recommendations for patterns with notably low rework."""
    for pat in patterns:
        if pat.avg_rework_cycles < 0.5 and pat.sample_size >= 3:
            recs.append(
                f"{pat.roles[0]} + {pat.roles[1]} ({pat.ordering}) has very low "
                f"rework ({pat.avg_rework_cycles:.1f} cycles) -- consider this pairing "
                f"for complex tasks."
            )


def _recommend_high_rework(
    patterns: tuple[CollaborationPattern, ...], recs: list[str]
) -> None:
    """Warn about patterns with high rework cycles."""
    for pat in patterns:
        if pat.avg_rework_cycles >= 2.0 and pat.sample_size >= 3:
            recs.append(
                f"{pat.roles[0]} + {pat.roles[1]} ({pat.ordering}) averages "
                f"{pat.avg_rework_cycles:.1f} rework cycles -- investigate "
                f"whether a different ordering or intermediate QA step would help."
            )


def _recommend_ordering_comparison(
    patterns: tuple[CollaborationPattern, ...], recs: list[str]
) -> None:
    """Compare parallel vs sequential success for same role pair."""
    pair_map: dict[tuple[str, ...], list[CollaborationPattern]] = defaultdict(list)
    for pat in patterns:
        pair_map[pat.roles].append(pat)

    for roles, variants in pair_map.items():
        if len(variants) < 2:
            continue
        best = max(variants, key=lambda v: v.success_rate)
        worst = min(variants, key=lambda v: v.success_rate)
        if best.success_rate - worst.success_rate >= 0.15:
            recs.append(
                f"{roles[0]} + {roles[1]}: {best.ordering} ordering outperforms "
                f"{worst.ordering} by {(best.success_rate - worst.success_rate):.0%} "
                f"success rate."
            )


def _recommend_qa_involvement(
    patterns: tuple[CollaborationPattern, ...], recs: list[str]
) -> None:
    """Recommend QA involvement if it reduces rework."""
    qa_patterns = [p for p in patterns if "qa" in p.roles and p.success_rate >= 0.7]
    if not qa_patterns:
        return

    avg_rework_with_qa = sum(p.avg_rework_cycles for p in qa_patterns) / len(qa_patterns)
    non_qa = [p for p in patterns if "qa" not in p.roles and p.sample_size >= 3]
    if not non_qa:
        return

    avg_rework_without_qa = sum(p.avg_rework_cycles for p in non_qa) / len(non_qa)
    if avg_rework_without_qa > 0 and avg_rework_with_qa < avg_rework_without_qa:
        reduction = (1 - avg_rework_with_qa / avg_rework_without_qa) * 100
        recs.append(
            f"QA involvement reduces rework by {reduction:.0f}% on average "
            f"({avg_rework_with_qa:.1f} vs {avg_rework_without_qa:.1f} cycles)."
        )


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def render_patterns_report(result: MiningResult) -> str:
    """Render a MiningResult as a Markdown report.

    Args:
        result: Output of :func:`mine_patterns`.

    Returns:
        Markdown-formatted report string.
    """
    lines: list[str] = []
    _line = lines.append

    _line("# Collaboration Pattern Report")
    _line("")
    _line(f"**Runs analyzed:** {result.total_runs_analyzed}")
    _line(f"**Patterns found:** {len(result.patterns)}")
    _line("")

    if result.patterns:
        _line("## Discovered Patterns")
        _line("")
        _line("| Roles | Ordering | Success Rate | Avg Rework | Samples |")
        _line("|-------|----------|-------------|------------|---------|")
        for pat in result.patterns:
            roles_str = " + ".join(pat.roles)
            _line(
                f"| {roles_str} | {pat.ordering} "
                f"| {pat.success_rate:.0%} | {pat.avg_rework_cycles:.1f} "
                f"| {pat.sample_size} |"
            )
        _line("")

    if result.recommendations:
        _line("## Recommendations")
        _line("")
        for rec in result.recommendations:
            _line(f"- {rec}")
        _line("")

    if not result.patterns:
        _line("*No collaboration patterns found. Need more multi-agent run data.*")
        _line("")

    return "\n".join(lines)
