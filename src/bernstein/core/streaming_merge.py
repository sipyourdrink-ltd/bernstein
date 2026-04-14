"""Streaming task results for long-running agents (incremental merge).

Allows agent output to be merged incrementally as chunks become available
rather than waiting for full task completion.  A test-writing agent that has
written 5 of 10 test files can merge the first 5 while still working on the
remaining 5, reducing wall-clock time.

Usage::

    from bernstein.core.streaming_merge import (
        IncrementalChunk,
        StreamingMergeState,
        StreamingMergeManager,
        detect_merge_ready_chunk,
        merge_chunk,
        should_stream,
    )

    if should_stream(task):
        chunk = detect_merge_ready_chunk(task["id"], agent_output)
        if chunk is not None:
            merge_chunk(chunk)
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class IncrementalChunk:
    """A chunk of work ready for incremental merge.

    Attributes:
        chunk_id: Unique identifier for this chunk.
        task_id: The parent task this chunk belongs to.
        files: Files produced or modified in this chunk.
        quality_gate_passed: Whether quality checks passed for this chunk.
        is_final: Whether this is the last chunk for the task.
    """

    chunk_id: str
    task_id: str
    files: tuple[str, ...]
    quality_gate_passed: bool
    is_final: bool = False


@dataclass(frozen=True)
class StreamingMergeState:
    """State of a streaming/incremental merge for a task.

    Attributes:
        task_id: The task being streamed.
        chunks_merged: Number of chunks successfully merged so far.
        chunks_pending: Number of chunks detected but not yet merged.
        files_merged: All files merged across all chunks.
        is_complete: Whether the task's streaming merge is finished.
    """

    task_id: str
    chunks_merged: int
    chunks_pending: int
    files_merged: tuple[str, ...]
    is_complete: bool


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_DEFAULT_STREAMING_PATTERNS: list[re.Pattern[str]] = [
    re.compile(r"(?m)^## (?:File|Step|Section)\s+\d+(?:\s|$)", re.IGNORECASE),
    re.compile(r"(?m)^---+$"),
    re.compile(r"(?m)^```(?:python|javascript|typescript|yaml|json)\s*$"),
]

_DEFAULT_MIN_CHUNK_LINES = 10
_DEFAULT_MAX_CHUNK_SIZE = 500
_STREAMING_TASK_KEYWORDS = frozenset(
    {
        "test",
        "tests",
        "migration",
        "migrations",
        "refactor",
        "multi-file",
        "batch",
        "bulk",
        "generate",
        "codegen",
    }
)

# ---------------------------------------------------------------------------
# Chunk detection
# ---------------------------------------------------------------------------


def detect_merge_ready_chunk(
    task_id: str,
    agent_output: str,
    *,
    patterns: list[re.Pattern[str]] | None = None,
    min_lines: int = _DEFAULT_MIN_CHUNK_LINES,
) -> IncrementalChunk | None:
    """Detect if a chunk of agent output is ready for incremental merge.

    Scans the output for section boundaries (markdown headers, code fences,
    horizontal rules) and returns the first detected chunk if it meets the
    minimum line threshold.

    Args:
        task_id: The task producing the output.
        agent_output: The raw output text from the agent.
        patterns: Custom boundary patterns.  Defaults to common markdown
            separators.
        min_lines: Minimum lines a chunk must contain to be mergeable.

    Returns:
        An :class:`IncrementalChunk` if a boundary is found, else ``None``.
    """
    if not agent_output or not agent_output.strip():
        return None

    boundary_patterns = patterns or _DEFAULT_STREAMING_PATTERNS
    lines = agent_output.split("\n")

    if len(lines) < min_lines:
        return None

    # Find the first boundary that splits the output into a substantial chunk
    for i, line in enumerate(lines):
        if i == 0:
            continue
        if any(p.match(line) for p in boundary_patterns):
            chunk_lines = lines[:i]
            if len(chunk_lines) >= min_lines:
                # Extract file references from the chunk
                files = _extract_file_references("\n".join(chunk_lines))
                return IncrementalChunk(
                    chunk_id=f"chunk-{task_id}-{i}",
                    task_id=task_id,
                    files=tuple(files),
                    quality_gate_passed=True,
                    is_final=False,
                )

    # If no boundary found but output is large enough, treat whole output as
    # a final chunk
    if len(lines) >= min_lines * 2:
        files = _extract_file_references(agent_output)
        return IncrementalChunk(
            chunk_id=f"chunk-{task_id}-final",
            task_id=task_id,
            files=tuple(files),
            quality_gate_passed=True,
            is_final=True,
        )

    return None


def _extract_file_references(text: str) -> list[str]:
    """Extract file path references from text.

    Matches common patterns like:
    - ``path/to/file.py``
    - ``src/module/file.ts``
    - ``Creating file: path/to/file``

    Args:
        text: The text to scan for file references.

    Returns:
        A deduplicated list of file path strings.
    """
    # Pattern for file paths with common extensions.
    # Group 1: action-word + path.  Group 2: bare path with known prefix.
    file_pattern = re.compile(
        r"(?:Creating|Writing|Modified|Updated|Created)\s+(?:file\s+)?[`']?([^\s`'\"]+\.\w{1,10})[`']?"
        r"|((?:src|lib|tests?|examples|docs?|scripts?|pkg)/[^\s`'\"]+\.\w{1,10})"
    )
    matches = file_pattern.findall(text)

    # Each match is a tuple (group1, group2); pick whichever matched.
    files: list[str] = []
    for match in matches:
        if isinstance(match, tuple):
            files.extend(m for m in match if m)
        elif isinstance(match, str) and match:
            files.append(match)

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique: list[str] = []
    for f in files:
        if f not in seen and len(f) < 200:
            seen.add(f)
            unique.append(f)

    return unique


# ---------------------------------------------------------------------------
# Task eligibility
# ---------------------------------------------------------------------------


def should_stream(task: dict[str, Any]) -> bool:
    """Determine if a task should use streaming merge.

    A task is eligible for streaming when its description or metadata
    indicates a multi-step, multi-file, or long-running workload.

    Args:
        task: A task dictionary.  Checked keys include ``description``,
            ``title``, ``steps``, and ``files``.

    Returns:
        ``True`` if the task is a good candidate for streaming merge.
    """
    # Check explicit flag
    if task.get("streaming") is True:
        return True

    # Check step count
    steps = task.get("steps")
    if isinstance(steps, (list, tuple)) and len(steps) >= 3:
        return True

    # Check file count
    files = task.get("files")
    if isinstance(files, (list, tuple)) and len(files) >= 3:
        return True

    # Check keywords in description and title
    desc = ""
    for key in ("description", "title", "goal"):
        val = task.get(key)
        if isinstance(val, str):
            desc += " " + val.lower()

    return any(kw in desc for kw in _STREAMING_TASK_KEYWORDS)


# ---------------------------------------------------------------------------
# Merge operation (sync stub — real impl would integrate with git/VCS)
# ---------------------------------------------------------------------------


def merge_chunk(chunk: IncrementalChunk) -> bool:
    """Merge a single chunk incrementally.

    In production this would stage the chunk's files, run quality gates
    (linting, type-checking, tests), and commit the result.  The stub
    returns ``True`` when the chunk passes quality gates.

    Args:
        chunk: The :class:`IncrementalChunk` to merge.

    Returns:
        ``True`` if the merge succeeded, ``False`` otherwise.
    """
    if not chunk.quality_gate_passed:
        logger.warning("Skipping chunk %s — quality gate failed", chunk.chunk_id)
        return False

    if not chunk.files:
        logger.debug("Chunk %s has no files to merge", chunk.chunk_id)
        return True

    logger.info(
        "Merging chunk %s for task %s (%d files, final=%s)",
        chunk.chunk_id,
        chunk.task_id,
        len(chunk.files),
        chunk.is_final,
    )

    # In production: stage files, run gates, commit
    return True


# ---------------------------------------------------------------------------
# Streaming merge manager
# ---------------------------------------------------------------------------


class StreamingMergeManager:
    """Manages streaming merge state for multiple concurrent tasks.

    Usage::

        mgr = StreamingMergeManager()
        mgr.register("task-1")
        mgr.record_chunk(merged_chunk)
        state = mgr.get_state("task-1")
    """

    def __init__(self) -> None:
        self._states: dict[str, _MutableMergeState] = {}

    def register(self, task_id: str) -> None:
        """Register a task for streaming merge tracking.

        Args:
            task_id: The task identifier.
        """
        self._states[task_id] = _MutableMergeState()

    def record_chunk(self, chunk: IncrementalChunk) -> None:
        """Record a merged chunk and update state.

        Args:
            chunk: The :class:`IncrementalChunk` that was merged.
        """
        state = self._states.get(chunk.task_id)
        if state is None:
            self.register(chunk.task_id)
            state = self._states[chunk.task_id]

        state.chunks_merged += 1
        state.files_merged.extend(chunk.files)

        if chunk.is_final:
            state.is_complete = True
            state.chunks_pending = 0
        else:
            state.chunks_pending = max(0, state.chunks_pending - 1)

    def record_pending(self, chunk: IncrementalChunk) -> None:
        """Record a detected chunk that has not yet been merged.

        Args:
            chunk: The :class:`IncrementalChunk` that was detected.
        """
        state = self._states.get(chunk.task_id)
        if state is None:
            self.register(chunk.task_id)
            state = self._states[chunk.task_id]

        state.chunks_pending += 1

    def get_state(self, task_id: str) -> StreamingMergeState:
        """Get the current streaming merge state for a task.

        Args:
            task_id: The task identifier.

        Returns:
            A :class:`StreamingMergeState` snapshot.
        """
        state = self._states.get(task_id)
        if state is None:
            return StreamingMergeState(
                task_id=task_id,
                chunks_merged=0,
                chunks_pending=0,
                files_merged=(),
                is_complete=True,
            )
        return StreamingMergeState(
            task_id=task_id,
            chunks_merged=state.chunks_merged,
            chunks_pending=state.chunks_pending,
            files_merged=tuple(dict.fromkeys(state.files_merged)),
            is_complete=state.is_complete,
        )

    def is_complete(self, task_id: str) -> bool:
        """Check if a task's streaming merge is complete.

        Args:
            task_id: The task identifier.

        Returns:
            ``True`` if all chunks have been merged.
        """
        state = self._states.get(task_id)
        return state is not None and state.is_complete

    def list_active(self) -> list[str]:
        """List task IDs with incomplete streaming merges.

        Returns:
            A list of task identifiers still in progress.
        """
        return [tid for tid, state in self._states.items() if not state.is_complete]

    def clear(self, task_id: str) -> None:
        """Remove tracking state for a task.

        Args:
            task_id: The task identifier.
        """
        self._states.pop(task_id, None)


class _MutableMergeState:
    """Internal mutable state for tracking a streaming merge."""

    __slots__ = ("chunks_merged", "chunks_pending", "files_merged", "is_complete")

    def __init__(self) -> None:
        self.chunks_merged: int = 0
        self.chunks_pending: int = 0
        self.files_merged: list[str] = []
        self.is_complete: bool = False
