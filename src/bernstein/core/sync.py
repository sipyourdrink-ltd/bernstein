"""Sync .sdd/backlog/*.yaml files with the task server.

Two tracking systems exist: static backlog files (.yaml or .md, authored by
humans or agents) and the dynamic task server (tasks.jsonl).  This module
bridges them:
- On demand or at startup, create server tasks for new backlog files.
- When a task is done on the server, move its backlog file to backlog/closed/.
"""

from __future__ import annotations

import logging
import re
import shutil
import unicodedata
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, cast

import httpx

from bernstein.core.backlog_parser import parse_backlog_path

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class BacklogTask:
    """Metadata parsed from a .sdd/backlog/open/*.yaml file.

    Attributes:
        title: Task title from the first ``# `` heading.
        description: Full file content.
        role: Agent role (e.g. ``backend``, ``qa``).
        priority: Numeric priority (1=critical, 2=normal, 3=nice-to-have).
        scope: Task scope (``small``, ``medium``, ``large``).
        complexity: Task complexity (``low``, ``medium``, ``high``).
        source_file: Basename of the originating backlog file.
    """

    title: str
    description: str
    role: str
    priority: int
    scope: str
    complexity: str
    source_file: str
    approval_required: bool = False


def parse_backlog_file(path: Path) -> BacklogTask | None:
    """Parse a backlog file (.yaml or .md) into a BacklogTask.

    Supports two formats:
    1. **YAML frontmatter** (``---`` delimited block at the top of the file).
    2. **Markdown bold fields** — lines like ``**Role:** backend``.

    Args:
        path: Path to the backlog file.

    Returns:
        Parsed BacklogTask, or None if the file cannot be parsed.
    """
    parsed = parse_backlog_path(path)
    if parsed is None:
        return None
    return BacklogTask(
        title=parsed.title,
        description=parsed.description,
        role=parsed.role,
        priority=parsed.priority,
        scope=parsed.scope,
        complexity=parsed.complexity,
        source_file=parsed.source_file,
        approval_required=parsed.require_human_approval,
    )


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
    ``"p0_c1_030426_feat_api-preconnect.yaml"`` → ``"api-preconnect"``

    Args:
        filename: Basename of the backlog file.

    Returns:
        Slug without leading number/prefix or file extension.
    """
    name = re.sub(r"^\d+-", "", filename)
    name = re.sub(r"^p\d+_c\d+_\d+_\w+_", "", name)
    name = re.sub(r"\.(md|yaml)$", "", name)
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
# Payload & fallback helpers
# ---------------------------------------------------------------------------


def _build_task_payload(task: BacklogTask) -> dict[str, Any]:
    """Build the JSON payload for creating a single task on the server.

    Embeds the source filename in the description for traceability.

    Args:
        task: Parsed backlog task.

    Returns:
        Dict suitable for ``POST /tasks`` or inclusion in a batch.
    """
    description = task.description
    if f"source: {task.source_file}" not in description:
        description = description + f"\n\n<!-- source: {task.source_file} -->"

    return {
        "title": task.title,
        "description": description,
        "role": task.role,
        "priority": task.priority,
        "scope": task.scope,
        "complexity": task.complexity,
        "approval_required": task.approval_required,
    }


def _sync_one_by_one(
    payloads: list[dict[str, Any]],
    files: list[Path],
    client: httpx.Client,
    server_url: str,
    result: SyncResult,
) -> None:
    """Create tasks one-by-one (fallback when ``/tasks/batch`` is unavailable).

    Args:
        payloads: Pre-built task payloads.
        files: Corresponding backlog file paths (same order as *payloads*).
        client: httpx client.
        server_url: Base URL of the task server.
        result: SyncResult to populate with created IDs or errors.
    """
    for payload, md_file in zip(payloads, files, strict=True):
        try:
            resp = client.post(f"{server_url}/tasks", json=payload)
            resp.raise_for_status()
            task_id: str = resp.json().get("id", "unknown")
            result.created.append(task_id)
            logger.info("Created task %s from %s", task_id, md_file.name)
        except httpx.HTTPError as exc:
            result.errors.append(f"Failed to create task from {md_file.name}: {exc}")


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
    """Sync ``.sdd/backlog/open/*.yaml`` files with the running task server.

    Steps:
    1. Scan ``backlog/open/`` for ``.yaml`` and ``.md`` files.
    2. For each file, create a task on the server if one with the same title
       does not already exist (fuzzy-matched via slug normalisation).
       The source filename is embedded in the description for traceability.
    3. For each ``done`` task on the server, move the matching backlog file
       from ``backlog/open/`` to ``backlog/closed/``.

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

        md_files = sorted([*backlog_open.glob("*.yaml"), *backlog_open.glob("*.md")])

        # --- Step 1: create new tasks (batched) ---
        batch_payloads: list[dict[str, Any]] = []
        batch_files: list[Path] = []

        for md_file in md_files:
            task = parse_backlog_file(md_file)
            if task is None:
                result.errors.append(f"Could not parse {md_file.name}")
                continue

            if _task_already_exists(task, existing_slugs):
                result.skipped.append(md_file.name)
                logger.debug("Skipping %s — task already on server", md_file.name)
                continue

            payload = _build_task_payload(task)
            batch_payloads.append(payload)
            batch_files.append(md_file)
            # Track slug immediately so later files in the same batch
            # with an identical title are deduplicated before sending.
            existing_slugs.add(normalise_title(task.title))

        if batch_payloads:
            try:
                resp = _client.post(
                    f"{server_url}/tasks/batch",
                    json={"tasks": batch_payloads},
                )
                resp.raise_for_status()
                data: dict[str, Any] = resp.json()
                for created_task in data.get("created", []):
                    task_id: str = created_task.get("id", "unknown")
                    result.created.append(task_id)
                    logger.info("Batch-created task %s", task_id)
                for title in data.get("skipped_titles", []):
                    result.skipped.append(title)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code == 404:
                    # Server doesn't support batch — fall back to one-by-one
                    _sync_one_by_one(
                        batch_payloads,
                        batch_files,
                        _client,
                        server_url,
                        result,
                    )
                else:
                    result.errors.append(f"Batch create failed: {exc}")
            except httpx.HTTPError as exc:
                result.errors.append(f"Batch create failed: {exc}")

        # --- Step 2: move files for completed tasks ---
        done_slugs: set[str] = {
            normalise_title(t.get("title", "")) for t in _get_tasks_by_status(_client, server_url, "closed")
        }

        backlog_claimed = workdir / ".sdd" / "backlog" / "claimed"
        scan_dirs = [backlog_open]
        if backlog_claimed.exists():
            scan_dirs.append(backlog_claimed)
        all_files: list[Path] = []
        for d in scan_dirs:
            all_files.extend(d.glob("*.yaml"))
            all_files.extend(d.glob("*.md"))
        for md_file in sorted(all_files):
            task = parse_backlog_file(md_file)
            if task is None:
                continue
            if normalise_title(task.title) in done_slugs:
                # Prefer closed/ over done/ (project convention)
                _closed_dir = workdir / ".sdd" / "backlog" / "closed"
                _closed_dir.mkdir(parents=True, exist_ok=True)
                dest = _closed_dir / md_file.name
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
