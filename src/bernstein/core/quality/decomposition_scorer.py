"""Task decomposition quality scorer using historical data.

Analyses completed task archives to determine which decomposition
granularities (subtask counts) produce the best outcomes.  Use this to
score a proposed decomposition or to recommend an optimal subtask count
for a given task complexity and scope.

Example::

    buckets = analyze_historical_decompositions(Path(".sdd/archive/tasks.jsonl"))
    score   = score_decomposition(subtask_count=4, history=buckets)
    rec     = recommend_granularity("medium", "medium", history=buckets)
    report  = render_decomposition_report(buckets)
"""

from __future__ import annotations

import json
import logging
import math
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DecompositionStats:
    """Aggregate statistics for a group of completed decompositions.

    Attributes:
        subtask_count: Representative subtask count for the group.
        success_rate: Fraction of subtasks that completed successfully (0-1).
        avg_duration_s: Mean wall-clock duration of subtasks in seconds.
        avg_cost_usd: Mean cost per subtask in USD (0.0 when unknown).
        sample_size: Number of parent tasks contributing to this group.
    """

    subtask_count: int
    success_rate: float
    avg_duration_s: float
    avg_cost_usd: float
    sample_size: int


@dataclass(frozen=True)
class DecompositionScore:
    """Quality score for a proposed decomposition.

    Attributes:
        score: Overall quality score in [0, 1].
        subtask_count: The evaluated subtask count.
        recommendation: Human-readable recommendation string.
        confidence: Confidence in the score based on sample size (0-1).
        stats: Historical statistics for the matching bucket, if available.
    """

    score: float
    subtask_count: int
    recommendation: str
    confidence: float
    stats: DecompositionStats | None


@dataclass(frozen=True)
class GranularityBucket:
    """A bucket grouping decompositions by subtask count range.

    Attributes:
        min_subtasks: Lower bound (inclusive) of this bucket.
        max_subtasks: Upper bound (inclusive) of this bucket.
        stats: Aggregate statistics for tasks in this range.
    """

    min_subtasks: int
    max_subtasks: int
    stats: DecompositionStats


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

# Bucket boundaries: [1, 2-3, 4-6, 7-10, 11+]
_BUCKET_RANGES: list[tuple[int, int]] = [
    (1, 1),
    (2, 3),
    (4, 6),
    (7, 10),
    (11, 999),
]

# Minimum samples for a bucket to be considered statistically meaningful.
_MIN_CONFIDENT_SAMPLES = 5

# Heuristic ideal subtask counts per (complexity, scope) pair.
_IDEAL_SUBTASKS: dict[tuple[str, str], int] = {
    ("low", "small"): 1,
    ("low", "medium"): 2,
    ("low", "large"): 3,
    ("medium", "small"): 2,
    ("medium", "medium"): 4,
    ("medium", "large"): 6,
    ("high", "small"): 3,
    ("high", "medium"): 5,
    ("high", "large"): 8,
}


def _bucket_for(subtask_count: int) -> tuple[int, int]:
    """Return the (min, max) bucket range containing *subtask_count*."""
    for lo, hi in _BUCKET_RANGES:
        if lo <= subtask_count <= hi:
            return (lo, hi)
    # Fallback: shouldn't be reachable with the 11-999 catch-all.
    return _BUCKET_RANGES[-1]


def _confidence(sample_size: int) -> float:
    """Map sample size to a [0, 1] confidence value.

    Uses a logarithmic curve that reaches ~0.9 at 30 samples and
    saturates near 1.0 beyond 100.
    """
    if sample_size <= 0:
        return 0.0
    return min(1.0, math.log1p(sample_size) / math.log1p(100))


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
        with archive_path.open(encoding="utf-8") as f:
            for line_num, raw_line in enumerate(f, 1):
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


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def analyze_historical_decompositions(
    archive_path: Path,
) -> list[GranularityBucket]:
    """Read completed tasks and group by subtask count to compute success rates.

    Scans the archive for parent tasks (those whose ``result_summary``
    mentions subtask completion) and their child subtasks (those with a
    ``parent_task_id``).  Groups by subtask count and computes per-bucket
    statistics.

    Args:
        archive_path: Path to ``.sdd/archive/tasks.jsonl``.

    Returns:
        Sorted list of ``GranularityBucket`` objects, one per populated
        bucket range.
    """
    records = _read_archive(archive_path)
    if not records:
        return []

    # Index: parent_task_id -> list[child_record]
    children: dict[str, list[dict[str, object]]] = defaultdict(list)
    all_by_id: dict[str, dict[str, object]] = {}

    for rec in records:
        task_id = str(rec.get("task_id", ""))
        if task_id:
            all_by_id[task_id] = rec
        parent_id = rec.get("parent_task_id")
        if isinstance(parent_id, str) and parent_id:
            children[parent_id].append(rec)

    # Also detect parents by result_summary pattern when parent_task_id
    # is missing (older archive format).
    for rec in records:
        summary = str(rec.get("result_summary", ""))
        task_id = str(rec.get("task_id", ""))
        if "subtask" in summary.lower() and task_id not in children:
            # Not a known parent — skip.
            continue

    # Build per-bucket accumulators.
    # bucket_key -> list of (subtask_count, success_rate, avg_duration, avg_cost)
    bucket_data: dict[tuple[int, int], list[tuple[int, float, float, float]]] = defaultdict(list)

    for _parent_id, subs in children.items():
        count = len(subs)
        if count == 0:
            continue

        successes = sum(1 for s in subs if str(s.get("status", "")) == "done")
        rate = successes / count

        durations: list[float] = []
        for s in subs:
            dur_val = s.get("duration_seconds")
            if isinstance(dur_val, (int, float)):
                durations.append(float(dur_val))
        avg_dur = sum(durations) / len(durations) if durations else 0.0

        costs: list[float] = []
        for s in subs:
            cost_val = s.get("cost_usd")
            if isinstance(cost_val, (int, float)):
                costs.append(float(cost_val))
        avg_cost = sum(costs) / len(costs) if costs else 0.0

        key = _bucket_for(count)
        bucket_data[key].append((count, rate, avg_dur, avg_cost))

    # Aggregate into GranularityBucket objects.
    buckets: list[GranularityBucket] = []
    for lo, hi in sorted(bucket_data):
        entries = bucket_data[(lo, hi)]
        sample_size = len(entries)
        avg_count = round(sum(e[0] for e in entries) / sample_size)
        avg_rate = sum(e[1] for e in entries) / sample_size
        avg_dur = sum(e[2] for e in entries) / sample_size
        avg_cost = sum(e[3] for e in entries) / sample_size

        stats = DecompositionStats(
            subtask_count=avg_count,
            success_rate=round(avg_rate, 4),
            avg_duration_s=round(avg_dur, 2),
            avg_cost_usd=round(avg_cost, 4),
            sample_size=sample_size,
        )
        buckets.append(GranularityBucket(min_subtasks=lo, max_subtasks=hi, stats=stats))

    return buckets


def score_decomposition(
    subtask_count: int,
    history: list[GranularityBucket],
) -> DecompositionScore:
    """Score a proposed decomposition against historical data.

    The score combines the historical success rate for the matching
    granularity bucket with a heuristic penalty for extreme subtask
    counts (too few or too many).

    Args:
        subtask_count: Number of subtasks in the proposed decomposition.
        history: Buckets from ``analyze_historical_decompositions``.

    Returns:
        A ``DecompositionScore`` with the overall quality assessment.
    """
    if subtask_count < 1:
        return DecompositionScore(
            score=0.0,
            subtask_count=subtask_count,
            recommendation="Subtask count must be at least 1.",
            confidence=1.0,
            stats=None,
        )

    # Find the matching bucket.
    target = _bucket_for(subtask_count)
    matched: GranularityBucket | None = None
    for bucket in history:
        if bucket.min_subtasks == target[0] and bucket.max_subtasks == target[1]:
            matched = bucket
            break

    if matched is None:
        # No historical data for this range — return a heuristic-only score.
        heuristic = _heuristic_score(subtask_count)
        return DecompositionScore(
            score=round(heuristic, 4),
            subtask_count=subtask_count,
            recommendation=_heuristic_recommendation(subtask_count),
            confidence=0.0,
            stats=None,
        )

    stats = matched.stats
    conf = _confidence(stats.sample_size)

    # Blend historical success rate with a heuristic shape penalty.
    heuristic = _heuristic_score(subtask_count)
    blended = stats.success_rate * 0.7 + heuristic * 0.3
    final_score = round(min(1.0, max(0.0, blended)), 4)

    if final_score >= 0.8:
        recommendation = (
            f"Good granularity. Historical success rate {stats.success_rate:.0%} across {stats.sample_size} samples."
        )
    elif final_score >= 0.5:
        recommendation = (
            f"Acceptable granularity. Consider adjusting — historical success rate is {stats.success_rate:.0%}."
        )
    else:
        recommendation = (
            f"Poor granularity. Historical success rate only {stats.success_rate:.0%}. Consider splitting differently."
        )

    return DecompositionScore(
        score=final_score,
        subtask_count=subtask_count,
        recommendation=recommendation,
        confidence=round(conf, 4),
        stats=stats,
    )


def recommend_granularity(
    task_complexity: str,
    task_scope: str,
    history: list[GranularityBucket],
) -> DecompositionScore:
    """Recommend an optimal subtask count for a given complexity and scope.

    If historical data contains a bucket with both high success rate and
    sufficient samples, that bucket is preferred.  Otherwise falls back
    to a heuristic based on complexity/scope.

    Args:
        task_complexity: One of ``"low"``, ``"medium"``, ``"high"``.
        task_scope: One of ``"small"``, ``"medium"``, ``"large"``.
        history: Buckets from ``analyze_historical_decompositions``.

    Returns:
        A ``DecompositionScore`` for the recommended subtask count.
    """
    complexity = task_complexity.lower()
    scope = task_scope.lower()
    ideal = _IDEAL_SUBTASKS.get((complexity, scope), 4)

    # If we have meaningful history, find the best-performing bucket.
    best_bucket: GranularityBucket | None = None
    best_score: float = -1.0

    for bucket in history:
        if bucket.stats.sample_size < _MIN_CONFIDENT_SAMPLES:
            continue
        # Penalise buckets far from the heuristic ideal.
        mid = (bucket.min_subtasks + bucket.max_subtasks) / 2
        distance_penalty = 1.0 / (1.0 + abs(mid - ideal) * 0.15)
        adjusted = bucket.stats.success_rate * distance_penalty
        if adjusted > best_score:
            best_score = adjusted
            best_bucket = bucket

    if best_bucket is not None:
        recommended = best_bucket.stats.subtask_count
        conf = _confidence(best_bucket.stats.sample_size)
        recommendation = (
            f"Recommend {recommended} subtasks for {complexity}/{scope} tasks "
            f"based on {best_bucket.stats.success_rate:.0%} historical success "
            f"rate ({best_bucket.stats.sample_size} samples)."
        )
        return DecompositionScore(
            score=round(min(1.0, best_score), 4),
            subtask_count=recommended,
            recommendation=recommendation,
            confidence=round(conf, 4),
            stats=best_bucket.stats,
        )

    # Fallback: pure heuristic.
    return DecompositionScore(
        score=round(_heuristic_score(ideal), 4),
        subtask_count=ideal,
        recommendation=(
            f"No sufficient historical data. Heuristic suggests {ideal} subtasks for {complexity}/{scope} tasks."
        ),
        confidence=0.0,
        stats=None,
    )


def render_decomposition_report(buckets: list[GranularityBucket]) -> str:
    """Render a Markdown report showing success rates by granularity bucket.

    Args:
        buckets: Output from ``analyze_historical_decompositions``.

    Returns:
        Markdown-formatted string.
    """
    if not buckets:
        return "# Decomposition Report\n\nNo historical decomposition data available.\n"

    lines: list[str] = [
        "# Decomposition Report",
        "",
        "| Subtasks | Success Rate | Avg Duration | Avg Cost | Samples |",
        "|----------|-------------|-------------|----------|---------|",
    ]

    for bucket in buckets:
        s = bucket.stats
        label = (
            f"{bucket.min_subtasks}"
            if bucket.min_subtasks == bucket.max_subtasks
            else f"{bucket.min_subtasks}-{bucket.max_subtasks}"
        )
        lines.append(
            f"| {label} | {s.success_rate:.0%} | {s.avg_duration_s:.0f}s | ${s.avg_cost_usd:.2f} | {s.sample_size} |"
        )

    # Find the best bucket.
    confident = [b for b in buckets if b.stats.sample_size >= _MIN_CONFIDENT_SAMPLES]
    if confident:
        best = max(confident, key=lambda b: b.stats.success_rate)
        lines.append("")
        lines.append(
            f"**Best granularity**: {best.min_subtasks}-{best.max_subtasks} "
            f"subtasks ({best.stats.success_rate:.0%} success rate, "
            f"{best.stats.sample_size} samples)."
        )
    else:
        lines.append("")
        lines.append("_Insufficient samples for a confident recommendation._")

    lines.append("")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Internal heuristics
# ---------------------------------------------------------------------------


def _heuristic_score(subtask_count: int) -> float:
    """Return a [0, 1] heuristic quality score for a subtask count.

    Models an inverted-U curve peaking around 3-5 subtasks — aligning
    with the principle that moderate decomposition works best.
    """
    # Peak at 4 subtasks, gaussian-like falloff.
    peak = 4.0
    sigma = 3.0
    return math.exp(-((subtask_count - peak) ** 2) / (2 * sigma**2))


def _heuristic_recommendation(subtask_count: int) -> str:
    """Generate a recommendation string when no historical data exists."""
    if subtask_count <= 1:
        return "Consider splitting into smaller subtasks for better parallelism."
    if subtask_count <= 3:
        return "Reasonable granularity. Good balance of parallelism and overhead."
    if subtask_count <= 6:
        return "Good granularity for medium-to-large tasks."
    if subtask_count <= 10:
        return "Many subtasks. Watch for coordination overhead."
    return "Very high subtask count. Risk of excessive coordination overhead."
