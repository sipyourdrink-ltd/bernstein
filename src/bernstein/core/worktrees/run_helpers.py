"""Capture agent-authored helpers as content-addressed run artefacts (#5322).

PR1 is capture and classification only. An agent-created file that was
executed during the run becomes a ``run_helper``: classified from journal
``file_create`` / ``file_execute`` events (read-only, deterministic), then
content-addressed into the CAS store with its origin step before the
worktree is reaped. Promotion / skill provenance is a follow-up PR.

Decision (stated for reviewers): a file that executed with a non-zero exit
code still counts as a helper. Reproduction harnesses are *supposed* to
fail; exit codes are recorded on the artefact so later promotion policy
can filter, but classification itself is ``created ∧ executed``.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from bernstein.core.persistence.cas_store import CASStore

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

logger = logging.getLogger(__name__)

JOURNAL_EVENT_FILE_CREATE = "file_create"
JOURNAL_EVENT_FILE_EXECUTE = "file_execute"
JOURNAL_EVENT_RUN_HELPER_CAPTURED = "run_helper_captured"

TRUST_CLASS_AGENT_AUTHORED = "agent_authored"


@dataclass(frozen=True, slots=True)
class RunHelper:
    """One executed, agent-created file classified from the run journal.

    Attributes:
        path: Worktree-relative POSIX path.
        origin_step: Journal ``index`` of the first ``file_create`` for
            this path (the origin step named on the run receipt).
        content_hash: CAS SHA-256 hex digest after capture; ``None`` from
            classify alone.
        execution_count: Number of matching ``file_execute`` events.
        exit_codes: Exit codes from those executes, in journal order.
        trust_class: Provenance class; agent-authored until promoted.
    """

    path: str
    origin_step: int
    content_hash: str | None
    execution_count: int
    exit_codes: tuple[int, ...]
    trust_class: str = TRUST_CLASS_AGENT_AUTHORED


def _normalize_relpath(raw: object) -> str | None:
    """Return a worktree-relative POSIX path, or ``None`` if unusable."""

    if not isinstance(raw, str):
        return None
    cleaned = raw.strip().replace("\\", "/")
    if not cleaned or cleaned.startswith("/") or cleaned.startswith("~"):
        return None
    parts = [p for p in cleaned.split("/") if p and p != "."]
    if not parts or any(p == ".." for p in parts):
        return None
    return "/".join(parts)


def classify_run_helpers(journal_events: Sequence[Mapping[str, Any]]) -> list[RunHelper]:
    """Classify run helpers from journal events alone.

    A helper is a path that has at least one ``file_create`` and at least
    one later ``file_execute``. Classification never probes the filesystem;
    exit codes (including non-zero) are recorded but do not exclude a path.

    Args:
        journal_events: Ordered journal rows (each with ``event`` and
            usually ``index`` / ``path`` / ``exit_code``).

    Returns:
        Deterministic list of :class:`RunHelper` sorted by ``origin_step``,
        then path. ``content_hash`` is always ``None`` here.
    """

    created: dict[str, int] = {}
    executions: dict[str, list[int]] = {}

    for position, row in enumerate(journal_events):
        event = row.get("event")
        path = _normalize_relpath(row.get("path"))
        if path is None:
            continue
        raw_index = row.get("index", position)
        try:
            index = int(raw_index)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            index = position

        if event == JOURNAL_EVENT_FILE_CREATE:
            created.setdefault(path, index)
            continue
        if event != JOURNAL_EVENT_FILE_EXECUTE:
            continue
        if path not in created:
            # Execute without a prior create in this journal is not an
            # agent-authored helper for this run (e.g. pre-existing tool).
            continue
        raw_exit = row.get("exit_code", 0)
        try:
            exit_code = int(raw_exit)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            exit_code = 0
        executions.setdefault(path, []).append(exit_code)

    helpers: list[RunHelper] = []
    for path, exit_codes in executions.items():
        helpers.append(
            RunHelper(
                path=path,
                origin_step=created[path],
                content_hash=None,
                execution_count=len(exit_codes),
                exit_codes=tuple(exit_codes),
            )
        )
    helpers.sort(key=lambda h: (h.origin_step, h.path))
    return helpers


def _resolve_helper_file(worktree_path: Path, relpath: str) -> Path | None:
    """Resolve ``relpath`` under ``worktree_path`` without escaping it."""

    candidate = (worktree_path / relpath).resolve()
    try:
        candidate.relative_to(worktree_path.resolve())
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate


def capture_run_helpers(
    worktree_path: Path,
    helpers: Sequence[RunHelper],
    cas: CASStore,
    *,
    journal: Any | None = None,
) -> list[RunHelper]:
    """Content-address helper file bytes into ``cas`` while the worktree exists.

    Args:
        worktree_path: Absolute worktree root (still present).
        helpers: Output of :func:`classify_run_helpers`.
        cas: Content-addressed store (typically ``.sdd/cas``).
        journal: Optional :class:`~bernstein.core.replay.journal.EventJournal`
            to append ``run_helper_captured`` naming rows for the run receipt.

    Returns:
        Helpers with ``content_hash`` filled. Missing files are skipped
        (logged) so GC is never wedged by a vanished path.
    """

    captured: list[RunHelper] = []
    for helper in helpers:
        file_path = _resolve_helper_file(worktree_path, helper.path)
        if file_path is None:
            logger.warning(
                "run_helper: skipping missing or escaped path %s under %s",
                helper.path,
                worktree_path,
            )
            continue
        try:
            data = file_path.read_bytes()
        except OSError as exc:
            logger.warning("run_helper: failed to read %s: %s", file_path, exc)
            continue
        digest = cas.put(
            data,
            content_type="application/octet-stream",
            metadata={
                "kind": "run_helper",
                "path": helper.path,
                "origin_step": helper.origin_step,
                "execution_count": helper.execution_count,
                "exit_codes": list(helper.exit_codes),
                "trust_class": helper.trust_class,
            },
        )
        filled = replace(helper, content_hash=digest)
        captured.append(filled)
        if journal is not None:
            try:
                journal.record(
                    JOURNAL_EVENT_RUN_HELPER_CAPTURED,
                    path=filled.path,
                    origin_step=filled.origin_step,
                    content_hash=digest,
                    execution_count=filled.execution_count,
                    exit_codes=list(filled.exit_codes),
                    trust_class=filled.trust_class,
                )
            except Exception as exc:  # boundary: never block reap on journal
                logger.warning(
                    "run_helper: failed to name helper on journal for %s: %s",
                    filled.path,
                    exc,
                )
    return captured


def resolve_run_id_from_pid_record(repo_root: Path, session_id: str) -> str | None:
    """Return ``run_id`` from the session PID record, if present."""

    pid_file = repo_root / ".sdd" / "runtime" / "pids" / f"{session_id}.json"
    if not pid_file.is_file():
        return None
    try:
        data = json.loads(pid_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    run_id = data.get("run_id")
    if isinstance(run_id, str) and run_id.strip():
        return run_id.strip()
    return None


def load_journal_events_for_run(sdd_dir: Path, run_id: str) -> list[dict[str, Any]]:
    """Load journal events for ``run_id``, or ``[]`` when absent/unreadable."""

    from bernstein.core.replay.journal import JournalPathError, load_events, run_journal_path

    try:
        path = run_journal_path(sdd_dir, run_id)
    except JournalPathError:
        return []
    try:
        return list(load_events(path).events)
    except OSError as exc:
        logger.warning("run_helper: failed to load journal for run %s: %s", run_id, exc)
        return []


def capture_helpers_before_reap(
    repo_root: Path,
    worktree_path: Path,
    session_id: str,
    *,
    dry_run: bool = False,
) -> list[RunHelper]:
    """Classify + CAS-capture helpers for one worktree before it is deleted.

    Best-effort: any failure logs and returns what was captured so GC never
    wedges. Skipped entirely on ``dry_run``.
    """

    if dry_run:
        return []
    sdd_dir = repo_root / ".sdd"
    run_id = resolve_run_id_from_pid_record(repo_root, session_id)
    if run_id is None:
        return []
    events = load_journal_events_for_run(sdd_dir, run_id)
    helpers = classify_run_helpers(events)
    if not helpers:
        return []
    cas = CASStore(sdd_dir / "cas")
    journal = None
    try:
        from bernstein.core.replay.journal import EventJournal, JournalPathError

        journal = EventJournal.resume(run_id, sdd_dir)
    except (JournalPathError, OSError) as exc:  # boundary: missing / unreadable journal
        logger.debug("run_helper: no journal to name helpers for run %s: %s", run_id, exc)
    try:
        return capture_run_helpers(worktree_path, helpers, cas, journal=journal)
    except Exception as exc:  # boundary
        logger.warning("run_helper: capture failed for %s: %s", worktree_path, exc)
        return []


__all__ = [
    "JOURNAL_EVENT_FILE_CREATE",
    "JOURNAL_EVENT_FILE_EXECUTE",
    "JOURNAL_EVENT_RUN_HELPER_CAPTURED",
    "TRUST_CLASS_AGENT_AUTHORED",
    "RunHelper",
    "capture_helpers_before_reap",
    "capture_run_helpers",
    "classify_run_helpers",
    "load_journal_events_for_run",
    "resolve_run_id_from_pid_record",
]
