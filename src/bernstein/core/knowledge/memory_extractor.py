"""Extract learnings from agent logs after task completion.

Lightweight, regex-based extraction of useful patterns from agent session
logs. No LLM calls — just deterministic text matching to capture error
resolutions, file modifications, and key decisions for future agents.
"""

from __future__ import annotations

import json
import logging
import re
import time
from dataclasses import asdict, dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Extraction patterns
# ---------------------------------------------------------------------------

# Error-then-fix: a line mentioning an error followed (within a window) by
# a line indicating the resolution (e.g. "Fixed", "Resolved", "Works now").
_ERROR_RE = re.compile(
    r"(?i)(error|exception|failed|broken|bug|issue|problem|traceback)",
)
_FIX_RE = re.compile(
    r"(?i)(fix(?:ed)?|resolv(?:ed|ing)|work(?:s|ed|ing)|success|pass(?:ed|es|ing)|solved)",
)

# File modifications — matches common agent output patterns.
# Allows an optional timestamp prefix like "[2026-04-04 10:02:00] ".
_FILE_MODIFIED_RE = re.compile(
    r"^(?:\[.*?\]\s+)?(?:Modified|Created|Wrote|Updated|Edited|Changed):\s+(\S+)",
)

# Key decisions — lines where the agent explains reasoning.
_DECISION_RE = re.compile(
    r"(?i)\b(decided|chose|because|reason(?:ing)?|trade-?off|instead of|opted|approach)\b",
)


@dataclass
class AgentMemory:
    """Structured learnings from a single agent session.

    Attributes:
        session_id: Unique identifier for the agent session.
        task_title: Title of the task the agent worked on.
        role: Agent role (e.g. "backend", "qa").
        learnings: Human-readable lessons extracted from the log.
        files_modified: Files the agent touched during execution.
        patterns_discovered: Reusable patterns (error/fix pairs, decisions).
        timestamp: Epoch time when the memory was created.
    """

    session_id: str
    task_title: str
    role: str
    learnings: list[str] = field(default_factory=list[str])
    files_modified: list[str] = field(default_factory=list[str])
    patterns_discovered: list[str] = field(default_factory=list[str])
    timestamp: float = field(default_factory=time.time)


# ---------------------------------------------------------------------------
# Extractor
# ---------------------------------------------------------------------------

# How many lines after an error to look for a corresponding fix.
_FIX_WINDOW = 15


class MemoryExtractor:
    """Extract and persist agent learnings from session logs.

    Reads log files from ``.sdd/runtime/``, parses them for error
    resolutions, file modifications, and key decisions, then stores
    structured memories in ``.sdd/memory/learnings.jsonl``.

    Args:
        workdir: Project root directory containing ``.sdd/``.
    """

    def __init__(self, workdir: Path) -> None:
        self._workdir = workdir

    # ----- extraction -----

    def extract_from_log(
        self,
        log_path: Path,
        task_title: str,
        role: str,
    ) -> AgentMemory:
        """Parse an agent log and return structured memory.

        Extraction is lightweight — regex only, no LLM calls.

        Args:
            log_path: Path to the agent session log file.
            task_title: Title of the completed task.
            role: Agent role that executed the task.

        Returns:
            An ``AgentMemory`` containing extracted learnings.
        """
        session_id = log_path.stem  # e.g. "abc123" from "abc123.log"
        memory = AgentMemory(
            session_id=session_id,
            task_title=task_title,
            role=role,
        )

        try:
            lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            logger.debug("Cannot read log at %s", log_path)
            return memory

        if not lines:
            return memory

        memory.files_modified = self._extract_files_modified(lines)
        memory.learnings = self._extract_error_fix_pairs(lines)
        memory.patterns_discovered = self._extract_decisions(lines)

        return memory

    # ----- persistence -----

    def save(self, memory: AgentMemory) -> None:
        """Append a memory record to the learnings JSONL file.

        Creates ``.sdd/memory/`` if it does not exist.

        Args:
            memory: The ``AgentMemory`` to persist.
        """
        mem_dir = self._workdir / ".sdd" / "memory"
        mem_dir.mkdir(parents=True, exist_ok=True)
        learnings_path = mem_dir / "learnings.jsonl"

        record = asdict(memory)
        try:
            with learnings_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, default=str) + "\n")
        except OSError:
            logger.warning("Failed to write memory to %s", learnings_path)

    # ----- querying -----

    def query(
        self,
        role: str | None = None,
        file_pattern: str | None = None,
    ) -> list[AgentMemory]:
        """Search past learnings with optional filters.

        Args:
            role: If provided, only return memories from this role.
            file_pattern: If provided, only return memories where at least
                one modified file contains this substring.

        Returns:
            List of matching ``AgentMemory`` records, newest first.
        """
        learnings_path = self._workdir / ".sdd" / "memory" / "learnings.jsonl"
        if not learnings_path.exists():
            return []

        results: list[AgentMemory] = []
        try:
            raw_lines = learnings_path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return []

        for raw_line in raw_lines:
            raw_line = raw_line.strip()
            if not raw_line:
                continue
            try:
                data = json.loads(raw_line)
            except json.JSONDecodeError:
                continue

            if role is not None and data.get("role") != role:
                continue
            if file_pattern is not None:
                files: list[str] = data.get("files_modified", [])
                if not any(file_pattern in f for f in files):
                    continue

            results.append(
                AgentMemory(
                    session_id=data.get("session_id", ""),
                    task_title=data.get("task_title", ""),
                    role=data.get("role", ""),
                    learnings=data.get("learnings", []),
                    files_modified=data.get("files_modified", []),
                    patterns_discovered=data.get("patterns_discovered", []),
                    timestamp=float(data.get("timestamp", 0)),
                )
            )

        # Newest first.
        results.sort(key=lambda m: m.timestamp, reverse=True)
        return results

    # ----- private helpers -----

    @staticmethod
    def _extract_files_modified(lines: list[str]) -> list[str]:
        """Return deduplicated list of files modified, preserving order."""
        seen: set[str] = set()
        result: list[str] = []
        for line in lines:
            m = _FILE_MODIFIED_RE.match(line.strip())
            if m:
                path = m.group(1)
                if path not in seen:
                    seen.add(path)
                    result.append(path)
        return result

    @staticmethod
    def _extract_error_fix_pairs(lines: list[str]) -> list[str]:
        """Find error lines that have a nearby fix and describe the pair."""
        learnings: list[str] = []
        seen: set[str] = set()
        for i, line in enumerate(lines):
            stripped = line.strip()
            if not _ERROR_RE.search(stripped):
                continue
            # Look ahead for a fix within the window.
            window_end = min(i + _FIX_WINDOW + 1, len(lines))
            for j in range(i + 1, window_end):
                fix_line = lines[j].strip()
                if _FIX_RE.search(fix_line):
                    # Truncate long lines for readability.
                    error_text = stripped[:200]
                    fix_text = fix_line[:200]
                    key = f"{error_text}|{fix_text}"
                    if key not in seen:
                        seen.add(key)
                        learnings.append(f"Error: {error_text} -> Fix: {fix_text}")
                    break
        return learnings

    @staticmethod
    def _extract_decisions(lines: list[str]) -> list[str]:
        """Extract lines containing key decision language."""
        decisions: list[str] = []
        seen: set[str] = set()
        for line in lines:
            stripped = line.strip()
            if not stripped:
                continue
            if _DECISION_RE.search(stripped):
                # Truncate for storage.
                text = stripped[:300]
                if text not in seen:
                    seen.add(text)
                    decisions.append(text)
        return decisions
