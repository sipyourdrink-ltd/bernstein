"""Team adoption dashboard showing Bernstein usage metrics across an org.

Reads the task archive (``.sdd/archive/tasks.jsonl``) and aggregates
per-user and per-team metrics for engineering managers to track adoption,
cost, quality, and throughput.

Example::

    from pathlib import Path
    from bernstein.core.observability.team_dashboard import (
        aggregate_user_metrics,
        aggregate_team_metrics,
        compute_adoption_score,
        render_team_dashboard_data,
    )

    archive = Path(".sdd/archive/tasks.jsonl")
    user = aggregate_user_metrics(archive, "alice")
    team = aggregate_team_metrics(archive, "backend-squad", ["alice", "bob"])
    score = compute_adoption_score(team)
    payload = render_team_dashboard_data(team)
"""

from __future__ import annotations

import json
import logging
import math
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class UserMetrics:
    """Per-user usage metrics aggregated from the task archive.

    Attributes:
        user_id: Identifier for the user (matches assigned_agent substring).
        total_runs: Number of tasks assigned to this user.
        tasks_completed: Number of tasks with status 'done'.
        tasks_failed: Number of tasks with status 'failed'.
        total_cost_usd: Total cost in USD across all tasks.
        code_lines_merged: Total number of files listed in owned_files
            across completed tasks (proxy for code contribution).
        quality_gate_pass_rate: Fraction of completed tasks vs total
            terminal tasks (0.0-1.0).
    """

    user_id: str
    total_runs: int
    tasks_completed: int
    tasks_failed: int
    total_cost_usd: float
    code_lines_merged: int
    quality_gate_pass_rate: float


@dataclass(frozen=True)
class TeamMetrics:
    """Aggregate team metrics composed from individual user metrics.

    Attributes:
        team_name: Human-readable team name.
        users: Tuple of per-user metrics for each team member.
        total_runs: Sum of all users' total_runs.
        total_cost_usd: Sum of all users' total_cost_usd.
        avg_quality_score: Mean quality_gate_pass_rate across users
            (0.0-1.0).
        adoption_score: Computed adoption score (0.0-1.0).
    """

    team_name: str
    users: tuple[UserMetrics, ...]
    total_runs: int
    total_cost_usd: float
    avg_quality_score: float
    adoption_score: float


# ---------------------------------------------------------------------------
# Archive reader
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
# User-level aggregation
# ---------------------------------------------------------------------------


def aggregate_user_metrics(archive_path: Path, user_id: str) -> UserMetrics:
    """Aggregate metrics for a single user from the task archive.

    Filters archive records where ``assigned_agent`` contains *user_id*
    (case-insensitive substring match) and computes counts, costs, and
    quality rate.

    Args:
        archive_path: Path to ``.sdd/archive/tasks.jsonl``.
        user_id: User identifier to match against assigned_agent.

    Returns:
        Frozen ``UserMetrics`` dataclass with aggregated numbers.
    """
    records = _read_archive(archive_path)
    user_lower = user_id.lower()

    total_runs = 0
    tasks_completed = 0
    tasks_failed = 0
    total_cost = 0.0
    code_lines_merged = 0

    for rec in records:
        agent = str(rec.get("assigned_agent", "") or "")
        if user_lower not in agent.lower():
            continue

        total_runs += 1
        status = str(rec.get("status", "")).lower()
        if status == "done":
            tasks_completed += 1
            owned = rec.get("owned_files")
            if isinstance(owned, list):
                code_lines_merged += len(cast("list[str]", owned))
        elif status == "failed":
            tasks_failed += 1

        cost = rec.get("cost_usd")
        if isinstance(cost, int | float) and cost > 0:
            total_cost += float(cost)

    terminal = tasks_completed + tasks_failed
    quality_rate = tasks_completed / terminal if terminal > 0 else 0.0

    return UserMetrics(
        user_id=user_id,
        total_runs=total_runs,
        tasks_completed=tasks_completed,
        tasks_failed=tasks_failed,
        total_cost_usd=round(total_cost, 6),
        code_lines_merged=code_lines_merged,
        quality_gate_pass_rate=round(quality_rate, 4),
    )


# ---------------------------------------------------------------------------
# Team-level aggregation
# ---------------------------------------------------------------------------


def aggregate_team_metrics(
    archive_path: Path,
    team_name: str,
    user_ids: list[str],
) -> TeamMetrics:
    """Aggregate metrics across multiple users into a team summary.

    Calls ``aggregate_user_metrics`` for each user and combines the
    results into a single ``TeamMetrics`` instance.  The adoption score
    is computed via ``compute_adoption_score``.

    Args:
        archive_path: Path to ``.sdd/archive/tasks.jsonl``.
        team_name: Human-readable team name.
        user_ids: List of user identifiers for team members.

    Returns:
        Frozen ``TeamMetrics`` dataclass with per-user and team totals.
    """
    user_metrics_list = [aggregate_user_metrics(archive_path, uid) for uid in user_ids]
    users = tuple(user_metrics_list)

    total_runs = sum(u.total_runs for u in users)
    total_cost = sum(u.total_cost_usd for u in users)

    quality_scores = [u.quality_gate_pass_rate for u in users if u.total_runs > 0]
    avg_quality = sum(quality_scores) / len(quality_scores) if quality_scores else 0.0

    # Build a preliminary TeamMetrics without adoption_score to pass to compute_adoption_score
    preliminary = TeamMetrics(
        team_name=team_name,
        users=users,
        total_runs=total_runs,
        total_cost_usd=round(total_cost, 6),
        avg_quality_score=round(avg_quality, 4),
        adoption_score=0.0,
    )
    adoption = compute_adoption_score(preliminary)

    return TeamMetrics(
        team_name=team_name,
        users=users,
        total_runs=total_runs,
        total_cost_usd=round(total_cost, 6),
        avg_quality_score=round(avg_quality, 4),
        adoption_score=adoption,
    )


# ---------------------------------------------------------------------------
# Adoption score
# ---------------------------------------------------------------------------

# Weights for the three adoption dimensions.
_ACTIVE_USER_WEIGHT = 0.4
_RUN_FREQUENCY_WEIGHT = 0.35
_QUALITY_TREND_WEIGHT = 0.25

# A team member is "active" if they have at least this many runs.
_ACTIVE_THRESHOLD = 1

# Logarithmic saturation point for run frequency.
# A team averaging ~50 runs/member saturates the frequency signal at ~1.0.
_FREQ_SATURATION = 50


def compute_adoption_score(team: TeamMetrics) -> float:
    """Compute an adoption score between 0.0 and 1.0 for a team.

    The score combines three signals:

    1. **Active user ratio** (weight 0.40): fraction of team members
       with at least ``_ACTIVE_THRESHOLD`` runs.
    2. **Run frequency** (weight 0.35): log-scaled average runs per
       user, saturating near 1.0 at ``_FREQ_SATURATION`` runs/user.
    3. **Quality trend** (weight 0.25): average quality gate pass rate
       across active users.

    Args:
        team: A ``TeamMetrics`` instance (adoption_score field is ignored).

    Returns:
        Float in [0.0, 1.0].
    """
    if not team.users:
        return 0.0

    total_members = len(team.users)

    # 1. Active user ratio
    active_count = sum(1 for u in team.users if u.total_runs >= _ACTIVE_THRESHOLD)
    active_ratio = active_count / total_members

    # 2. Run frequency (log-scaled per user)
    avg_runs = team.total_runs / total_members
    frequency_signal = min(1.0, math.log1p(avg_runs) / math.log1p(_FREQ_SATURATION))

    # 3. Quality trend
    quality_signal = team.avg_quality_score

    raw = (
        _ACTIVE_USER_WEIGHT * active_ratio
        + _RUN_FREQUENCY_WEIGHT * frequency_signal
        + _QUALITY_TREND_WEIGHT * quality_signal
    )
    return round(min(1.0, max(0.0, raw)), 4)


# ---------------------------------------------------------------------------
# Dashboard renderer
# ---------------------------------------------------------------------------


def render_team_dashboard_data(team: TeamMetrics) -> dict[str, object]:
    """Render team metrics as a JSON-serialisable dict for the API.

    Args:
        team: A ``TeamMetrics`` instance.

    Returns:
        Dict suitable for ``json.dumps`` / FastAPI ``JSONResponse``.
    """
    return {
        "timestamp": time.time(),
        "team_name": team.team_name,
        "total_runs": team.total_runs,
        "total_cost_usd": team.total_cost_usd,
        "avg_quality_score": team.avg_quality_score,
        "adoption_score": team.adoption_score,
        "users": [
            {
                "user_id": u.user_id,
                "total_runs": u.total_runs,
                "tasks_completed": u.tasks_completed,
                "tasks_failed": u.tasks_failed,
                "total_cost_usd": u.total_cost_usd,
                "code_lines_merged": u.code_lines_merged,
                "quality_gate_pass_rate": u.quality_gate_pass_rate,
            }
            for u in team.users
        ],
    }
