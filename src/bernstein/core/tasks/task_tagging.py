"""Task tagging and filtering for selective re-runs.

Supports adding tags to tasks and filtering by tag for selective
execution.  Tags can be assigned in plan YAML, via the API, or
programmatically.

Usage::

    from bernstein.core.task_tagging import TaskTagger

    tagger = TaskTagger()
    tagger.add_tags("task-1", ["backend", "api"])
    backend_tasks = tagger.filter_by_tag(tasks, "backend")
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Sequence

    from bernstein.core.models import Task

logger = logging.getLogger(__name__)

_TAG_RE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")


class InvalidTagError(ValueError):
    """Raised when a tag string is invalid."""


def validate_tag(tag: str) -> str:
    """Validate and normalise a tag string.

    Tags must be alphanumeric with hyphens or underscores, starting
    with an alphanumeric character.  They are stored lowercase.

    Args:
        tag: Raw tag string.

    Returns:
        Normalised lowercase tag.

    Raises:
        InvalidTagError: If the tag format is invalid.
    """
    normalised = tag.strip().lower()
    if not normalised:
        raise InvalidTagError("Tag cannot be empty")
    if not _TAG_RE.match(normalised):
        raise InvalidTagError(
            f"Invalid tag {tag!r}: must be alphanumeric with hyphens/underscores, "
            "starting with an alphanumeric character"
        )
    return normalised


@dataclass
class TagSet:
    """Tag collection for a single task.

    Attributes:
        task_id: The task identifier.
        tags: Set of normalised tag strings.
    """

    task_id: str
    tags: set[str] = field(default_factory=set[str])


class TaskTagger:
    """Manages tags for tasks and provides filtering capabilities."""

    def __init__(self) -> None:
        self._tag_sets: dict[str, TagSet] = {}

    def add_tags(self, task_id: str, tags: Sequence[str]) -> set[str]:
        """Add tags to a task.

        Args:
            task_id: Task identifier.
            tags: Tags to add.

        Returns:
            The full set of tags after addition.

        Raises:
            InvalidTagError: If any tag is invalid.
        """
        normalised = {validate_tag(t) for t in tags}
        if task_id not in self._tag_sets:
            self._tag_sets[task_id] = TagSet(task_id=task_id)
        self._tag_sets[task_id].tags.update(normalised)
        return set(self._tag_sets[task_id].tags)

    def remove_tags(self, task_id: str, tags: Sequence[str]) -> set[str]:
        """Remove tags from a task.

        Args:
            task_id: Task identifier.
            tags: Tags to remove.

        Returns:
            The full set of tags after removal.
        """
        normalised = {validate_tag(t) for t in tags}
        tag_set = self._tag_sets.get(task_id)
        if tag_set is None:
            return set()
        tag_set.tags -= normalised
        return set(tag_set.tags)

    def get_tags(self, task_id: str) -> set[str]:
        """Get all tags for a task.

        Args:
            task_id: Task identifier.

        Returns:
            Set of tag strings (empty if task has no tags).
        """
        tag_set = self._tag_sets.get(task_id)
        return set(tag_set.tags) if tag_set else set()

    def has_tag(self, task_id: str, tag: str) -> bool:
        """Check if a task has a specific tag.

        Args:
            task_id: Task identifier.
            tag: Tag to check (normalised automatically).

        Returns:
            True if the task has the tag.
        """
        normalised = validate_tag(tag)
        tag_set = self._tag_sets.get(task_id)
        return normalised in tag_set.tags if tag_set else False

    def filter_by_tag(
        self,
        tasks: Sequence[Task],
        tag: str,
    ) -> list[Task]:
        """Filter tasks to those that have a specific tag.

        Args:
            tasks: Tasks to filter.
            tag: Tag to filter by (normalised automatically).

        Returns:
            List of tasks that have the specified tag.
        """
        normalised = validate_tag(tag)
        return [t for t in tasks if self.has_tag(t.id, normalised)]

    def filter_by_any_tag(
        self,
        tasks: Sequence[Task],
        tags: Sequence[str],
    ) -> list[Task]:
        """Filter tasks to those that have any of the specified tags.

        Args:
            tasks: Tasks to filter.
            tags: Tags to match (OR logic).

        Returns:
            List of tasks that have at least one of the specified tags.
        """
        normalised = {validate_tag(t) for t in tags}
        return [t for t in tasks if normalised & self.get_tags(t.id)]

    def filter_by_all_tags(
        self,
        tasks: Sequence[Task],
        tags: Sequence[str],
    ) -> list[Task]:
        """Filter tasks to those that have all of the specified tags.

        Args:
            tasks: Tasks to filter.
            tags: Tags to match (AND logic).

        Returns:
            List of tasks that have all of the specified tags.
        """
        normalised = {validate_tag(t) for t in tags}
        return [t for t in tasks if normalised <= self.get_tags(t.id)]

    def all_tags(self) -> set[str]:
        """Return all unique tags across all tasks.

        Returns:
            Set of all tag strings.
        """
        result: set[str] = set()
        for ts in self._tag_sets.values():
            result.update(ts.tags)
        return result

    def tasks_with_tag(self, tag: str) -> list[str]:
        """Return task IDs that have a specific tag.

        Args:
            tag: Tag to search for.

        Returns:
            List of task IDs.
        """
        normalised = validate_tag(tag)
        return [ts.task_id for ts in self._tag_sets.values() if normalised in ts.tags]

    def tag_counts(self) -> dict[str, int]:
        """Return a count of how many tasks have each tag.

        Returns:
            Dict mapping tag -> count.
        """
        counts: dict[str, int] = {}
        for ts in self._tag_sets.values():
            for tag in ts.tags:
                counts[tag] = counts.get(tag, 0) + 1
        return counts
