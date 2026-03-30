"""Shared parser for backlog markdown task files."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class ParsedBacklogTask:
    """Normalized metadata extracted from one backlog file."""

    title: str
    description: str
    role: str
    priority: int
    scope: str
    complexity: str
    source_file: str

    def to_task_payload(self) -> dict[str, object]:
        """Convert to POST /tasks payload."""
        return {
            "title": self.title,
            "description": self.description,
            "role": self.role,
            "priority": self.priority,
            "scope": self.scope,
            "complexity": self.complexity,
        }


def parse_backlog_text(filename: str, content: str) -> ParsedBacklogTask | None:
    """Parse backlog markdown text into normalized task metadata."""
    text = content.strip()
    if not text:
        return None

    parsed = _parse_yaml_frontmatter(filename, content)
    if parsed is not None:
        return parsed
    return _parse_markdown_fields(filename, content)


def parse_backlog_path(path: Path) -> ParsedBacklogTask | None:
    """Parse backlog file from disk."""
    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return None
    return parse_backlog_text(path.name, content)


def _parse_yaml_frontmatter(filename: str, content: str) -> ParsedBacklogTask | None:
    if not content.startswith("---"):
        return None
    end = content.find("\n---", 3)
    if end == -1:
        return None
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        return None
    try:
        loaded: object = yaml.safe_load(content[3:end]) or {}
    except Exception:
        return None
    if not isinstance(loaded, dict):
        return None
    raw = cast("dict[str, object]", loaded)

    title = str(raw.get("title", "")).strip()
    if not title:
        body = content[end + 4 :].splitlines()
        title = _extract_h1_title(body)
    if not title:
        return None

    return ParsedBacklogTask(
        title=title,
        description=content.strip(),
        role=str(raw.get("role", "backend")).strip() or "backend",
        priority=_parse_priority(raw.get("priority", 2)),
        scope=_parse_scope(str(raw.get("scope", "medium"))),
        complexity=_parse_complexity(str(raw.get("complexity", "medium"))),
        source_file=filename,
    )


def _parse_markdown_fields(filename: str, content: str) -> ParsedBacklogTask | None:
    lines = content.splitlines()
    title = _extract_h1_title(lines)
    if not title:
        return None

    role_match = re.search(r"\*\*Role:\*\*\s*(.+)", content, flags=re.IGNORECASE)
    priority_match = re.search(r"\*\*Priority:\*\*\s*(.+)", content, flags=re.IGNORECASE)
    scope_match = re.search(r"\*\*Scope:\*\*\s*(.+)", content, flags=re.IGNORECASE)
    complexity_match = re.search(r"\*\*Complexity:\*\*\s*(.+)", content, flags=re.IGNORECASE)

    return ParsedBacklogTask(
        title=title,
        description=content.strip(),
        role=(role_match.group(1).strip() if role_match else "backend"),
        priority=_parse_priority(priority_match.group(1) if priority_match else 2),
        scope=_parse_scope(scope_match.group(1) if scope_match else "medium"),
        complexity=_parse_complexity(complexity_match.group(1) if complexity_match else "medium"),
        source_file=filename,
    )


def _extract_h1_title(lines: list[str]) -> str:
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("# "):
            return stripped[2:].strip()
    return ""


def _parse_priority(raw: object) -> int:
    match = re.search(r"\d+", str(raw))
    if not match:
        return 2
    value = int(match.group(0))
    if value <= 1:
        return 1
    if value >= 3:
        return 3
    return 2


def _parse_scope(raw: str) -> str:
    value = raw.strip().lower()
    return value if value in {"small", "medium", "large"} else "medium"


def _parse_complexity(raw: str) -> str:
    value = raw.strip().lower()
    return value if value in {"low", "medium", "high"} else "medium"
