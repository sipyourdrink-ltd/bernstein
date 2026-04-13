"""Cost-per-quality Pareto frontier optimization.

Finds Pareto-optimal model configurations that maximise quality for a
given cost (or minimise cost for a given quality bar).  A configuration
is *Pareto-optimal* when no other configuration is both cheaper **and**
higher quality.

The module reads historical task archive data
(``.sdd/archive/tasks.jsonl``) to build per-model cost/quality profiles,
then computes the Pareto frontier and generates recommendations.

Example::

    from pathlib import Path
    from bernstein.core.cost.pareto_frontier import (
        analyze_from_archive,
        recommend_for_quality,
        render_pareto_report,
    )

    frontier = analyze_from_archive(Path(".sdd/archive/tasks.jsonl"))
    cheap = recommend_for_quality(frontier, min_quality=0.8)
    print(render_pareto_report(frontier))
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ModelConfig:
    """Aggregated cost/quality profile for a single model.

    Attributes:
        model_name: Identifier of the model (e.g. ``"claude-sonnet-4-20250514"``).
        avg_cost_usd: Mean cost in USD across observed tasks.
        avg_quality_score: Mean quality score in ``[0, 1]``.
        sample_size: Number of tasks used to compute the averages.
    """

    model_name: str
    avg_cost_usd: float
    avg_quality_score: float
    sample_size: int


@dataclass(frozen=True)
class ParetoPoint:
    """A single point on the cost-quality plane.

    Attributes:
        model_name: The model this point represents.
        cost_usd: Average cost of using this model.
        quality_score: Average quality produced by this model.
        is_pareto_optimal: Whether this point lies on the Pareto frontier.
    """

    model_name: str
    cost_usd: float
    quality_score: float
    is_pareto_optimal: bool


@dataclass(frozen=True)
class ParetoFrontier:
    """The complete Pareto frontier for a task type.

    Attributes:
        points: All evaluated points (Pareto-optimal and dominated).
        task_type: Task type filter used (empty string for all types).
        recommendations: Human-readable recommendations derived from the
            frontier analysis.
    """

    points: tuple[ParetoPoint, ...]
    task_type: str
    recommendations: tuple[str, ...]


# ---------------------------------------------------------------------------
# Pareto computation
# ---------------------------------------------------------------------------


def compute_pareto_frontier(configs: list[ModelConfig]) -> list[ParetoPoint]:
    """Identify Pareto-optimal configurations from a list of model configs.

    A config is Pareto-optimal when no other config is *both* cheaper and
    higher quality.  Ties in cost or quality alone do not dominate.

    Args:
        configs: Model configurations to evaluate.

    Returns:
        List of :class:`ParetoPoint` for every input config, with
        ``is_pareto_optimal`` set appropriately.  Sorted by ascending
        cost.
    """
    if not configs:
        return []

    points: list[ParetoPoint] = []
    for cfg in configs:
        dominated = False
        for other in configs:
            if other is cfg:
                continue
            # ``other`` dominates ``cfg`` iff it is at least as good on both
            # axes and strictly better on at least one.
            if (
                other.avg_cost_usd <= cfg.avg_cost_usd
                and other.avg_quality_score >= cfg.avg_quality_score
                and (other.avg_cost_usd < cfg.avg_cost_usd or other.avg_quality_score > cfg.avg_quality_score)
            ):
                dominated = True
                break
        points.append(
            ParetoPoint(
                model_name=cfg.model_name,
                cost_usd=cfg.avg_cost_usd,
                quality_score=cfg.avg_quality_score,
                is_pareto_optimal=not dominated,
            )
        )

    points.sort(key=lambda p: (p.cost_usd, -p.quality_score))
    return points


# ---------------------------------------------------------------------------
# Recommendation helpers
# ---------------------------------------------------------------------------


def recommend_for_quality(
    frontier: ParetoFrontier,
    min_quality: float,
) -> ParetoPoint | None:
    """Find the cheapest Pareto-optimal config that meets a quality bar.

    Args:
        frontier: A computed Pareto frontier.
        min_quality: Minimum acceptable quality score in ``[0, 1]``.

    Returns:
        The cheapest Pareto-optimal point with
        ``quality_score >= min_quality``, or ``None`` if no config
        qualifies.
    """
    candidates = [p for p in frontier.points if p.is_pareto_optimal and p.quality_score >= min_quality]
    if not candidates:
        return None
    return min(candidates, key=lambda p: p.cost_usd)


def recommend_for_budget(
    frontier: ParetoFrontier,
    max_cost: float,
) -> ParetoPoint | None:
    """Find the highest-quality Pareto-optimal config within a budget.

    Args:
        frontier: A computed Pareto frontier.
        max_cost: Maximum acceptable cost in USD.

    Returns:
        The highest-quality Pareto-optimal point with
        ``cost_usd <= max_cost``, or ``None`` if no config qualifies.
    """
    candidates = [p for p in frontier.points if p.is_pareto_optimal and p.cost_usd <= max_cost]
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.quality_score)


# ---------------------------------------------------------------------------
# Archive analysis
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


def _quality_from_status(status: str) -> float:
    """Derive a quality score from task completion status.

    Maps terminal statuses to a ``[0, 1]`` quality score:
    - ``done`` -> ``1.0``
    - ``failed`` -> ``0.0``
    - anything else -> ``0.5``

    Args:
        status: Task status string from the archive record.

    Returns:
        Quality score in ``[0, 1]``.
    """
    if status == "done":
        return 1.0
    if status == "failed":
        return 0.0
    return 0.5


def _extract_model_name(record: dict[str, object]) -> str | None:
    """Extract a model name from an archive record.

    Checks ``model``, ``assigned_model``, and ``role`` fields in that
    order.  Returns ``None`` if no usable model identifier is found.

    Args:
        record: A single archive JSONL record.

    Returns:
        Model name string, or ``None``.
    """
    for key in ("model", "assigned_model"):
        val = record.get(key)
        if isinstance(val, str) and val:
            return val

    # Fall back to role as a grouping key when model is absent.
    role = record.get("role")
    if isinstance(role, str) and role:
        return f"role:{role}"

    return None


def analyze_from_archive(
    archive_path: Path,
    task_type: str = "",
) -> ParetoFrontier:
    """Build model configs from historical task data and compute the frontier.

    Reads ``.sdd/archive/tasks.jsonl``, groups completed tasks by model,
    computes average cost and quality per model, then returns the Pareto
    frontier with recommendations.

    Args:
        archive_path: Path to the archive JSONL file.
        task_type: Optional task type filter (empty string = all types).

    Returns:
        A :class:`ParetoFrontier` computed from the archive data.
    """
    records = _read_archive(archive_path)

    # Group by model
    model_costs: dict[str, list[float]] = {}
    model_qualities: dict[str, list[float]] = {}

    for rec in records:
        # Filter by task_type if specified
        if task_type:
            rec_type = rec.get("task_type", "")
            if isinstance(rec_type, str) and rec_type != task_type:
                continue

        model = _extract_model_name(rec)
        if model is None:
            continue

        cost = rec.get("cost_usd")
        if not isinstance(cost, (int, float)) or cost < 0:
            continue

        status = rec.get("status")
        if not isinstance(status, str):
            continue

        quality = _quality_from_status(status)

        model_costs.setdefault(model, []).append(float(cost))
        model_qualities.setdefault(model, []).append(quality)

    configs: list[ModelConfig] = []
    for model_name in model_costs:
        costs = model_costs[model_name]
        qualities = model_qualities[model_name]
        configs.append(
            ModelConfig(
                model_name=model_name,
                avg_cost_usd=sum(costs) / len(costs),
                avg_quality_score=sum(qualities) / len(qualities),
                sample_size=len(costs),
            )
        )

    points = compute_pareto_frontier(configs)
    recommendations = _generate_recommendations(points)

    return ParetoFrontier(
        points=tuple(points),
        task_type=task_type,
        recommendations=tuple(recommendations),
    )


def _generate_recommendations(points: list[ParetoPoint]) -> list[str]:
    """Generate human-readable recommendations from Pareto analysis.

    Args:
        points: All evaluated points (Pareto-optimal and dominated).

    Returns:
        List of recommendation strings.
    """
    if not points:
        return ["No data available for recommendations."]

    pareto = [p for p in points if p.is_pareto_optimal]
    dominated = [p for p in points if not p.is_pareto_optimal]

    recs: list[str] = []

    if pareto:
        cheapest = min(pareto, key=lambda p: p.cost_usd)
        best_quality = max(pareto, key=lambda p: p.quality_score)

        recs.append(
            f"Cheapest efficient option: {cheapest.model_name} "
            f"(${cheapest.cost_usd:.4f}, quality {cheapest.quality_score:.2f})"
        )

        if best_quality.model_name != cheapest.model_name:
            recs.append(
                f"Highest quality option: {best_quality.model_name} "
                f"(${best_quality.cost_usd:.4f}, quality {best_quality.quality_score:.2f})"
            )

    if dominated:
        names = ", ".join(p.model_name for p in dominated)
        recs.append(f"Dominated (inefficient) models: {names}")

    return recs


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def render_pareto_report(frontier: ParetoFrontier) -> str:
    """Render a Markdown report of the Pareto frontier.

    Produces a table listing all evaluated models with Pareto-optimal
    configurations highlighted, followed by recommendations.

    Args:
        frontier: A computed Pareto frontier.

    Returns:
        Markdown-formatted report string.
    """
    lines: list[str] = []

    title_suffix = f" ({frontier.task_type})" if frontier.task_type else ""
    lines.append(f"## Cost-Quality Pareto Frontier{title_suffix}")
    lines.append("")

    if not frontier.points:
        lines.append("No data available.")
        return "\n".join(lines)

    # Table header
    lines.append("| Model | Cost (USD) | Quality | Pareto Optimal |")
    lines.append("|-------|-----------|---------|----------------|")

    for point in frontier.points:
        marker = "**yes**" if point.is_pareto_optimal else "no"
        lines.append(f"| {point.model_name} | ${point.cost_usd:.4f} | {point.quality_score:.2f} | {marker} |")

    lines.append("")

    # Recommendations
    if frontier.recommendations:
        lines.append("### Recommendations")
        lines.append("")
        for rec in frontier.recommendations:
            lines.append(f"- {rec}")

    return "\n".join(lines)
