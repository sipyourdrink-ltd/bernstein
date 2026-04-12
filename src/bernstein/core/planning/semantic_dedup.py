"""Semantic deduplication of similar tasks across plan stages.

Detects near-duplicate tasks using pure-Python text similarity (no external
dependencies).  Two similarity signals are combined:

1. Normalised token overlap (Jaccard index on lowercased word tokens).
2. Character trigram similarity (Jaccard index on character 3-grams).

A weighted average of both yields the final similarity score.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DuplicatePair:
    """A pair of tasks detected as near-duplicates.

    Attributes:
        task_a_title: Title of the first task.
        task_b_title: Title of the second task.
        similarity_score: Combined similarity in [0.0, 1.0].
        stage_a: Name of the stage containing the first task.
        stage_b: Name of the stage containing the second task.
    """

    task_a_title: str
    task_b_title: str
    similarity_score: float
    stage_a: str
    stage_b: str


@dataclass(frozen=True)
class DeduplicationResult:
    """Outcome of a deduplication scan over plan stages.

    Attributes:
        duplicates: Detected duplicate pairs.
        unique_count: Number of tasks that are *not* part of any duplicate pair.
        duplicate_count: Number of tasks that appear in at least one duplicate pair.
    """

    duplicates: tuple[DuplicatePair, ...]
    unique_count: int
    duplicate_count: int


# ---------------------------------------------------------------------------
# Text similarity helpers
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[a-z0-9]+")

# Weights for the two similarity signals.
_TOKEN_WEIGHT: float = 0.6
_TRIGRAM_WEIGHT: float = 0.4


def _tokenize(text: str) -> set[str]:
    """Lowercase and split *text* into word tokens.

    Args:
        text: Arbitrary text string.

    Returns:
        Set of lowercased alphanumeric tokens.
    """
    return set(_WORD_RE.findall(text.lower()))


def _trigrams(text: str) -> set[str]:
    """Produce character-level trigrams from *text*.

    Leading/trailing whitespace is stripped and the string is lowercased before
    trigram extraction.

    Args:
        text: Arbitrary text string.

    Returns:
        Set of 3-character substrings.
    """
    t = text.lower().strip()
    if len(t) < 3:
        return {t} if t else set()
    return {t[i : i + 3] for i in range(len(t) - 2)}


def _jaccard(set_a: set[str], set_b: set[str]) -> float:
    """Compute the Jaccard index of two sets.

    Args:
        set_a: First set.
        set_b: Second set.

    Returns:
        Jaccard similarity in [0.0, 1.0].  Returns 0.0 when both sets are
        empty (by convention).
    """
    if not set_a and not set_b:
        return 0.0
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return intersection / union


def compute_text_similarity(text_a: str, text_b: str) -> float:
    """Compute similarity between two text strings.

    Combines normalised token overlap (Jaccard on word tokens) with character
    trigram similarity (Jaccard on trigrams) using a weighted average.

    Args:
        text_a: First text.
        text_b: Second text.

    Returns:
        Similarity score in [0.0, 1.0].
    """
    token_sim = _jaccard(_tokenize(text_a), _tokenize(text_b))
    trigram_sim = _jaccard(_trigrams(text_a), _trigrams(text_b))
    return _TOKEN_WEIGHT * token_sim + _TRIGRAM_WEIGHT * trigram_sim


# ---------------------------------------------------------------------------
# Duplicate detection
# ---------------------------------------------------------------------------


def find_duplicate_tasks(
    tasks: list[dict[str, str]],
    threshold: float = 0.75,
) -> list[DuplicatePair]:
    """Find near-duplicate tasks via pairwise similarity comparison.

    Each task dict must contain at least ``"title"`` and ``"stage"`` keys.
    An optional ``"description"`` key is folded into the comparison text when
    present.

    Args:
        tasks: List of task dicts with ``"title"`` and ``"stage"`` keys.
        threshold: Minimum similarity score to consider a pair duplicates.

    Returns:
        List of :class:`DuplicatePair` instances whose similarity exceeds
        *threshold*, sorted by descending similarity.
    """
    pairs: list[DuplicatePair] = []
    n = len(tasks)
    for i in range(n):
        text_i = tasks[i]["title"]
        desc_i = tasks[i].get("description", "")
        if desc_i and desc_i != text_i:
            text_i = f"{text_i} {desc_i}"
        for j in range(i + 1, n):
            text_j = tasks[j]["title"]
            desc_j = tasks[j].get("description", "")
            if desc_j and desc_j != text_j:
                text_j = f"{text_j} {desc_j}"
            score = compute_text_similarity(text_i, text_j)
            if score >= threshold:
                pairs.append(
                    DuplicatePair(
                        task_a_title=tasks[i]["title"],
                        task_b_title=tasks[j]["title"],
                        similarity_score=round(score, 4),
                        stage_a=tasks[i]["stage"],
                        stage_b=tasks[j]["stage"],
                    )
                )
    return sorted(pairs, key=lambda p: p.similarity_score, reverse=True)


def deduplicate_plan(stages: list[dict[str, Any]], threshold: float = 0.75) -> DeduplicationResult:
    """Scan plan stages for duplicate tasks and return a summary.

    Accepts the ``stages`` list in the same shape used by Bernstein plan YAML
    files (each stage is a dict with ``"name"`` and ``"steps"``; each step has
    a ``"title"`` or ``"goal"`` key and an optional ``"description"``).

    Args:
        stages: List of stage dicts from a parsed plan.
        threshold: Minimum similarity to flag a pair as duplicate.

    Returns:
        A :class:`DeduplicationResult` summarising detected duplicates.
    """
    # Flatten stages into a task list
    flat: list[dict[str, str]] = []
    for stage in stages:
        stage_name = str(stage.get("name", ""))
        raw_steps: list[dict[str, Any]] = list(stage.get("steps") or [])
        for step in raw_steps:
            title = str(step.get("title") or step.get("goal", ""))
            if not title:
                continue
            description = str(step.get("description", ""))
            flat.append({"title": title, "stage": stage_name, "description": description})

    pairs = find_duplicate_tasks(flat, threshold=threshold)

    # Count how many unique titles appear in at least one duplicate pair
    dup_titles: set[str] = set()
    for pair in pairs:
        dup_titles.add(pair.task_a_title)
        dup_titles.add(pair.task_b_title)

    all_titles = {t["title"] for t in flat}
    duplicate_count = len(dup_titles)
    unique_count = len(all_titles) - duplicate_count

    return DeduplicationResult(
        duplicates=tuple(pairs),
        unique_count=unique_count,
        duplicate_count=duplicate_count,
    )


# ---------------------------------------------------------------------------
# Merge suggestion
# ---------------------------------------------------------------------------


def suggest_merge(pair: DuplicatePair) -> str:
    """Suggest which task to keep from a duplicate pair.

    The heuristic keeps the task with the longer title, as a proxy for
    specificity.  When lengths are equal, task A is preferred.

    Args:
        pair: A detected duplicate pair.

    Returns:
        Human-readable merge suggestion string.
    """
    if len(pair.task_b_title) > len(pair.task_a_title):
        keep, drop = pair.task_b_title, pair.task_a_title
        keep_stage, drop_stage = pair.stage_b, pair.stage_a
    else:
        keep, drop = pair.task_a_title, pair.task_b_title
        keep_stage, drop_stage = pair.stage_a, pair.stage_b

    return (
        f"Keep '{keep}' (stage '{keep_stage}') and remove "
        f"'{drop}' (stage '{drop_stage}') — "
        f"similarity {pair.similarity_score:.0%}"
    )
