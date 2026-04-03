"""Agent lesson propagation system.

Agents file lessons when they complete tasks. New agents receive relevant
lessons by tag overlap. Lessons are stored in .sdd/memory/lessons.jsonl and
decay over time.

Lessons are canonical — deduplication prevents storing the same lesson twice.
Each lesson is immutable once filed, but can be updated with higher confidence
if the same lesson appears again from a different source.

Concurrent access is protected by the **memory lock protocol** (:mod:`memory_lock_protocol`):
PID/mtime-based locks with stale lock recovery, and atomic writes with backup/rollback.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import asdict, dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.memory_integrity import (
    build_entry_integrity,
    detect_memory_poisoning,
)
from bernstein.core.memory_lock_protocol import MemoryFileGuard, guarded_memory_write

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# Lessons older than this many days have reduced relevance
_DECAY_DAYS = 30
_DECAY_FACTOR = 0.7  # 70% of original confidence after DECAY_DAYS

# Similarity threshold for deduplicating lessons (Jaccard on tags + keyword match)
_DEDUP_THRESHOLD = 0.75

# Staleness threshold: lessons older than this get a caveat (T652)
_STALENESS_DAYS = 1

# Truncation budget: max characters for lesson context (T654)
# ~4000 chars ≈ ~1000 tokens, leaving room for other context sections.
_MAX_LESSON_CHARS = 4000
_TRUNCATION_WARNING = "\n\n---\n**Note:** Some lessons were omitted due to context window limits."


def compute_lesson_staleness(created_timestamp: float, now: float | None = None) -> float:
    """Return the age of a lesson in days (T652).

    Args:
        created_timestamp: Unix timestamp when the lesson was filed.
        now: Override for current time (useful for testing).

    Returns:
        Age in days.
    """
    reference = now if now is not None else time.time()
    return (reference - created_timestamp) / 86400


def is_lesson_stale(created_timestamp: float, now: float | None = None) -> bool:
    """Return True if a lesson is older than the staleness threshold (T652).

    Args:
        created_timestamp: Unix timestamp when the lesson was filed.
        now: Override for current time (useful for testing).

    Returns:
        True when the lesson age exceeds ``_STALENESS_DAYS``.
    """
    return compute_lesson_staleness(created_timestamp, now) > _STALENESS_DAYS


@dataclass(frozen=True)
class Lesson:
    """A single lesson filed by an agent on task completion.

    Attributes:
        lesson_id: Unique identifier for this lesson (UUID).
        tags: List of tags for retrieval (e.g., "auth", "database", "testing").
        content: The actual lesson text (what did you learn?).
        confidence: Score 0-1 indicating how confident in this lesson.
        created_timestamp: Unix timestamp when first filed.
        filed_by_agent: Agent ID that filed this lesson.
        task_id: Task ID that generated this lesson.
        version: Version counter; incremented when confidence is updated.
    """

    lesson_id: str
    tags: list[str]
    content: str
    confidence: float
    created_timestamp: float
    filed_by_agent: str
    task_id: str
    version: int = 1
    # Integrity fields (OWASP ASI06 2026 — Memory Provenance & Integrity)
    content_hash: str | None = None  # SHA-256 of immutable fields
    prev_hash: str | None = None  # chain_hash of predecessor entry
    chain_hash: str | None = None  # SHA-256 of (content_hash + prev_hash)


def file_lesson(
    sdd_dir: Path,
    task_id: str,
    agent_id: str,
    content: str,
    tags: list[str],
    confidence: float = 0.8,
) -> str:
    """File a lesson when an agent completes a task.

    If an identical lesson already exists (same tags + content), increments
    its confidence instead of creating a duplicate. Otherwise creates a new
    lesson and appends it to lessons.jsonl.

    Uses the memory lock protocol (PID/mtime lock + atomic write with backup)
    for the read-check-write sequence to prevent race conditions when
    concurrent agents file lessons simultaneously.

    Args:
        sdd_dir: Path to .sdd directory.
        task_id: Task ID that generated this lesson.
        agent_id: Agent ID that filed this lesson.
        content: The lesson text.
        tags: Tags for retrieval (e.g., ["auth", "security"]).
        confidence: Confidence score 0-1 (default 0.8).

    Returns:
        The lesson_id (string UUID) of the filed or updated lesson.
    """
    sdd_dir.mkdir(parents=True, exist_ok=True)
    memory_dir = sdd_dir / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    lessons_path = memory_dir / "lessons.jsonl"

    # Normalize inputs
    tags_lower = sorted(set(t.lower().strip() for t in tags if t.strip()))
    confidence_clamped = max(0.0, min(1.0, confidence))
    now = time.time()

    # --- Memory poisoning check (OWASP ASI06 2026) ---
    poison = detect_memory_poisoning(content, tags_lower, confidence_clamped)
    if poison.is_suspicious:
        logger.warning(
            "Rejected lesson from agent %s (task %s): %s",
            agent_id,
            task_id,
            poison.reason,
        )
        raise ValueError(f"Lesson rejected — {poison.reason}")

    # Use lock protocol for read-check-write (dedup or update decision)
    with guarded_memory_write(lessons_path) as guard:
        existing_lesson_id = _find_similar_lesson_in_content(guard.original_content, tags_lower, content)
        if existing_lesson_id:
            # Update existing lesson's confidence and version
            _update_lesson_confidence_from_content(lessons_path, existing_lesson_id, confidence_clamped, guard)
            return existing_lesson_id

        # Create new lesson
        lesson_id = str(uuid.uuid4())
        lesson = Lesson(
            lesson_id=lesson_id,
            tags=tags_lower,
            content=content,
            confidence=confidence_clamped,
            created_timestamp=now,
            filed_by_agent=agent_id,
            task_id=task_id,
            version=1,
        )

        # Compute integrity fields (content hash + chain hash)
        lesson_dict = asdict(lesson)
        prev_chain_hash = _get_last_chain_hash_from_content(guard.original_content)
        integrity = build_entry_integrity(lesson_dict, prev_chain_hash)
        lesson_dict.update(integrity.as_dict())

        # Build new content (append to existing or create new)
        existing_text = guard.original_content or ""
        new_line = json.dumps(lesson_dict) + "\n"
        new_content = existing_text + new_line if existing_text else new_line

        if existing_text:
            guard.write_backup()
        guard.write_new(new_content)

        logger.info(
            "Filed lesson %s from agent %s on task %s with tags %s (content_hash=%s…)",
            lesson_id,
            agent_id,
            task_id,
            tags_lower,
            integrity.content_hash[:12],
        )

    return lesson_id


def get_lessons_for_agent(
    sdd_dir: Path,
    task_tags: list[str],
    limit: int = 5,
) -> list[Lesson]:
    """Retrieve lessons relevant to an agent about to start a task.

    Matches by tag overlap. Lessons with higher confidence and lower age
    (accounting for decay) rank higher. Applies decay to confidence scores
    based on age.

    Args:
        sdd_dir: Path to .sdd directory.
        task_tags: Tags describing the task the agent will work on.
        limit: Maximum number of lessons to return.

    Returns:
        List of lessons ranked by relevance, up to *limit* in length.
    """
    lessons_path = sdd_dir / "memory" / "lessons.jsonl"
    if not lessons_path.exists():
        return []

    task_tags_lower = set(t.lower().strip() for t in task_tags if t.strip())
    if not task_tags_lower:
        return []

    lessons_with_score: list[tuple[Lesson, float]] = []
    now = time.time()

    try:
        with open(lessons_path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    lesson = _parse_lesson(data)
                    if lesson is None:
                        continue

                    # Compute tag overlap
                    lesson_tags = set(lesson.tags)
                    overlap = len(task_tags_lower & lesson_tags)
                    if overlap == 0:
                        continue  # No tag match

                    # Apply decay and recompute confidence
                    age_days = (now - lesson.created_timestamp) / (24 * 3600)
                    if age_days > _DECAY_DAYS:
                        decay = _DECAY_FACTOR ** (age_days / _DECAY_DAYS)
                        decayed_confidence = lesson.confidence * decay
                    else:
                        decayed_confidence = lesson.confidence

                    # Score: overlap + decayed confidence
                    relevance = overlap + decayed_confidence

                    lesson_with_decay = Lesson(
                        lesson_id=lesson.lesson_id,
                        tags=lesson.tags,
                        content=lesson.content,
                        confidence=decayed_confidence,
                        created_timestamp=lesson.created_timestamp,
                        filed_by_agent=lesson.filed_by_agent,
                        task_id=lesson.task_id,
                        version=lesson.version,
                    )
                    lessons_with_score.append((lesson_with_decay, relevance))
                except (json.JSONDecodeError, TypeError, KeyError) as e:
                    logger.debug("Skipped malformed lesson in JSONL: %s", e)
                    continue

    except OSError as e:
        logger.warning("Failed to read lessons from %s: %s", lessons_path, e)
        return []

    # Sort by relevance score (descending) and return top-N
    lessons_with_score.sort(key=lambda x: x[1], reverse=True)
    return [lesson for lesson, _ in lessons_with_score[:limit]]


def gather_lessons_for_context(
    sdd_dir: Path,
    task_tags: list[str],
    now: float | None = None,
    max_chars: int = _MAX_LESSON_CHARS,
) -> str:
    """Format lessons into a string for injection into agent context.

    Retrieves relevant lessons and formats them as a markdown section.
    Lessons older than ``_STALENESS_DAYS`` receive a staleness caveat.
    Output is truncated at *max_chars* with a truncation warning appended
    when the budget is exceeded (T654).

    Args:
        sdd_dir: Path to .sdd directory.
        task_tags: Tags describing the task.
        now: Override for current time (useful for testing).
        max_chars: Maximum output length before truncation occurs.

    Returns:
        Markdown-formatted string of lessons, or empty string if none found.
    """
    lessons = get_lessons_for_agent(sdd_dir, task_tags)
    if not lessons:
        return ""

    reference = now if now is not None else time.time()
    lines = ["## Prior Agent Lessons", ""]
    for lesson in lessons:
        lesson_block = _format_lesson_block(lesson, reference)
        # Check if adding this lesson would exceed the budget
        candidate = "\n".join([*lines, lesson_block])
        if len(candidate) > max_chars:
            # Skip this lesson — if any lessons remain, append truncation notice
            return "\n".join(lines) + _TRUNCATION_WARNING
        lines.append(lesson_block)

    return "\n".join(lines)


def _format_lesson_block(lesson: Lesson, now: float) -> str:
    """Format a single lesson as a markdown block (T652, T654).

    Args:
        lesson: Lesson instance to format.
        now: Current time reference for staleness computation.

    Returns:
        Formatted markdown string for the lesson.
    """
    stale = is_lesson_stale(lesson.created_timestamp, now)
    parts = [
        f"**Tags:** {', '.join(lesson.tags)}",
        f"**Confidence:** {lesson.confidence:.2f}",
        f"**From task:** {lesson.task_id}",
    ]
    if stale:
        age_days = compute_lesson_staleness(lesson.created_timestamp, now)
        parts.append(f"**Staleness:** This lesson is {age_days:.0f} days old and may be outdated.")
    parts.append("")
    parts.append(lesson.content)
    parts.append("")
    return "\n".join(parts)


def _content_similarity(s1: str, s2: str) -> float:
    """Compute a simple similarity score between two strings (0-1).

    Uses word overlap.
    """
    words1 = set(s1.split())
    words2 = set(s2.split())
    if not (words1 | words2):
        return 1.0 if s1 == s2 else 0.0

    intersection = len(words1 & words2)
    union = len(words1 | words2)
    return intersection / union


def _parse_lesson(data: Any) -> Lesson | None:
    """Parse a JSON dict into a Lesson dataclass.

    Args:
        data: JSON dict from JSONL file.

    Returns:
        Lesson instance, or None if parsing fails.
    """
    try:
        return Lesson(
            lesson_id=str(data["lesson_id"]),
            tags=[t.lower().strip() for t in data.get("tags", [])],
            content=str(data["content"]),
            confidence=float(data.get("confidence", 0.8)),
            created_timestamp=float(data["created_timestamp"]),
            filed_by_agent=str(data["filed_by_agent"]),
            task_id=str(data["task_id"]),
            version=int(data.get("version", 1)),
            content_hash=data.get("content_hash") or None,
            prev_hash=data.get("prev_hash") or None,
            chain_hash=data.get("chain_hash") or None,
        )
    except (KeyError, ValueError, TypeError) as e:
        logger.debug("Failed to parse lesson: %s", e)
        return None


# ---------------------------------------------------------------------------
# Internal helpers — content-based (lock-protocol aware)
# ---------------------------------------------------------------------------


def _find_similar_lesson_in_content(
    content: str | None,
    tags: list[str],
    lesson_content: str,
) -> str | None:
    """Find an existing lesson with similar tags and content in raw JSONL text.

    Like :func:`_find_similar_lesson` but operates on in-memory content
    rather than reading from disk, for use within a locked section.

    Args:
        content: Raw JSONL file content (or None if empty).
        tags: Tags for the new lesson.
        lesson_content: Content for the new lesson.

    Returns:
        lesson_id of similar lesson, or None if no match found.
    """
    if not content:
        return None

    new_tags_set = set(tags)
    content_lower = lesson_content.lower()

    for line in content.split("\n"):
        line_stripped = line.strip()
        if not line_stripped:
            continue
        try:
            data = json.loads(line_stripped)
            lesson = _parse_lesson(data)
            if lesson is None:
                continue

            # Jaccard similarity on tags
            existing_tags = set(lesson.tags)
            if not (new_tags_set | existing_tags):
                continue

            intersection = len(new_tags_set & existing_tags)
            union = len(new_tags_set | existing_tags)
            jaccard = intersection / union if union > 0 else 0.0

            # If Jaccard is high AND content is similar, it's a duplicate
            if jaccard >= _DEDUP_THRESHOLD and _content_similarity(content_lower, lesson.content.lower()) > 0.8:
                return lesson.lesson_id
        except (json.JSONDecodeError, TypeError, KeyError):
            continue

    return None


def _update_lesson_confidence_from_content(
    lessons_path: Path,
    lesson_id: str,
    new_confidence: float,
    guard: MemoryFileGuard,
) -> None:
    """Update confidence and version of an existing lesson within a guard.

    Modifies the content in the existing guard and writes the result atomically.

    Args:
        lessons_path: Path to lessons.jsonl (for logging).
        lesson_id: ID of lesson to update.
        new_confidence: New confidence score.
        guard: The active memory file guard holding the current content.
    """
    original = guard.original_content
    if original is None:
        return

    updated = False
    lines: list[str] = []

    for line in original.split("\n"):
        line_stripped = line.strip()
        if not line_stripped:
            continue
        try:
            data = json.loads(line_stripped)
            if data.get("lesson_id") == lesson_id:
                # Update confidence and version
                data["confidence"] = new_confidence
                data["version"] = data.get("version", 1) + 1
                updated = True
            lines.append(json.dumps(data))
        except (json.JSONDecodeError, TypeError):
            lines.append(line_stripped)

    if updated:
        guard.write_backup()
        new_content = "\n".join(lines) + "\n"
        guard.write_new(new_content)
        logger.debug("Updated confidence for lesson %s in %s", lesson_id, lessons_path)


def _get_last_chain_hash_from_content(content: str | None) -> str:
    """Extract the chain_hash of the last lesson entry from raw JSONL text.

    Args:
        content: Raw JSONL file content (or None if empty).

    Returns:
        The chain_hash of the last entry, or ``GENESIS_HASH`` for the first entry.
    """
    from bernstein.core.memory_integrity import GENESIS_HASH

    if not content:
        return GENESIS_HASH

    last_hash: str = GENESIS_HASH
    for line in content.split("\n"):
        line_stripped = line.strip()
        if not line_stripped:
            continue
        try:
            data = json.loads(line_stripped)
            ch = data.get("chain_hash")
            if ch:
                last_hash = ch
        except (json.JSONDecodeError, TypeError):
            continue

    return last_hash
