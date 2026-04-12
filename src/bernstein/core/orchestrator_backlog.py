"""Orchestrator backlog management: file-based task ingestion.

Extracted from orchestrator.py as part of ORCH-009 decomposition.
Functions here operate on an Orchestrator instance passed as the first
argument, keeping the Orchestrator class as the public facade.
"""

from __future__ import annotations

import contextlib
import logging
import re
import time
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.backlog_parser import ParsedBacklogTask
    from bernstein.core.models import Task

logger = logging.getLogger(__name__)


def sync_backlog_file(orch: Any, task: Task) -> None:
    """Move the matching .md file from backlog/open/ to backlog/closed/.

    Args:
        orch: The orchestrator instance.
        task: The completed task to sync.
    """
    open_dir = orch._workdir / ".sdd" / "backlog" / "open"
    if not open_dir.exists():
        return

    closed_dir = orch._workdir / ".sdd" / "backlog" / "closed"
    closed_dir.mkdir(parents=True, exist_ok=True)

    title_words = backlog_words_from_title(task.title)

    best_match: str | None = None
    best_score = 0
    for md_file in open_dir.glob("*.md"):
        slug = re.sub(r"^\d+-", "", md_file.name[:-3])
        file_words = set(slug.split("-"))
        significant_file_words = {w for w in file_words if len(w) >= 4}
        overlap = title_words & significant_file_words
        if overlap and len(overlap) > best_score:
            best_score = len(overlap)
            best_match = md_file.name

    if best_match is None:
        return

    src = open_dir / best_match
    dst = closed_dir / best_match
    if not src.exists():
        return

    content = src.read_text(encoding="utf-8")
    ts = time.strftime("%Y-%m-%d %H:%M:%S")
    summary = task.result_summary or ""
    content += f"\n\n---\n**completed**: {ts}\n**task_id**: {task.id}\n**result**: {summary}\n"
    dst.write_text(content, encoding="utf-8")
    src.unlink()
    logger.info("Synced backlog: %s -> closed/", best_match)


def ingest_backlog(orch: Any) -> int:
    """Scan .sdd/backlog/open/ and .sdd/backlog/issues/ for new task files.

    Both directories are scanned so that GitHub-synced P0/P1 tickets
    (in ``issues/``) are ingested alongside internal backlog (``open/``).
    Candidates are sorted by priority so P0 tasks are ingested first.

    - ``open/`` files are **moved** to ``claimed/`` after ingestion.
    - ``issues/`` files stay in place; a marker is created in ``claimed/``
      to prevent re-ingestion.

    Args:
        orch: The orchestrator instance.

    Returns:
        Number of files ingested this call.
    """
    open_dir = orch._workdir / ".sdd" / "backlog" / "open"
    issues_dir = orch._workdir / ".sdd" / "backlog" / "issues"
    claimed_dir = orch._workdir / ".sdd" / "backlog" / "claimed"

    # Collect .md, .yaml, .yml from both directories
    backlog_files: list[Path] = []
    for src_dir in (open_dir, issues_dir):
        if src_dir.exists():
            backlog_files.extend(src_dir.glob("*.md"))
            backlog_files.extend(src_dir.glob("*.yaml"))
            backlog_files.extend(src_dir.glob("*.yml"))
    backlog_files.sort()

    if not backlog_files:
        return 0

    # Rate-limit ingestion: max 50 files per tick to prevent server overload.
    # With 700+ backlog files, posting them all in one tick crashes the server.
    _MAX_INGEST_PER_TICK = 50

    # Build title dedup set from existing server tasks (if available).
    # Prevents re-creating tasks that already exist on the server after a
    # restart or crash that prevented file move to claimed/.
    existing_titles: set[str] = set()
    if not hasattr(orch, "_ingested_titles"):
        orch._ingested_titles: set[str] = set()
        # Seed from server on first call
        try:
            resp = orch._client.get(f"{orch._config.server_url}/tasks")
            resp.raise_for_status()
            for task in resp.json():
                title = task.get("title", "")
                if title:
                    orch._ingested_titles.add(title.lower().strip())
        except Exception:
            pass  # Server may be starting up; dedup will be best-effort
    existing_titles = orch._ingested_titles

    claimed_dir.mkdir(parents=True, exist_ok=True)

    from bernstein.core.backlog_parser import parse_backlog_text

    # Phase 1: Parse all candidates, filter dupes, sort by priority
    candidates: list[tuple[Path, ParsedBacklogTask]] = []
    for backlog_file in backlog_files:
        if (claimed_dir / backlog_file.name).exists():
            continue

        content = backlog_file.read_text(encoding="utf-8")
        parsed_task = parse_backlog_text(backlog_file.name, content)
        if parsed_task is None:
            logger.warning("ingest_backlog: could not parse %s — skipping", backlog_file.name)
            _claim_backlog_file(orch, backlog_file, open_dir, claimed_dir)
            continue

        title_key = parsed_task.title.lower().strip()
        if title_key in existing_titles:
            _claim_backlog_file(orch, backlog_file, open_dir, claimed_dir)
            continue

        candidates.append((backlog_file, parsed_task))

    # Sort by priority (lower = more critical) so P0 tasks are ingested first
    candidates.sort(key=lambda t: t[1].priority)
    batch_files = candidates[:_MAX_INGEST_PER_TICK]

    if not batch_files:
        return 0

    # Phase 2: POST batch — single HTTP call for all collected tasks
    payloads = [parsed.to_task_payload() for _, parsed in batch_files]
    try:
        resp = orch._client.post(
            f"{orch._config.server_url}/tasks/batch",
            json={"tasks": payloads},
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code == 404:
            # Server doesn't support batch yet — fall back to one-by-one
            return _ingest_backlog_one_by_one(orch, batch_files, open_dir, claimed_dir)
        logger.warning("ingest_backlog: batch POST failed: %s", exc)
        return 0  # Move NONE on failure
    except httpx.HTTPError as exc:
        logger.warning("ingest_backlog: batch POST failed: %s", exc)
        return 0

    # Phase 3: Mark files as claimed — only on success
    count = 0
    for backlog_file, parsed in batch_files:
        title_key = parsed.title.lower().strip()
        existing_titles.add(title_key)
        _claim_backlog_file(orch, backlog_file, open_dir, claimed_dir)
        count += 1
        logger.info("Ingested backlog file: %s (from %s/)", backlog_file.name, backlog_file.parent.name)

    return count


def _claim_backlog_file(orch: Any, backlog_file: Path, open_dir: Path, claimed_dir: Path) -> None:
    """Mark a backlog file as claimed.

    Files from ``open/`` are moved into ``claimed/``.
    Files from ``issues/`` stay in place — only a marker is created in
    ``claimed/`` so they are not re-ingested.

    Args:
        orch: The orchestrator instance.
        backlog_file: Path to the backlog file.
        open_dir: Path to the open backlog directory.
        claimed_dir: Path to the claimed backlog directory.
    """
    with contextlib.suppress(OSError):
        if backlog_file.parent == open_dir:
            backlog_file.rename(claimed_dir / backlog_file.name)
        else:
            (claimed_dir / backlog_file.name).touch()


def _ingest_backlog_one_by_one(
    orch: Any,
    batch_files: list[tuple[Path, ParsedBacklogTask]],
    open_dir: Path,
    claimed_dir: Path,
) -> int:
    """Fallback: ingest files one-by-one when server lacks batch endpoint.

    Args:
        orch: The orchestrator instance.
        batch_files: List of (path, parsed_task) tuples to ingest.
        open_dir: Path to the open backlog directory.
        claimed_dir: Path to the claimed backlog directory.

    Returns:
        Number of files ingested.
    """
    count = 0
    for backlog_file, parsed in batch_files:
        payload = parsed.to_task_payload()
        try:
            resp = orch._client.post(
                f"{orch._config.server_url}/tasks",
                json=payload,
            )
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            logger.warning(
                "ingest_backlog: POST failed for %s: %s",
                backlog_file.name,
                exc,
            )
            continue  # Skip this file, try next

        orch._ingested_titles.add(parsed.title.lower().strip())
        _claim_backlog_file(orch, backlog_file, open_dir, claimed_dir)
        count += 1
        logger.info("Ingested backlog file (one-by-one): %s", backlog_file.name)
    return count


def backlog_words_from_title(title: str) -> set[str]:
    """Extract significant lowercase words (>=4 chars) from a task title.

    Args:
        title: Task title string.

    Returns:
        Set of significant lowercase words.
    """
    expanded = re.sub(r"([a-z])([A-Z])", r"\1 \2", title)
    tokens = re.split(r"[^a-zA-Z0-9]+", expanded.lower())
    return {w for w in tokens if len(w) >= 4}
