"""Tests for task difficulty estimator."""

from __future__ import annotations

from bernstein.core.difficulty_estimator import estimate_difficulty, minutes_for_level


def test_estimate_difficulty_trivial() -> None:
    """Test trivial task."""
    desc = "Fix typo."
    score = estimate_difficulty(desc)
    assert score.level == "trivial"
    assert minutes_for_level(score.level) == 10


def test_estimate_difficulty_low() -> None:
    """Test low difficulty task."""
    # word count 50 -> 1.0. code refs: 1 -> total 2.0 -> low
    desc = "This is a slightly longer description that just explains the task. " * 5 + " `code` "
    score = estimate_difficulty(desc)
    assert score.level == "low"
    assert minutes_for_level(score.level) == 20


def test_estimate_difficulty_medium() -> None:
    """Test medium difficulty task."""
    # Simpler description to get medium score
    desc = "Refactor the database logic. `code_here`"
    # word count: 6 -> 0.12. code refs: 1. keywords: refactor, database = 4
    # score ~ 5.12 -> medium
    score = estimate_difficulty(desc)
    assert score.level == "medium"
    assert minutes_for_level(score.level) == 45


def test_estimate_difficulty_high() -> None:
    """Test high difficulty task."""
    # 15 backticks
    desc = "`1` `2` `3` `4` `5` `6` `7` `8` `9` `10` `11` `12` `13` `14` `15`"
    score = estimate_difficulty(desc)
    assert score.level == "high"
    assert minutes_for_level(score.level) == 90


def test_estimate_difficulty_critical() -> None:
    """Test critical difficulty task."""
    # keywords: refactor, architect, security, database, migrate = 5 * 2 = 10
    # 15 func calls = 15
    desc = "refactor architect security database migrate. " + "func() " * 15
    score = estimate_difficulty(desc)
    assert score.level == "critical"
    assert minutes_for_level(score.level) == 120
