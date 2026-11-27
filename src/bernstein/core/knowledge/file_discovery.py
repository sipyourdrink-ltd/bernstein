"""File discovery and project context gathering.

Provides fast file tree enumeration, project context assembly,
and memory retrieval from .sdd/ directory.
"""

from __future__ import annotations

import logging
import time
from contextlib import suppress
from typing import TYPE_CHECKING, Any

from bernstein.core.git_context import (
    ls_files as _gc_ls_files,
)

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

_file_tree_cache: dict[str, tuple[float, list[str]]] = {}

_IGNORED_DIRS = frozenset(
    {
        ".git",
        "__pycache__",
        "node_modules",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "venv",
        "dist",
        "build",
        ".egg-info",
        ".tox",
        ".sdd/runtime",
    }
)

_IGNORED_SUFFIXES = frozenset({".pyc", ".pyo", ".egg-info"})


def _should_skip(path: Path) -> bool:
    """Return True if *path* should be excluded from the file tree."""
    for part in path.parts:
        if part in _IGNORED_DIRS:
            return True
    return path.suffix in _IGNORED_SUFFIXES


_FILE_TREE_TTL = 60.0  # seconds


def clear_caches() -> None:
    """Reset all module-level caches. Useful for tests."""
    _file_tree_cache.clear()


def file_tree(workdir: Path, max_lines: int = 50) -> str:
    """Build a compact file-tree listing of the project.

    Uses ``git ls-files`` when inside a git repo (fast, respects
    .gitignore). Falls back to a recursive walk with heuristic filters.

    File names are cached per *workdir* for 60 seconds. Each call
    applies its own line limit to the cached listing.

    Args:
        workdir: Project root directory.
        max_lines: Maximum number of lines to include.

    Returns:
        A newline-separated file listing, truncated to *max_lines*.
    """
    cache_key = str(workdir)
    now = time.monotonic()
    cached = _file_tree_cache.get(cache_key)
    if cached is not None and now - cached[0] < _FILE_TREE_TTL:
        lines = cached[1]
    else:
        lines = _gc_ls_files(workdir)
        if not lines:
            for path in sorted(workdir.rglob("*")):
                if path.is_dir():
                    continue
                rel = path.relative_to(workdir)
                if _should_skip(rel):
                    continue
                lines.append(str(rel))
        _file_tree_cache[cache_key] = (now, lines)

    if len(lines) > max_lines:
        truncated = lines[:max_lines]
        truncated.append(f"... ({len(lines) - max_lines} more files)")
        return "\n".join(truncated)
    return "\n".join(lines)


def _read_if_exists(path: Path, max_chars: int = 4000) -> str | None:
    """Read a text file, returning None if it doesn't exist.

    Args:
        path: File to read.
        max_chars: Truncate content to this many characters.

    Returns:
        File content (possibly truncated) or None.
    """
    if not path.is_file():
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    if len(text) > max_chars:
        return text[:max_chars] + f"\n... (truncated, {len(text)} chars total)"
    return text


def available_roles(templates_dir: Path) -> list[str]:
    """Discover available specialist roles from the templates directory.

    Each subdirectory of *templates_dir* that contains a
    ``system_prompt.md`` is treated as a valid role.

    Args:
        templates_dir: Path to ``templates/roles/``.

    Returns:
        Sorted list of role names.
    """
    if not templates_dir.is_dir():
        return []
    roles = [
        child.name
        for child in sorted(templates_dir.iterdir())
        if child.is_dir() and (child / "system_prompt.md").exists()
    ]
    return roles


def gather_project_context(workdir: Path, max_lines: int = 100) -> str:
    """Gather project context for the manager: file tree, README, .sdd/project.md.

    Args:
        workdir: Project root directory.
        max_lines: Maximum file-tree lines.

    Returns:
        Formatted context string ready for prompt injection.
    """
    sections: list[str] = []

    # File tree
    tree = file_tree(workdir, max_lines=max_lines)
    if tree:
        sections.append(f"## File tree\n```\n{tree}\n```")

    # README
    for name in ("README.md", "README.rst", "README.txt", "README"):
        readme = _read_if_exists(workdir / name)
        if readme:
            sections.append(f"## README\n{readme}")
            break

    # .sdd/project.md
    project_md = _read_if_exists(workdir / ".sdd" / "project.md")
    if project_md:
        sections.append(f"## Project description (.sdd/project.md)\n{project_md}")

    return "\n\n".join(sections)


def get_recent_project_memory(sdd_dir: Path, limit: int = 5) -> list[dict[str, Any]]:
    """Retrieve recent decisions/lessons from .sdd/knowledge/.

    Args:
        sdd_dir: Path to .sdd directory.
        limit: Maximum number of recent items to return.

    Returns:
        List of decision dicts with title, summary, date.
    """
    kb_dir = sdd_dir / "knowledge"
    if not kb_dir.exists():
        return []

    items: list[dict[str, Any]] = []
    for path in sorted(kb_dir.glob("*.md"), reverse=True)[:limit]:
        with suppress(OSError):
            content = path.read_text(encoding="utf-8")
            # Extract first line as title
            lines = content.split("\n")
            title = lines[0].lstrip("#").strip() if lines else path.stem
            items.append(
                {
                    "title": title,
                    "summary": content[:200],
                    "date": path.stat().st_mtime,
                }
            )

    return items


def gather_project_memory(sdd_dir: Path) -> str:
    """Build a formatted summary of recent project decisions for context.

    Args:
        sdd_dir: Path to .sdd directory.

    Returns:
        Formatted memory string or empty string if no memory found.
    """
    items = get_recent_project_memory(sdd_dir, limit=5)
    if not items:
        return ""

    lines = ["## Recent project decisions"]
    for item in items:
        lines.append(f"- {item['title']}")
        if item.get("summary"):
            lines.append(f"  {item['summary'][:100]}")

    return "\n".join(lines)
