"""Detect and merge duplicate tasks using text similarity."""

from __future__ import annotations

import re
from collections import defaultdict
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from bernstein.core.models import Task


def normalize_text(text: str) -> str:
    """Normalize text for comparison."""
    text = text.lower()
    text = re.sub(r"[^\w\s]", "", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def compute_word_overlap(text1: str, text2: str) -> float:
    """Compute word overlap similarity between two texts."""
    words1 = set(normalize_text(text1).split())
    words2 = set(normalize_text(text2).split())

    if not words1 or not words2:
        return 0.0

    intersection = words1 & words2
    union = words1 | words2

    return len(intersection) / len(union) if union else 0.0


def detect_duplicates(
    tasks: list[Task],
    threshold: float = 0.7,
) -> list[tuple[str, str, float]]:
    """Detect duplicate tasks based on title and description similarity.

    Args:
        tasks: List of tasks to check for duplicates.
        threshold: Similarity threshold (0.0-1.0) for considering tasks duplicates.

    Returns:
        List of (task_id_1, task_id_2, similarity_score) tuples for duplicate pairs.
    """
    duplicates: list[tuple[str, str, float]] = []

    # Group by role first (only compare within same role)
    by_role: dict[str, list[Task]] = defaultdict(list)
    for task in tasks:
        if task.status.value in ("open", "planned"):
            by_role[task.role].append(task)

    # Compare tasks within each role
    for role_tasks in by_role.values():
        for i, task1 in enumerate(role_tasks):
            for task2 in role_tasks[i + 1 :]:
                # Compare titles
                title_sim = compute_word_overlap(task1.title, task2.title)

                # Compare descriptions
                desc_sim = compute_word_overlap(task1.description, task2.description)

                # Weighted average (title more important)
                similarity = (title_sim * 0.6) + (desc_sim * 0.4)

                if similarity >= threshold:
                    duplicates.append((task1.id, task2.id, round(similarity, 3)))

    # Sort by similarity descending
    duplicates.sort(key=lambda x: x[2], reverse=True)
    return duplicates


def merge_duplicate_tasks(
    task1: Task,
    task2: Task,
    _similarity_score: float,
) -> Task:
    """Merge two duplicate tasks into one.

    Keeps the higher priority task, combines descriptions and completion signals.

    Args:
        task1: First task (typically higher priority).
        task2: Second task (will be merged into first).
        similarity_score: Similarity score between the tasks.

    Returns:
        Merged task.
    """
    # Keep higher priority (lower number)
    primary = task1 if task1.priority <= task2.priority else task2
    secondary = task2 if task1.priority <= task2.priority else task1

    # Combine descriptions
    combined_desc = primary.description
    if secondary.description not in primary.description:
        combined_desc += f"\n\n---\n\nAlso from duplicate task:\n{secondary.description}"

    # Combine completion signals
    combined_signals = list(primary.completion_signals)
    for sig in secondary.completion_signals:
        if sig not in primary.completion_signals:
            combined_signals.append(sig)

    # Combine owned files
    combined_files = list(primary.owned_files)
    for f in secondary.owned_files:
        if f not in primary.owned_files:
            combined_files.append(f)

    # Return updated primary task
    # Note: In practice, you'd update via the task server
    from bernstein.core.models import Task

    return Task(
        id=primary.id,
        title=primary.title,
        description=combined_desc,
        role=primary.role,
        priority=min(primary.priority, secondary.priority),
        scope=primary.scope,
        complexity=primary.complexity,
        status=primary.status,
        task_type=primary.task_type,
        completion_signals=combined_signals,
        owned_files=combined_files,
        depends_on=list(set(primary.depends_on + secondary.depends_on)),
    )
