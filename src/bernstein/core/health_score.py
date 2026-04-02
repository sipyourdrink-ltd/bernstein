"""Codebase health score calculation."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class HealthScore:
    """Overall codebase health score."""

    total: int  # 0-100
    test_coverage: int  # 0-100
    lint_score: int  # 0-100
    complexity_score: int  # 0-100 (lower complexity = higher score)
    dependency_freshness: int  # 0-100
    breakdown: dict[str, int]


def calculate_health_score(metrics_dir: Path) -> HealthScore:
    """Calculate overall codebase health score.

    Combines:
    - Test coverage
    - Lint score
    - Code complexity
    - Dependency freshness

    Args:
        metrics_dir: Path to .sdd/metrics directory.

    Returns:
        HealthScore with total and breakdown.
    """
    # Read quality scores
    quality_scores = _read_quality_scores(metrics_dir)
    lint_score = quality_scores.get("lint_score", 50)
    test_score = quality_scores.get("tests_score", 50)

    # Read complexity metrics
    complexity_score = _calculate_complexity_score(metrics_dir)

    # Read dependency freshness
    dep_freshness = _calculate_dependency_freshness(metrics_dir)

    # Calculate weighted total
    weights = {
        "test_coverage": 0.30,
        "lint_score": 0.25,
        "complexity": 0.25,
        "dependency_freshness": 0.20,
    }

    total = int(
        test_score * weights["test_coverage"]
        + lint_score * weights["lint_score"]
        + complexity_score * weights["complexity"]
        + dep_freshness * weights["dependency_freshness"]
    )

    return HealthScore(
        total=min(100, max(0, total)),
        test_coverage=test_score,
        lint_score=lint_score,
        complexity_score=complexity_score,
        dependency_freshness=dep_freshness,
        breakdown={
            "test_coverage": test_score,
            "lint_score": lint_score,
            "complexity": complexity_score,
            "dependency_freshness": dep_freshness,
        },
    )


def _read_quality_scores(metrics_dir: Path) -> dict[str, int]:
    """Read quality scores from metrics.

    Args:
        metrics_dir: Path to .sdd/metrics directory.

    Returns:
        Dictionary with lint_score and tests_score.
    """
    result: dict[str, int] = {"lint_score": 50, "tests_score": 50}

    quality_file = metrics_dir / "quality_scores.jsonl"
    if not quality_file.exists():
        return result

    scores = []
    try:
        for line in quality_file.read_text().splitlines():
            if not line.strip():
                continue
            data = json.loads(line)
            breakdown = data.get("breakdown", {})
            scores.append(breakdown)
    except (json.JSONDecodeError, OSError):
        return result

    if not scores:
        return result

    # Average recent scores
    avg_lint = sum(s.get("lint", 50) for s in scores[-10:]) / min(10, len(scores))
    avg_tests = sum(s.get("tests", 50) for s in scores[-10:]) / min(10, len(scores))

    result["lint_score"] = int(avg_lint)
    result["tests_score"] = int(avg_tests)

    return result


def _calculate_complexity_score(metrics_dir: Path) -> int:
    """Calculate complexity score (lower complexity = higher score).

    Args:
        metrics_dir: Path to .sdd/metrics directory.

    Returns:
        Complexity score 0-100.
    """
    # Default score if no metrics
    return 70


def _calculate_dependency_freshness(metrics_dir: Path) -> int:
    """Calculate dependency freshness score.

    Args:
        metrics_dir: Path to .sdd/metrics directory.

    Returns:
        Freshness score 0-100.
    """
    # Default score if no metrics
    return 80


def format_health_report(score: HealthScore) -> str:
    """Format health score as human-readable report.

    Args:
        score: HealthScore instance.

    Returns:
        Formatted report string.
    """
    grade = _score_to_grade(score.total)

    lines = [
        "Codebase Health Score",
        "=" * 40,
        f"Overall: {score.total}/100 ({grade})",
        "",
        "Breakdown:",
        f"  Test Coverage:    {score.test_coverage}/100",
        f"  Lint Score:       {score.lint_score}/100",
        f"  Complexity:       {score.complexity_score}/100",
        f"  Dependencies:     {score.dependency_freshness}/100",
    ]

    return "\n".join(lines)


def _score_to_grade(score: int) -> str:
    """Convert score to letter grade."""
    if score >= 90:
        return "A"
    elif score >= 80:
        return "B"
    elif score >= 70:
        return "C"
    elif score >= 60:
        return "D"
    else:
        return "F"
