"""Shared parser for backlog markdown task files."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from pathlib import Path


@dataclass(frozen=True)
class ParsedBacklogTask:
    """Normalized metadata extracted from one backlog file.

    Extended fields from Ticket Format v1 (YAML frontmatter) are used
    by the orchestrator for model routing, quality gates, and file locking.
    """

    title: str
    description: str
    role: str
    priority: int
    scope: str
    complexity: str
    source_file: str
    # ── Extended fields (Ticket Format v1) ──
    ticket_id: str = ""
    model: str = "auto"
    effort: str = "normal"
    ticket_type: str = "feature"
    tags: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()
    affected_paths: tuple[str, ...] = ()
    context_files: tuple[str, ...] = ()
    estimated_minutes: int = 45
    require_review: bool = False
    require_human_approval: bool = False
    janitor_signals: tuple[dict[str, str], ...] = ()

    def to_task_payload(self) -> dict[str, object]:
        """Convert to POST /tasks payload."""
        payload: dict[str, object] = {
            "title": self.title,
            "description": self.description,
            "role": self.role,
            "priority": self.priority,
            "scope": self.scope,
            "complexity": self.complexity,
        }
        if self.model != "auto":
            payload["model"] = self.model
        if self.effort != "normal":
            payload["effort"] = self.effort
        if self.estimated_minutes != 45:
            payload["estimated_minutes"] = self.estimated_minutes
        return payload


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

    # Extended fields
    tags_raw = raw.get("tags", [])
    tags = tuple(str(t) for t in tags_raw) if isinstance(tags_raw, list) else ()
    deps_raw = raw.get("depends_on", [])
    depends_on = tuple(str(d) for d in deps_raw) if isinstance(deps_raw, list) else ()
    affected_raw = raw.get("affected_paths", [])
    affected = tuple(str(p) for p in affected_raw) if isinstance(affected_raw, list) else ()
    context_raw = raw.get("context_files", [])
    context = tuple(str(p) for p in context_raw) if isinstance(context_raw, list) else ()
    janitor_raw = raw.get("janitor_signals", [])
    janitor = (
        tuple(
            {"type": str(s.get("type", "")), "value": str(s.get("value", ""))}
            for s in janitor_raw
            if isinstance(s, dict)
        )
        if isinstance(janitor_raw, list)
        else ()
    )

    return ParsedBacklogTask(
        title=title,
        description=content.strip(),
        role=str(raw.get("role", "backend")).strip() or "backend",
        priority=_parse_priority(raw.get("priority", 2)),
        scope=_parse_scope(str(raw.get("scope", "medium"))),
        complexity=_parse_complexity(str(raw.get("complexity", "medium"))),
        source_file=filename,
        ticket_id=str(raw.get("id", "")).strip(),
        model=str(raw.get("model", "auto")).strip(),
        effort=str(raw.get("effort", "normal")).strip(),
        ticket_type=str(raw.get("type", "feature")).strip(),
        tags=tags,
        depends_on=depends_on,
        affected_paths=affected,
        context_files=context,
        estimated_minutes=int(raw.get("estimated_minutes", 45)),
        require_review=bool(raw.get("require_review", False)),
        require_human_approval=bool(raw.get("require_human_approval", False)),
        janitor_signals=janitor,
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
