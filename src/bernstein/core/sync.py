"""Sync .sdd/backlog/*.md files with the task server.

Two tracking systems exist: static backlog .md files (authored by humans or agents)
and the dynamic task server (tasks.jsonl). This module bridges them:
- On demand or at startup, create server tasks for new backlog files.
- When a task is done on the server, move its backlog file to backlog/done/.
"""

from __future__ import annotations

import logging
import re
import shutil
import unicodedata
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import httpx

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


@dataclass
class BacklogTask:
    """Metadata parsed from a .sdd/backlog/open/*.md file.

    Attributes:
        title: Task title from the first ``# `` heading.
        description: Full file content.
        role: Agent role (e.g. ``backend``, ``qa``).
        priority: Numeric priority (1 = critical, 2 = normal, 3 = low).
        scope: Task scope (``small``, ``medium``, ``large``).
        complexity: Task complexity (``low``, ``medium``, ``high``).
        source_file: Basename of the originating .md file.
    """

    title: str
    description: str
    role: str
    priority: int
    scope: str
    complexity: str
    source_file: str


def parse_backlog_file(path: Path) -> BacklogTask | None:
    """Parse a backlog .md file into a BacklogTask.

    Supports two formats:
    1. **YAML frontmatter** (``---`` delimited block at the top of the file).
    2. **Markdown bold fields** — lines like ``**Role:** backend``.

    Args:
        path: Path to the ``.md`` file.

    Returns:
        Parsed BacklogTask, or None if the file cannot be parsed.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        logger.warning("Cannot read backlog file %s: %s", path, exc)
        return None

    if not text.strip():
        return None

    # Try YAML frontmatter first
    task = _parse_yaml_frontmatter(text, path.name)
    if task is not None:
        return task

    # Fall back to markdown bold fields
    return _parse_markdown_fields(text, path.name)


def _parse_yaml_frontmatter(text: str, filename: str) -> BacklogTask | None:
    """Try to parse YAML frontmatter from file text.

    Args:
        text: Full file text.
        filename: Source filename for the BacklogTask.

    Returns:
        BacklogTask if YAML frontmatter found and valid, else None.
    """
    if not text.startswith("---"):
        return None

    end = text.find("\n---", 3)
    if end == -1:
        return None

    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError:
        return None

    try:
        raw: dict[str, Any] = yaml.safe_load(text[3:end]) or {}
    except Exception:
        return None

    title: str = str(raw.get("title", "")).strip()
    if not title:
        # Try first heading in the body
        body = text[end + 4 :]
        title = _extract_heading(body)

    if not title:
        return None

    role = str(raw.get("role", "backend")).strip()
    priority = _parse_priority(str(raw.get("priority", "2")))
    scope = _normalise_word(str(raw.get("scope", "medium")))
    complexity = _normalise_word(str(raw.get("complexity", "medium")))

    return BacklogTask(
        title=title,
        description=text,
        role=role,
        priority=priority,
        scope=scope,
        complexity=complexity,
        source_file=filename,
    )


def _parse_markdown_fields(text: str, filename: str) -> BacklogTask | None:
    """Parse task metadata from markdown bold fields.

    Looks for lines like ``**Role:** backend``.

    Args:
        text: Full file text.
        filename: Source filename for the BacklogTask.

    Returns:
        BacklogTask, or None if no title found.
    """
    title = _extract_heading(text)
    if not title:
        return None

    role = _extract_md_field(text, "Role") or "backend"
    priority = _parse_priority(_extract_md_field(text, "Priority") or "2")
    scope = _normalise_word(_extract_md_field(text, "Scope") or "medium")
    complexity = _normalise_word(_extract_md_field(text, "Complexity") or "medium")

    return BacklogTask(
        title=title,
        description=text,
        role=role,
        priority=priority,
        scope=scope,
        complexity=complexity,
        source_file=filename,
    )


def _extract_heading(text: str) -> str:
    """Return the first ``# `` heading from markdown text."""
    for line in text.splitlines():
        if line.startswith("# "):
            return line[2:].strip()
    return ""


def _extract_md_field(text: str, field_name: str) -> str | None:
    """Extract ``**FieldName:** value`` from markdown text.

    Args:
        text: Markdown text to search.
        field_name: Name of the field (case-insensitive).

    Returns:
        Stripped value string, or None if not found.
    """
    pattern = rf"\*\*{re.escape(field_name)}:\*\*\s*(.+)"
    match = re.search(pattern, text, re.IGNORECASE)
    if match:
        return match.group(1).strip()
    return None


def _parse_priority(raw: str) -> int:
    """Extract the leading integer from a priority string.

    Handles formats like ``"1 (critical)"``, ``"2"`` and ``"high"``.

    Args:
        raw: Raw priority string.

    Returns:
        Integer priority (1-3), defaulting to 2.
    """
    match = re.match(r"(\d+)", raw.strip())
    if match:
        return max(1, min(3, int(match.group(1))))
    return 2


def _normalise_word(raw: str) -> str:
    """Lowercase and strip parenthetical annotations.

    ``"medium (standard)"`` → ``"medium"``

    Args:
        raw: Raw field value.

    Returns:
        First word, lowercased.
    """
    first = raw.split()[0] if raw.split() else raw
    return first.lower()


# ---------------------------------------------------------------------------
# Deduplication helpers
# ---------------------------------------------------------------------------


def normalise_title(title: str) -> str:
    """Normalise a task title to a comparable slug.

    ``"Wire TierAwareRouter into spawner"`` → ``"wire-tierawarerouter-into-spawner"``

    Args:
        title: Raw title string.

    Returns:
        Lowercase slug with non-alphanumeric runs replaced by hyphens.
    """
    normalised = unicodedata.normalize("NFKD", title.lower())
    return re.sub(r"[^a-z0-9]+", "-", normalised).strip("-")


def _file_to_slug(filename: str) -> str:
    """Convert a backlog filename to a slug for fuzzy matching.

    ``"115-wire-tier-aware-router.md"`` → ``"wire-tier-aware-router"``

    Args:
        filename: Basename of the backlog file.

    Returns:
        Slug without leading number prefix or ``.md`` extension.
    """
    name = re.sub(r"^\d+-", "", filename)
    name = re.sub(r"\.md$", "", name)
    return name.lower()


def _task_already_exists(task: BacklogTask, existing_slugs: set[str]) -> bool:
    """Return True if a matching task already exists on the server.

    Checks both the normalised title and the file slug (for files that were
    created before this sync ran and whose titles may differ slightly).

    Args:
        task: Parsed backlog task.
        existing_slugs: Set of normalised title slugs from the server.

    Returns:
        True if a duplicate is detected.
    """
    if normalise_title(task.title) in existing_slugs:
        return True
    file_slug = _file_to_slug(task.source_file)
    return any(file_slug and file_slug == slug for slug in existing_slugs)


# ---------------------------------------------------------------------------
# Server interaction
# ---------------------------------------------------------------------------


def _get_tasks_by_status(
    client: httpx.Client,
    server_url: str,
    status: str,
) -> list[dict[str, Any]]:
    """Fetch tasks filtered by status from the server.

    Args:
        client: httpx client.
        server_url: Base URL of the task server.
        status: Status string (e.g. ``"open"``, ``"done"``).

    Returns:
        List of task dicts, or empty list on error.
    """
    try:
        resp = client.get(f"{server_url}/tasks", params={"status": status})
        resp.raise_for_status()
        return cast("list[dict[str, Any]]", resp.json())
    except httpx.ConnectError:
        raise
    except httpx.HTTPError as exc:
        logger.warning("Failed to fetch tasks (status=%s): %s", status, exc)
        return []


def _build_existing_slugs(client: httpx.Client, server_url: str) -> set[str]:
    """Build a set of normalised title slugs for all tasks on the server.

    Queries open, claimed, in_progress, done, and failed tasks to avoid
    creating duplicates regardless of current status.

    Args:
        client: httpx client.
        server_url: Base URL of the task server.

    Returns:
        Set of normalised slugs.
    """
    slugs: set[str] = set()
    for status in ("open", "claimed", "in_progress", "done", "failed"):
        for task in _get_tasks_by_status(client, server_url, status):
            title = task.get("title", "")
            if title:
                slugs.add(normalise_title(title))
    return slugs


# ---------------------------------------------------------------------------
# SyncResult
# ---------------------------------------------------------------------------


@dataclass
class SyncResult:
    """Outcome of a backlog-to-server sync run.

    Attributes:
        created: Task IDs that were newly created on the server.
        skipped: Filenames skipped because a matching task already existed.
        moved: Filenames moved from ``backlog/open/`` to ``backlog/done/``.
        errors: Human-readable error messages.
    """

    created: list[str] = field(default_factory=lambda: [])
    skipped: list[str] = field(default_factory=lambda: [])
    moved: list[str] = field(default_factory=lambda: [])
    errors: list[str] = field(default_factory=lambda: [])


# ---------------------------------------------------------------------------
# Main sync function
# ---------------------------------------------------------------------------


def sync_backlog_to_server(
    workdir: Path,
    server_url: str = "http://127.0.0.1:8052",
    *,
    client: httpx.Client | None = None,
) -> SyncResult:
    """Sync ``.sdd/backlog/open/*.md`` files with the running task server.

    Steps:
    1. Scan ``backlog/open/`` for ``.md`` files.
    2. For each file, create a task on the server if one with the same title
       does not already exist (fuzzy-matched via slug normalisation).
       The source filename is embedded in the description for traceability.
    3. For each ``done`` task on the server, move the matching backlog file
       from ``backlog/open/`` to ``backlog/done/``.

    Args:
        workdir: Project root directory (parent of ``.sdd/``).
        server_url: Base URL of the task server.
        client: Optional httpx client for testing (created if not given).

    Returns:
        SyncResult with counts and errors.
    """
    result = SyncResult()
    backlog_open = workdir / ".sdd" / "backlog" / "open"
    backlog_done = workdir / ".sdd" / "backlog" / "done"

    if not backlog_open.exists():
        return result

    backlog_done.mkdir(parents=True, exist_ok=True)

    owned_client = client is None
    _client = client or httpx.Client(timeout=10.0)

    try:
        # Build a set of slugs for all existing server tasks
        try:
            existing_slugs = _build_existing_slugs(_client, server_url)
        except httpx.ConnectError:
            result.errors.append("Cannot connect to task server — is it running?")
            return result

        md_files = sorted(backlog_open.glob("*.md"))

        # --- Step 1: create new tasks ---
        for md_file in md_files:
            task = parse_backlog_file(md_file)
            if task is None:
                result.errors.append(f"Could not parse {md_file.name}")
                continue

            if _task_already_exists(task, existing_slugs):
                result.skipped.append(md_file.name)
                logger.debug("Skipping %s — task already on server", md_file.name)
                continue

            # Embed source filename in description so it can be traced back
            description = task.description
            if f"source: {task.source_file}" not in description:
                description = description + f"\n\n<!-- source: {task.source_file} -->"

            payload: dict[str, Any] = {
                "title": task.title,
                "description": description,
                "role": task.role,
                "priority": task.priority,
                "scope": task.scope,
                "complexity": task.complexity,
            }
            try:
                resp = _client.post(f"{server_url}/tasks", json=payload)
                resp.raise_for_status()
                task_id: str = resp.json().get("id", "unknown")
                result.created.append(task_id)
                # Add to slugs so subsequent files don't create duplicates
                existing_slugs.add(normalise_title(task.title))
                logger.info("Created task %s from %s", task_id, md_file.name)
            except httpx.HTTPError as exc:
                result.errors.append(f"Failed to create task from {md_file.name}: {exc}")

        # --- Step 2: move files for completed tasks ---
        done_slugs: set[str] = {
            normalise_title(t.get("title", "")) for t in _get_tasks_by_status(_client, server_url, "done")
        }

        for md_file in sorted(backlog_open.glob("*.md")):
            task = parse_backlog_file(md_file)
            if task is None:
                continue
            if normalise_title(task.title) in done_slugs:
                dest = backlog_done / md_file.name
                try:
                    shutil.move(str(md_file), str(dest))
                    result.moved.append(md_file.name)
                    logger.info("Moved %s to backlog/done/", md_file.name)
                except OSError as exc:
                    result.errors.append(f"Failed to move {md_file.name}: {exc}")

    finally:
        if owned_client:
            _client.close()

    return result
