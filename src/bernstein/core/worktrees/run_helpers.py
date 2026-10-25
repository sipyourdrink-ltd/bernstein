"""Capture agent-authored helpers as content-addressed run artefacts (#5322).

This module is capture and classification only. An agent-created file that was
executed during the run is a *run helper*: it is classified from the run
journal's ``file_create`` / ``file_execute`` rows alone (read-only and
deterministic, the discipline :mod:`bernstein.core.worktrees.classifier`
applies to whole worktrees), and its bytes are content-addressed into
``.sdd/cas`` while the worktree still exists.

Each capture is named by a :class:`RunHelperRecord` appended to
``.sdd/runs/<run_id>/run_helpers.jsonl``. The run journal itself is never
written: a finalized run has sealed its journal head, and extending it from a
later process would make ``bernstein seal`` refuse the run. The record is also
stored in the CAS under its own digest, which covers the path, origin step and
exit codes, so two helpers with identical bytes keep separate records (the
CAS skips metadata on a dedup hit, so blob metadata cannot carry them).

A file that executed and exited non-zero still counts as a helper: a
reproduction harness is supposed to fail. Exit codes are recorded so a later
promotion policy can filter on them; an execution with no usable exit code is
recorded as ``None`` (unknown), never as ``0``. ``-1`` is not used because the
spawner already uses it for an abnormally terminated session.

Not wired into ``bernstein worktrees gc`` yet: no adapter emits
``file_create`` / ``file_execute`` and nothing maps a reaped worktree to its
run id. :func:`capture_helpers_for_run` is the seam the gc sweep will call
once both exist. Promotion to a skill is separate work.
"""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.persistence.cas_store import CASStore
from bernstein.core.replay.journal import JournalPathError, load_events, run_journal_path

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence
    from pathlib import Path

logger = logging.getLogger(__name__)

JOURNAL_EVENT_FILE_CREATE = "file_create"
JOURNAL_EVENT_FILE_EXECUTE = "file_execute"

TRUST_CLASS_AGENT_AUTHORED = "agent_authored"
RUN_HELPER_KIND = "run_helper"
RUN_HELPERS_FILENAME = "run_helpers.jsonl"


@dataclass(frozen=True, slots=True)
class RunHelper:
    """One executed, agent-created file classified from the run journal.

    Attributes:
        path: Worktree-relative POSIX path.
        origin_step: Journal ``index`` of the first ``file_create`` for the path.
        execution_count: Number of ``file_execute`` rows after that create.
        exit_codes: Exit code of each of those executions, in journal order;
            ``None`` where the row carried no integer exit code.
        trust_class: Provenance class; agent-authored until promoted.
    """

    path: str
    origin_step: int
    execution_count: int
    exit_codes: tuple[int | None, ...]
    trust_class: str = TRUST_CLASS_AGENT_AUTHORED


@dataclass(frozen=True, slots=True)
class RunHelperRecord:
    """A captured run helper, as named on the run's ``run_helpers.jsonl``.

    Attributes:
        run_id: The run whose journal classified the helper.
        journal_head: ``event_hash`` of the last journal row read, so the
            classification can be recomputed against the same journal prefix.
        path, origin_step, execution_count, exit_codes, trust_class: As on
            :class:`RunHelper`.
        content_hash: SHA-256 hex digest of the file bytes (the CAS key).
    """

    run_id: str
    journal_head: str
    path: str
    origin_step: int
    execution_count: int
    exit_codes: tuple[int | None, ...]
    trust_class: str
    content_hash: str

    def to_dict(self) -> dict[str, Any]:
        """Return the record as a JSON-ready mapping."""
        return {
            "kind": RUN_HELPER_KIND,
            "run_id": self.run_id,
            "journal_head": self.journal_head,
            "path": self.path,
            "origin_step": self.origin_step,
            "execution_count": self.execution_count,
            "exit_codes": list(self.exit_codes),
            "trust_class": self.trust_class,
            "content_hash": self.content_hash,
        }

    def canonical_bytes(self) -> bytes:
        """Return the sorted-key, compact UTF-8 JSON the record hash covers."""
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")

    @property
    def record_hash(self) -> str:
        """SHA-256 hex digest of :meth:`canonical_bytes` (also its CAS key)."""
        return hashlib.sha256(self.canonical_bytes()).hexdigest()

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> RunHelperRecord:
        """Rebuild a record from :meth:`to_dict` output.

        Raises:
            KeyError, TypeError, ValueError: When ``data`` is not a record.
        """
        if data["kind"] != RUN_HELPER_KIND:
            raise ValueError(f"not a run helper record: {data['kind']!r}")
        return cls(
            run_id=str(data["run_id"]),
            journal_head=str(data["journal_head"]),
            path=str(data["path"]),
            origin_step=int(data["origin_step"]),
            execution_count=int(data["execution_count"]),
            exit_codes=tuple(None if code is None else int(code) for code in data["exit_codes"]),
            trust_class=str(data["trust_class"]),
            content_hash=str(data["content_hash"]),
        )


def _normalize_relpath(raw: object) -> str | None:
    """Return a worktree-relative POSIX path, or ``None`` if it can leave the worktree."""
    if not isinstance(raw, str):
        return None
    cleaned = raw.strip().replace("\\", "/")
    if not cleaned or cleaned.startswith(("/", "~")):
        return None
    if len(cleaned) >= 2 and cleaned[1] == ":":
        return None
    parts = [p for p in cleaned.split("/") if p and p != "."]
    if not parts or ".." in parts:
        return None
    return "/".join(parts)


def _exit_code(row: Mapping[str, Any]) -> int | None:
    raw = row.get("exit_code")
    if isinstance(raw, bool) or not isinstance(raw, int):
        return None
    return raw


def classify_run_helpers(journal_events: Sequence[Mapping[str, Any]]) -> list[RunHelper]:
    """Classify run helpers from journal rows alone.

    A helper is a path with a ``file_create`` row followed by at least one
    ``file_execute`` row. An execute with no earlier create in the same
    journal (a pre-existing tool) is not a helper. The filesystem is never
    consulted.

    Args:
        journal_events: Journal rows in journal order.

    Returns:
        Helpers sorted by ``(origin_step, path)``.
    """
    created: dict[str, int] = {}
    executions: dict[str, list[int | None]] = {}

    for position, row in enumerate(journal_events):
        event = row.get("event")
        if event not in {JOURNAL_EVENT_FILE_CREATE, JOURNAL_EVENT_FILE_EXECUTE}:
            continue
        path = _normalize_relpath(row.get("path"))
        if path is None:
            continue
        raw_index = row.get("index")
        index = raw_index if isinstance(raw_index, int) and not isinstance(raw_index, bool) else position
        if event == JOURNAL_EVENT_FILE_CREATE:
            created.setdefault(path, index)
        elif path in created:
            executions.setdefault(path, []).append(_exit_code(row))

    helpers = [
        RunHelper(
            path=path,
            origin_step=created[path],
            execution_count=len(codes),
            exit_codes=tuple(codes),
        )
        for path, codes in executions.items()
    ]
    helpers.sort(key=lambda h: (h.origin_step, h.path))
    return helpers


def _resolve_helper_file(worktree_path: Path, relpath: str) -> Path | None:
    """Resolve ``relpath`` under ``worktree_path``; ``None`` if it escapes or is not a file."""
    candidate = (worktree_path / relpath).resolve()
    try:
        candidate.relative_to(worktree_path.resolve())
    except ValueError:
        return None
    if not candidate.is_file():
        return None
    return candidate


def run_helpers_path(sdd_dir: Path, run_id: str) -> Path:
    """Return ``.sdd/runs/<run_id>/run_helpers.jsonl``, beside the run journal.

    Raises:
        JournalPathError: When ``run_id`` is not a safe path segment.
    """
    return run_journal_path(sdd_dir, run_id).parent / RUN_HELPERS_FILENAME


def capture_run_helpers(
    worktree_path: Path,
    helpers: Sequence[RunHelper],
    *,
    cas: CASStore,
    sidecar: Path,
    run_id: str,
    journal_head: str,
) -> list[RunHelperRecord]:
    """Content-address each helper's bytes and append its record to ``sidecar``.

    A helper whose file is missing, escapes the worktree, cannot be read, or
    cannot be stored is logged and skipped; the rest are still captured.

    Returns:
        The records written, in ``helpers`` order.
    """
    records: list[RunHelperRecord] = []
    for helper in helpers:
        file_path = _resolve_helper_file(worktree_path, helper.path)
        if file_path is None:
            logger.warning("run_helper: skipping missing or escaping path %s under %s", helper.path, worktree_path)
            continue
        try:
            content_hash = cas.put(file_path.read_bytes(), metadata={"kind": RUN_HELPER_KIND})
            record = RunHelperRecord(
                run_id=run_id,
                journal_head=journal_head,
                path=helper.path,
                origin_step=helper.origin_step,
                execution_count=helper.execution_count,
                exit_codes=helper.exit_codes,
                trust_class=helper.trust_class,
                content_hash=content_hash,
            )
            cas.put(record.canonical_bytes(), content_type="application/json")
            row = {"record": record.to_dict(), "record_hash": record.record_hash}
            sidecar.parent.mkdir(parents=True, exist_ok=True)
            with sidecar.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(row, ensure_ascii=False, sort_keys=True) + "\n")
        except Exception as exc:  # boundary: one helper's failure must not lose the rest
            logger.warning("run_helper: failed to capture %s: %s", file_path, exc)
            continue
        records.append(record)
    return records


def capture_helpers_for_run(repo_root: Path, worktree_path: Path, run_id: str) -> list[RunHelperRecord]:
    """Classify and capture one run's helpers from ``worktree_path``.

    Never raises: a capture failure must not block the caller (the gc sweep,
    once wired), so any error is logged at warning level and the records
    captured so far are returned.
    """
    sdd_dir = repo_root / ".sdd"
    try:
        sidecar = run_helpers_path(sdd_dir, run_id)
        journal_file = run_journal_path(sdd_dir, run_id)
    except JournalPathError as exc:
        logger.warning("run_helper: not capturing for unsafe run id %r: %s", run_id, exc)
        return []
    try:
        loaded = load_events(journal_file)
    except OSError as exc:
        logger.warning("run_helper: cannot read journal for run %s: %s", run_id, exc)
        return []
    if loaded.discarded_count:
        logger.warning(
            "run_helper: journal for run %s has %d unreadable line(s); classifying the rest",
            run_id,
            loaded.discarded_count,
        )
    helpers = classify_run_helpers(loaded.events)
    if not helpers:
        return []
    journal_head = str(loaded.events[-1].get("event_hash", ""))
    try:
        return capture_run_helpers(
            worktree_path,
            helpers,
            cas=CASStore(sdd_dir / "cas"),
            sidecar=sidecar,
            run_id=run_id,
            journal_head=journal_head,
        )
    except Exception as exc:  # boundary: capture must never block the caller
        logger.warning("run_helper: capture failed for run %s under %s: %s", run_id, worktree_path, exc)
        return []


def read_run_helper_records(sdd_dir: Path, run_id: str) -> list[RunHelperRecord]:
    """Return the run's helper records whose ``record_hash`` recomputes.

    A row that does not parse, or whose record no longer hashes to its
    ``record_hash``, is logged and left out. The hash is unkeyed: it catches
    an edited row, not a forger who rewrites the hash too.
    """
    try:
        path = run_helpers_path(sdd_dir, run_id)
    except JournalPathError:
        return []
    if not path.is_file():
        return []
    records: list[RunHelperRecord] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
            record = RunHelperRecord.from_dict(row["record"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError):
            logger.warning("run_helper: skipping malformed row in %s", path)
            continue
        if record.record_hash != row.get("record_hash"):
            logger.warning("run_helper: skipping row for %s whose record hash does not recompute", record.path)
            continue
        records.append(record)
    return records


__all__ = [
    "JOURNAL_EVENT_FILE_CREATE",
    "JOURNAL_EVENT_FILE_EXECUTE",
    "RUN_HELPERS_FILENAME",
    "RUN_HELPER_KIND",
    "TRUST_CLASS_AGENT_AUTHORED",
    "RunHelper",
    "RunHelperRecord",
    "capture_helpers_for_run",
    "capture_run_helpers",
    "classify_run_helpers",
    "read_run_helper_records",
    "run_helpers_path",
]
