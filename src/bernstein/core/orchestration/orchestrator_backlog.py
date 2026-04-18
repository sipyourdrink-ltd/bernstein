"""Orchestrator backlog management: file-based task ingestion.

Extracted from orchestrator.py as part of ORCH-009 decomposition.
Functions here operate on an Orchestrator instance passed as the first
argument, keeping the Orchestrator class as the public facade.
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Any

import httpx

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.backlog_parser import ParsedBacklogTask

logger = logging.getLogger(__name__)


def _collect_backlog_files(orch: Any) -> list[Path]:
    """Collect and filter backlog files from open/ and issues/ directories.

    Args:
        orch: The orchestrator instance.

    Returns:
        Sorted list of backlog file paths (filtered by BERNSTEIN_TASK_FILTER if set).
    """
    import os

    open_dir = orch._workdir / ".sdd" / "backlog" / "open"
    issues_dir = orch._workdir / ".sdd" / "backlog" / "issues"

    files: list[Path] = []
    for src_dir in (open_dir, issues_dir):
        if src_dir.exists():
            files.extend(src_dir.glob("*.md"))
            files.extend(src_dir.glob("*.yaml"))
            files.extend(src_dir.glob("*.yml"))
    files.sort()

    task_filter = os.environ.get("BERNSTEIN_TASK_FILTER")
    if task_filter:
        task_filter_lower = task_filter.lower()
        files = [f for f in files if task_filter_lower in f.name.lower()]
        logger.info("Task filter '%s' matched %d backlog file(s)", task_filter, len(files))
    return files


def _ensure_ingested_titles(orch: Any) -> set[str]:
    """Ensure the ingested-titles dedup set is initialized and return it.

    On first call, seeds from existing server tasks.

    Args:
        orch: The orchestrator instance.

    Returns:
        Set of lowered, stripped task titles already on the server.
    """
    if not hasattr(orch, "_ingested_titles"):
        orch._ingested_titles: set[str] = set()
        try:
            resp = orch._client.get(f"{orch._config.server_url}/tasks")
            resp.raise_for_status()
            for task in resp.json():
                title = task.get("title", "")
                if title:
                    orch._ingested_titles.add(title.lower().strip())
        except Exception:
            pass
    return orch._ingested_titles


def _parse_candidates(
    orch: Any,
    backlog_files: list[Path],
    open_dir: Path,
    claimed_dir: Path,
    existing_titles: set[str],
) -> list[tuple[Path, ParsedBacklogTask]]:
    """Parse backlog files, filter duplicates, return sorted candidates.

    Args:
        orch: Orchestrator instance.
        backlog_files: Files to parse.
        open_dir: Open backlog directory.
        claimed_dir: Claimed backlog directory.
        existing_titles: Titles already ingested.

    Returns:
        Priority-sorted list of (path, parsed_task) tuples.
    """
    from bernstein.core.backlog_parser import parse_backlog_text

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

    candidates.sort(key=lambda t: t[1].priority)
    return candidates


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
    claimed_dir = orch._workdir / ".sdd" / "backlog" / "claimed"

    backlog_files = _collect_backlog_files(orch)
    if not backlog_files:
        return 0

    _MAX_INGEST_PER_TICK = 50

    existing_titles = _ensure_ingested_titles(orch)
    claimed_dir.mkdir(parents=True, exist_ok=True)

    candidates = _parse_candidates(orch, backlog_files, open_dir, claimed_dir, existing_titles)
    batch_files = candidates[:_MAX_INGEST_PER_TICK]

    if not batch_files:
        return 0

    payloads = [parsed.to_task_payload() for _, parsed in batch_files]
    try:
        resp = orch._client.post(
            f"{orch._config.server_url}/tasks/batch",
            json={"tasks": payloads},
        )
        resp.raise_for_status()
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (404, 422):
            # 404: older server build without the batch endpoint.
            # 422: one task in the batch fails pydantic validation — fall
            # back so valid tasks still land instead of poisoning the whole
            # batch every tick.
            return _ingest_backlog_one_by_one(orch, batch_files, open_dir, claimed_dir)
        logger.warning("ingest_backlog: batch POST failed: %s", exc)
        return 0
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


def _claim_backlog_file(_orch: Any, backlog_file: Path, open_dir: Path, claimed_dir: Path) -> None:
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
