"""Detect and import existing project workflow files as Bernstein tasks.

Scans for TODO.md, TASKS.md, and .plan files in the project root and
converts unchecked checkbox items into open Bernstein tasks. This lets
Bernstein augment — rather than replace — the user's existing task system.
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    import httpx

logger = logging.getLogger(__name__)

# File names to detect (checked in project root only)
_WORKFLOW_FILENAMES: list[str] = [
    "TODO.md",
    "TASKS.md",
    "todo.md",
    "tasks.md",
    ".plan",
]

# Matches unchecked Markdown checkbox items: `- [ ] description`
_CHECKBOX_RE = re.compile(r"^\s*[-*]\s+\[\s+\]\s+(.+)$")

# Sentinel stored in task metadata so we can detect already-imported items
_IMPORT_SOURCE_KEY = "workflow_import_source"


def detect_workflow_files(workdir: Path) -> list[Path]:
    """Return existing workflow/task files found in *workdir*.

    Only the immediate project root is scanned — subdirectory files are
    intentionally ignored to avoid picking up third-party task lists.

    Args:
        workdir: Project root directory.

    Returns:
        List of discovered workflow file paths, in detection order.
    """
    import os

    found: list[Path] = []
    seen_inodes: set[tuple[int, int]] = set()  # (device, inode) pairs
    for name in _WORKFLOW_FILENAMES:
        candidate = workdir / name
        if candidate.is_file():
            try:
                st = os.stat(candidate)
                inode_key = (st.st_dev, st.st_ino)
            except OSError:
                continue
            if inode_key not in seen_inodes:
                seen_inodes.add(inode_key)
                found.append(candidate)
    return found


def parse_markdown_tasks(content: str) -> list[str]:
    """Extract unchecked todo items from Markdown content.

    Parses lines matching ``- [ ] task description`` (or ``* [ ] …``).
    Checked items (``- [x]`` / ``- [X]``) are ignored so completed work
    is not re-imported.

    Args:
        content: Full text of a Markdown workflow file.

    Returns:
        Ordered list of task description strings for unchecked items.
    """
    tasks: list[str] = []
    for line in content.splitlines():
        match = _CHECKBOX_RE.match(line)
        if match:
            text = match.group(1).strip()
            if text:
                tasks.append(text)
    return tasks


def import_workflow_tasks(
    workdir: Path,
    client: httpx.Client,
    base_url: str,
    *,
    dry_run: bool = False,
) -> int:
    """Import unchecked todo items from workflow files as open Bernstein tasks.

    Scans *workdir* for TODO.md, TASKS.md, and ``.plan`` files, parses
    unchecked checkbox items, and POSTs them to the task server. Tasks
    whose titles already exist in the server (case-insensitive) are
    skipped to prevent duplicates on repeated runs.

    Args:
        workdir: Project root directory to scan.
        client: httpx client connected to the task server.
        base_url: Base URL of the task server (e.g. ``http://127.0.0.1:8052``).
        dry_run: When True, detect and log items but do not create tasks.

    Returns:
        Number of tasks imported (or that *would* be imported in dry-run mode).
    """
    workflow_files = detect_workflow_files(workdir)
    if not workflow_files:
        return 0

    logger.info(
        "workflow_importer: detected %d workflow file(s): %s",
        len(workflow_files),
        [f.name for f in workflow_files],
    )

    # Collect existing task titles from the server to skip duplicates.
    existing_titles: set[str] = set()
    try:
        resp = client.get(f"{base_url}/tasks", params={"limit": 500, "offset": 0})
        if resp.is_success:
            body = resp.json()
            tasks_raw = body.get("tasks", body) if isinstance(body, dict) else body
            for t in tasks_raw:
                if isinstance(t, dict) and t.get("title"):
                    existing_titles.add(str(t["title"]).strip().lower())
    except Exception as exc:
        logger.warning("workflow_importer: could not fetch existing tasks: %s", exc)

    imported = 0
    for wf_file in workflow_files:
        try:
            content = wf_file.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            logger.warning("workflow_importer: cannot read %s: %s", wf_file.name, exc)
            continue

        items = parse_markdown_tasks(content)
        logger.info(
            "workflow_importer: %s → %d unchecked item(s)",
            wf_file.name,
            len(items),
        )

        for title in items:
            if title.strip().lower() in existing_titles:
                logger.debug("workflow_importer: skipping duplicate %r", title)
                continue

            if dry_run:
                logger.info("workflow_importer [dry-run]: would import %r", title)
                imported += 1
                continue

            payload: dict[str, object] = {
                "title": title,
                "description": f"Imported from {wf_file.name}: {title}",
                "role": "backend",
                "priority": 2,
                "scope": "medium",
                "complexity": "medium",
                "metadata": {_IMPORT_SOURCE_KEY: wf_file.name},
            }
            try:
                post_resp = client.post(f"{base_url}/tasks", json=payload)
                post_resp.raise_for_status()
                existing_titles.add(title.strip().lower())
                imported += 1
                logger.info("workflow_importer: imported %r from %s", title, wf_file.name)
            except Exception as exc:
                logger.warning(
                    "workflow_importer: failed to import %r: %s",
                    title,
                    exc,
                )

    if imported:
        logger.info("workflow_importer: imported %d task(s) total", imported)
    return imported
