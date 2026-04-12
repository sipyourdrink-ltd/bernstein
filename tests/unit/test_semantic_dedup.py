"""Tests for semantic task deduplication (planning/semantic_dedup.py)."""

from __future__ import annotations

from bernstein.core.planning.semantic_dedup import (
    DeduplicationResult,
    DuplicatePair,
    compute_text_similarity,
    deduplicate_plan,
    find_duplicate_tasks,
    suggest_merge,
)

# ---------------------------------------------------------------------------
# compute_text_similarity
# ---------------------------------------------------------------------------


def test_identical_strings_have_perfect_similarity() -> None:
    """Identical strings yield a score of 1.0."""
    assert compute_text_similarity("add unit tests", "add unit tests") == 1.0


def test_empty_strings_have_zero_similarity() -> None:
    """Two empty strings yield 0.0."""
    assert compute_text_similarity("", "") == 0.0


def test_completely_different_strings_are_low() -> None:
    """Unrelated strings produce a very low score."""
    score = compute_text_similarity("implement REST API", "fix CSS animation")
    assert score < 0.3


def test_similar_rephrasing_scores_high() -> None:
    """Two phrasings of the same idea should score above 0.5."""
    score = compute_text_similarity(
        "add logging to HTTP server",
        "add HTTP server logging",
    )
    assert score > 0.5


def test_case_insensitive() -> None:
    """Similarity is case-insensitive."""
    assert compute_text_similarity("Add Tests", "add tests") == 1.0


# ---------------------------------------------------------------------------
# DuplicatePair / DeduplicationResult frozen guarantees
# ---------------------------------------------------------------------------


def test_duplicate_pair_is_frozen() -> None:
    """DuplicatePair should be immutable."""
    pair = DuplicatePair(
        task_a_title="a",
        task_b_title="b",
        similarity_score=0.9,
        stage_a="s1",
        stage_b="s2",
    )
    try:
        pair.similarity_score = 0.1  # type: ignore[misc]
        raise AssertionError("Expected FrozenInstanceError")  # pragma: no cover
    except AttributeError:
        pass  # expected


def test_deduplication_result_is_frozen() -> None:
    """DeduplicationResult should be immutable."""
    result = DeduplicationResult(duplicates=(), unique_count=1, duplicate_count=0)
    try:
        result.unique_count = 99  # type: ignore[misc]
        raise AssertionError("Expected FrozenInstanceError")  # pragma: no cover
    except AttributeError:
        pass  # expected


# ---------------------------------------------------------------------------
# find_duplicate_tasks
# ---------------------------------------------------------------------------


def test_find_duplicates_identical_tasks() -> None:
    """Two identical tasks must always be detected."""
    tasks = [
        {"title": "write unit tests for auth", "stage": "testing"},
        {"title": "write unit tests for auth", "stage": "qa"},
    ]
    pairs = find_duplicate_tasks(tasks, threshold=0.75)
    assert len(pairs) == 1
    assert pairs[0].similarity_score == 1.0
    assert pairs[0].stage_a == "testing"
    assert pairs[0].stage_b == "qa"


def test_find_duplicates_similar_rephrasing() -> None:
    """Rephrased tasks with high overlap should be flagged."""
    tasks = [
        {"title": "add logging to the HTTP server", "stage": "backend"},
        {"title": "add HTTP server logging", "stage": "observability"},
    ]
    pairs = find_duplicate_tasks(tasks, threshold=0.5)
    assert len(pairs) >= 1
    assert pairs[0].similarity_score >= 0.5


def test_find_duplicates_dissimilar_tasks() -> None:
    """Completely different tasks should produce no pairs."""
    tasks = [
        {"title": "implement database migrations", "stage": "backend"},
        {"title": "design landing page mockup", "stage": "frontend"},
    ]
    pairs = find_duplicate_tasks(tasks, threshold=0.75)
    assert len(pairs) == 0


def test_find_duplicates_respects_threshold() -> None:
    """Raising the threshold should filter out borderline pairs."""
    tasks = [
        {"title": "add user authentication", "stage": "auth"},
        {"title": "implement user auth flow", "stage": "auth"},
    ]
    loose = find_duplicate_tasks(tasks, threshold=0.3)
    strict = find_duplicate_tasks(tasks, threshold=0.95)
    assert len(loose) >= len(strict)


def test_find_duplicates_sorted_by_score() -> None:
    """Results are sorted by descending similarity."""
    tasks = [
        {"title": "write integration tests", "stage": "qa"},
        {"title": "write integration tests", "stage": "ci"},
        {"title": "add integration test suite", "stage": "testing"},
    ]
    pairs = find_duplicate_tasks(tasks, threshold=0.4)
    scores = [p.similarity_score for p in pairs]
    assert scores == sorted(scores, reverse=True)


def test_find_duplicates_uses_description() -> None:
    """Description text should be incorporated when present."""
    tasks = [
        {"title": "setup", "stage": "init", "description": "install all project dependencies"},
        {"title": "setup", "stage": "bootstrap", "description": "install all project dependencies"},
    ]
    pairs = find_duplicate_tasks(tasks, threshold=0.75)
    assert len(pairs) == 1


# ---------------------------------------------------------------------------
# deduplicate_plan — cross-stage detection
# ---------------------------------------------------------------------------


def test_deduplicate_plan_cross_stage() -> None:
    """Duplicates across different stages are detected."""
    stages = [
        {
            "name": "backend",
            "steps": [{"title": "implement REST endpoint for users"}],
        },
        {
            "name": "api",
            "steps": [{"title": "implement REST endpoint for users"}],
        },
    ]
    result = deduplicate_plan(stages, threshold=0.75)
    assert isinstance(result, DeduplicationResult)
    assert len(result.duplicates) == 1
    assert result.duplicate_count == 1  # same title counted once
    assert result.duplicates[0].stage_a == "backend"
    assert result.duplicates[0].stage_b == "api"


def test_deduplicate_plan_no_duplicates() -> None:
    """A plan with entirely distinct tasks has no duplicates."""
    stages = [
        {
            "name": "db",
            "steps": [{"title": "design database schema"}],
        },
        {
            "name": "ui",
            "steps": [{"title": "build React dashboard"}],
        },
    ]
    result = deduplicate_plan(stages)
    assert len(result.duplicates) == 0
    assert result.unique_count == 2
    assert result.duplicate_count == 0


def test_deduplicate_plan_uses_goal_key() -> None:
    """Steps with 'goal' instead of 'title' are also checked."""
    stages = [
        {
            "name": "s1",
            "steps": [{"goal": "add error handling"}],
        },
        {
            "name": "s2",
            "steps": [{"goal": "add error handling"}],
        },
    ]
    result = deduplicate_plan(stages, threshold=0.75)
    assert len(result.duplicates) == 1


def test_deduplicate_plan_within_same_stage() -> None:
    """Duplicates within a single stage are also detected."""
    stages = [
        {
            "name": "init",
            "steps": [
                {"title": "set up linting rules"},
                {"title": "set up linting rules"},
            ],
        },
    ]
    result = deduplicate_plan(stages, threshold=0.75)
    assert len(result.duplicates) == 1
    assert result.duplicates[0].stage_a == "init"
    assert result.duplicates[0].stage_b == "init"


# ---------------------------------------------------------------------------
# suggest_merge
# ---------------------------------------------------------------------------


def test_suggest_merge_keeps_longer_title() -> None:
    """The more specific (longer) title is recommended for keeping."""
    pair = DuplicatePair(
        task_a_title="add tests",
        task_b_title="add comprehensive unit tests for auth module",
        similarity_score=0.82,
        stage_a="qa",
        stage_b="testing",
    )
    suggestion = suggest_merge(pair)
    assert "add comprehensive unit tests for auth module" in suggestion
    assert "Keep" in suggestion
    assert "remove" in suggestion


def test_suggest_merge_equal_length_keeps_a() -> None:
    """When lengths are equal, task A is preferred."""
    pair = DuplicatePair(
        task_a_title="add auth",
        task_b_title="add auth",
        similarity_score=1.0,
        stage_a="s1",
        stage_b="s2",
    )
    suggestion = suggest_merge(pair)
    assert "stage 's1'" in suggestion
    assert "Keep" in suggestion


def test_suggest_merge_contains_similarity_pct() -> None:
    """The suggestion includes the similarity percentage."""
    pair = DuplicatePair(
        task_a_title="x",
        task_b_title="y long title",
        similarity_score=0.85,
        stage_a="a",
        stage_b="b",
    )
    suggestion = suggest_merge(pair)
    assert "85%" in suggestion
