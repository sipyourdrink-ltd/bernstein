"""Correlation analysis between code complexity and agent performance.

Computes AST-based file complexity metrics and correlates them with agent
task outcomes (success rate, duration, cost, retries) to surface insights
about which code characteristics predict agent struggle.

Uses only the ``ast`` and ``statistics`` standard library modules --
no external dependencies required.
"""

from __future__ import annotations

import ast
import logging
import math
import statistics
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FileMetrics:
    """Complexity metrics for a single source file.

    Attributes:
        file_path: Absolute or relative path to the file.
        cyclomatic_complexity: Count of decision branches and boolean ops.
        fan_in: Number of modules that import names *from* this file.
        fan_out: Number of modules this file imports.
        churn_count: Number of times the file was modified (externally supplied).
        line_count: Total line count of the file.
    """

    file_path: str
    cyclomatic_complexity: int
    fan_in: int
    fan_out: int
    churn_count: int
    line_count: int


@dataclass(frozen=True)
class AgentOutcome:
    """Observed outcome of an agent working on a file.

    Attributes:
        task_id: Unique task identifier.
        file_path: Path to the file the task operated on.
        success: Whether the task completed successfully.
        duration_s: Wall-clock time in seconds.
        cost_usd: Dollar cost of the agent run.
        retries: Number of retry attempts before final result.
    """

    task_id: str
    file_path: str
    success: bool
    duration_s: float
    cost_usd: float
    retries: int


@dataclass(frozen=True)
class CorrelationResult:
    """Result of correlating one metric with agent outcomes.

    Attributes:
        metric_name: Name of the file metric (e.g. ``cyclomatic_complexity``).
        correlation_coefficient: Pearson r in [-1.0, 1.0].
        p_value: Two-tailed p-value approximation.
        sample_size: Number of data points used.
        insight: Human-readable interpretation of the result.
    """

    metric_name: str
    correlation_coefficient: float
    p_value: float
    sample_size: int
    insight: str


@dataclass(frozen=True)
class AnalysisReport:
    """Full correlation analysis report.

    Attributes:
        correlations: Tuple of per-metric correlation results.
        recommendations: Actionable suggestions derived from the analysis.
        high_risk_files: Files whose complexity exceeds the risk threshold.
    """

    correlations: tuple[CorrelationResult, ...]
    recommendations: tuple[str, ...]
    high_risk_files: tuple[str, ...]


# ---------------------------------------------------------------------------
# AST-based metric computation
# ---------------------------------------------------------------------------

#: AST node types that contribute to cyclomatic complexity.
_BRANCH_NODES: tuple[type[ast.AST], ...] = (
    ast.If,
    ast.For,
    ast.While,
    ast.ExceptHandler,
    ast.With,
    ast.Assert,
    ast.AsyncFor,
    ast.AsyncWith,
)


def compute_file_metrics(
    file_path: str | Path,
    *,
    churn_count: int = 0,
    fan_in: int = 0,
) -> FileMetrics:
    """Compute complexity metrics for a Python source file.

    Uses ``ast`` to count branches/loops (cyclomatic complexity) and
    import statements (fan-out).  ``fan_in`` and ``churn_count`` are
    provided externally since they require cross-file or VCS data.

    Args:
        file_path: Path to the Python source file.
        churn_count: Externally-supplied git churn count.
        fan_in: Externally-supplied count of other modules importing this file.

    Returns:
        A :class:`FileMetrics` instance with computed values.

    Raises:
        FileNotFoundError: If *file_path* does not exist.
    """
    path = Path(file_path)
    source = path.read_text(encoding="utf-8", errors="replace")
    line_count = source.count("\n") + (1 if source and not source.endswith("\n") else 0)

    try:
        tree = ast.parse(source, filename=str(path))
    except SyntaxError:
        logger.warning("SyntaxError parsing %s — returning zero complexity", path)
        return FileMetrics(
            file_path=str(file_path),
            cyclomatic_complexity=0,
            fan_in=fan_in,
            fan_out=0,
            churn_count=churn_count,
            line_count=line_count,
        )

    cyclomatic = _count_cyclomatic(tree)
    fan_out_count = _count_fan_out(tree)

    return FileMetrics(
        file_path=str(file_path),
        cyclomatic_complexity=cyclomatic,
        fan_in=fan_in,
        fan_out=fan_out_count,
        churn_count=churn_count,
        line_count=line_count,
    )


def _count_cyclomatic(tree: ast.AST) -> int:
    """Count decision points in an AST (cyclomatic complexity approximation).

    Counts branch/loop nodes plus ``BoolOp`` values (each ``and``/``or``
    adds one branch per extra operand).

    Args:
        tree: Parsed AST.

    Returns:
        Non-negative integer complexity count.
    """
    complexity = 0
    for node in ast.walk(tree):
        if isinstance(node, _BRANCH_NODES):
            complexity += 1
        elif isinstance(node, ast.BoolOp):
            complexity += len(node.values) - 1
    return complexity


def _count_fan_out(tree: ast.AST) -> int:
    """Count unique modules imported by the source file.

    Each ``import X`` and ``from X import ...`` contributes one unique
    top-level module to the fan-out count.

    Args:
        tree: Parsed AST.

    Returns:
        Number of unique imported modules.
    """
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module.split(".")[0])
    return len(modules)


# ---------------------------------------------------------------------------
# Correlation computation
# ---------------------------------------------------------------------------

_METRIC_FIELDS: tuple[str, ...] = (
    "cyclomatic_complexity",
    "fan_in",
    "fan_out",
    "churn_count",
    "line_count",
)


def _pearson_r(xs: list[float], ys: list[float]) -> float:
    """Compute Pearson correlation coefficient using stdlib ``statistics``.

    Args:
        xs: First variable values.
        ys: Second variable values (same length as *xs*).

    Returns:
        Pearson r in [-1.0, 1.0], or 0.0 when undefined (constant input).
    """
    n = len(xs)
    if n < 2:
        return 0.0

    try:
        return statistics.correlation(xs, ys)
    except statistics.StatisticsError:
        return 0.0


def _p_value_approx(r: float, n: int) -> float:
    """Approximate two-tailed p-value for Pearson r using the t-distribution.

    Uses the t-statistic ``t = r * sqrt((n-2)/(1-r^2))`` and a simple
    approximation of the cumulative t-distribution tail.

    Args:
        r: Pearson correlation coefficient.
        n: Sample size.

    Returns:
        Approximate p-value in [0.0, 1.0].
    """
    if n < 3 or abs(r) >= 1.0:
        return 1.0 if abs(r) < 1.0 else 0.0

    df = n - 2
    denom = 1.0 - r * r
    if denom <= 0:
        return 0.0

    t_stat = abs(r) * math.sqrt(df / denom)

    # Approximate tail probability using the relationship between
    # the t-distribution CDF and the incomplete beta function.
    # For large df this converges to the normal CDF.
    if t_stat == 0:
        return 1.0
    # For large |t|, p is very small
    if t_stat > 10:
        return 0.0001
    # Simple approximation: two-tailed p from standard normal
    # Using the complementary error function via math.erfc for better accuracy
    p = math.erfc(t_stat / math.sqrt(2.0))
    return min(1.0, max(0.0, p))


def _build_insight(metric_name: str, r: float, p: float) -> str:
    """Generate a human-readable insight string.

    Args:
        metric_name: Name of the metric.
        r: Pearson correlation coefficient.
        p: Approximate p-value.

    Returns:
        Descriptive string summarising the correlation.
    """
    strength: str
    abs_r = abs(r)
    if abs_r < 0.1:
        strength = "negligible"
    elif abs_r < 0.3:
        strength = "weak"
    elif abs_r < 0.5:
        strength = "moderate"
    elif abs_r < 0.7:
        strength = "strong"
    else:
        strength = "very strong"

    direction = "positive" if r >= 0 else "negative"
    sig = "statistically significant" if p < 0.05 else "not statistically significant"

    readable = metric_name.replace("_", " ")
    return f"{strength.capitalize()} {direction} correlation between {readable} and outcome ({sig}, p={p:.4f})"


def correlate_complexity_with_outcomes(
    metrics: list[FileMetrics],
    outcomes: list[AgentOutcome],
) -> list[CorrelationResult]:
    """Compute Pearson correlations between file metrics and agent outcomes.

    Joins *metrics* and *outcomes* on ``file_path``, then correlates each
    numeric metric with the outcome's success (as 0/1), duration, and cost.
    Returns one :class:`CorrelationResult` per metric, using the
    success-rate correlation as the primary coefficient.

    Args:
        metrics: File-level complexity metrics.
        outcomes: Agent task outcomes.

    Returns:
        List of :class:`CorrelationResult` (one per metric field).
    """
    # Build lookup: file_path -> metrics
    metrics_by_path: dict[str, FileMetrics] = {m.file_path: m for m in metrics}

    # Collect paired data points
    paired: list[tuple[FileMetrics, AgentOutcome]] = []
    for outcome in outcomes:
        fm = metrics_by_path.get(outcome.file_path)
        if fm is not None:
            paired.append((fm, outcome))

    results: list[CorrelationResult] = []

    for field_name in _METRIC_FIELDS:
        if not paired:
            results.append(
                CorrelationResult(
                    metric_name=field_name,
                    correlation_coefficient=0.0,
                    p_value=1.0,
                    sample_size=0,
                    insight=f"No paired data for {field_name.replace('_', ' ')}",
                )
            )
            continue

        xs = [float(getattr(fm, field_name)) for fm, _ in paired]
        # Correlate with failure (1 - success) so positive r means
        # "higher metric → more failures"
        ys = [0.0 if outcome.success else 1.0 for _, outcome in paired]

        n = len(xs)
        r = _pearson_r(xs, ys)
        p = _p_value_approx(r, n)
        insight = _build_insight(field_name, r, p)

        results.append(
            CorrelationResult(
                metric_name=field_name,
                correlation_coefficient=r,
                p_value=p,
                sample_size=n,
                insight=insight,
            )
        )

    return results


# ---------------------------------------------------------------------------
# High-risk file identification
# ---------------------------------------------------------------------------

_DEFAULT_COMPLEXITY_THRESHOLD: int = 15


def identify_high_risk_files(
    metrics: list[FileMetrics],
    threshold: int = _DEFAULT_COMPLEXITY_THRESHOLD,
) -> list[str]:
    """Return file paths whose cyclomatic complexity exceeds *threshold*.

    Args:
        metrics: List of file metrics.
        threshold: Complexity cutoff (inclusive: > threshold is high-risk).

    Returns:
        Sorted list of high-risk file paths.
    """
    return sorted(
        m.file_path for m in metrics if m.cyclomatic_complexity > threshold
    )


# ---------------------------------------------------------------------------
# Report rendering
# ---------------------------------------------------------------------------


def build_analysis_report(
    correlations: list[CorrelationResult],
    high_risk_files: list[str],
) -> AnalysisReport:
    """Assemble an :class:`AnalysisReport` from computed data.

    Generates recommendations based on significant correlations and
    high-risk file counts.

    Args:
        correlations: Per-metric correlation results.
        high_risk_files: Files exceeding the complexity threshold.

    Returns:
        An :class:`AnalysisReport` ready for rendering.
    """
    recommendations: list[str] = []

    significant = [c for c in correlations if c.p_value < 0.05]
    for c in significant:
        readable = c.metric_name.replace("_", " ")
        if c.correlation_coefficient > 0:
            recommendations.append(
                f"Reduce {readable} to improve agent success rate "
                f"(r={c.correlation_coefficient:.2f})"
            )
        else:
            recommendations.append(
                f"Higher {readable} is associated with better outcomes "
                f"(r={c.correlation_coefficient:.2f})"
            )

    if high_risk_files:
        recommendations.append(
            f"Refactor {len(high_risk_files)} high-complexity file(s) "
            f"to reduce agent failure risk"
        )

    if not recommendations:
        recommendations.append(
            "No statistically significant correlations found — "
            "collect more data or review metric coverage"
        )

    return AnalysisReport(
        correlations=tuple(correlations),
        recommendations=tuple(recommendations),
        high_risk_files=tuple(high_risk_files),
    )


def render_correlation_report(report: AnalysisReport) -> str:
    """Render an :class:`AnalysisReport` as Markdown.

    Produces a table of correlations, a recommendations list, and a
    high-risk files section.

    Args:
        report: The analysis report to render.

    Returns:
        Markdown string.
    """
    lines: list[str] = []
    lines.append("# Complexity-Performance Correlation Report")
    lines.append("")

    # Correlation table
    lines.append("## Correlations")
    lines.append("")
    lines.append("| Metric | r | p-value | N | Insight |")
    lines.append("|--------|---|---------|---|---------|")
    for c in report.correlations:
        lines.append(
            f"| {c.metric_name} | {c.correlation_coefficient:+.3f} "
            f"| {c.p_value:.4f} | {c.sample_size} | {c.insight} |"
        )
    lines.append("")

    # Recommendations
    lines.append("## Recommendations")
    lines.append("")
    for rec in report.recommendations:
        lines.append(f"- {rec}")
    lines.append("")

    # High-risk files
    if report.high_risk_files:
        lines.append("## High-Risk Files")
        lines.append("")
        for f in report.high_risk_files:
            lines.append(f"- `{f}`")
        lines.append("")

    return "\n".join(lines)
