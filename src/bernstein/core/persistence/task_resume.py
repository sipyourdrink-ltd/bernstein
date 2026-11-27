"""Per-task resume checkpoints - pick up paused/killed/crashed tasks.

This module is the storage layer for ``bernstein resume <task-id>``. It is
deliberately narrow: a single JSON file per task captured after every
successful step transition, recording everything we need to rebuild the run
context without re-walking the trace from scratch.

Layout::

    .sdd/runtime/checkpoints/<task-id>/checkpoint.json

Schema (versioned via :data:`SCHEMA_VERSION`):

* ``task_id`` - identifies the task this checkpoint belongs to.
* ``last_completed_step_id`` - last step the orchestrator confirmed done.
* ``trace_cursor`` - byte offset into ``.sdd/traces/<task-id>.jsonl``
  marking the last replayed event.
* ``scratchpad_path`` / ``scratchpad_sha256`` - pointer + content hash for
  the recovered scratchpad. The hash is recorded so a downstream resume
  can detect post-checkpoint scratchpad tampering.
* ``adapter`` / ``adapter_session_id`` - adapter name and the session
  identifier the adapter handed us when the task was first spawned.
* ``worktree_path`` - absolute path to the preserved worktree (``v1``
  scope is local-only, so this is intentionally a literal path).
* ``resume_count`` - how many times this task has been resumed; the CLI
  bumps it before re-spawning so flaky tasks are visible in the dashboard.
* ``stall_reason`` - *optional*. Set when the checkpoint was written at an
  automatic stall-kill boundary (heartbeat staleness or identical-progress
  detection) rather than after a normal step completion; ``None`` for the
  latter. Value is a :class:`~bernstein.core.orchestration.supervisor_receipt.StallReason`
  string.
* ``merge_cursor`` - *optional* streaming-merge cursor handed in by
  :mod:`bernstein.core.streaming_merge`. Coordination with the merge
  pipeline is a follow-up; we capture the shape now so adopters do not
  have to migrate later.
* ``meta`` - adapter-opaque key/value bag (model, role, etc.). Adapters
  with a ``resume()`` capability can stash anything they need here.
* ``created_at`` / ``updated_at`` - UTC ISO-8601 timestamps.

The on-disk write is atomic: we write to ``checkpoint.json.tmp`` then
``os.replace`` so a kill during persist cannot leave a partial file.

Out of scope (per spec):
    * Cross-machine resume (paths are local).
    * Distributed checkpoint storage (no S3 / object-store backend).
    * Resume across role-definition changes.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Any, cast

from pydantic import BaseModel, ConfigDict, Field, ValidationError

if TYPE_CHECKING:
    from collections.abc import Callable

from bernstein.core.persistence.atomic_write import write_atomic_json
from bernstein.core.security.path_containment import PathContainmentError

logger = logging.getLogger(__name__)

__all__ = [
    "CHECKPOINTS_SUBDIR",
    "CHECKPOINT_FILENAME",
    "RESUME_REQUESTS_SUFFIX",
    "SCHEMA_VERSION",
    "SOURCE_TASK_CHECKPOINT",
    "SOURCE_WORK_LEDGER",
    "CheckpointCorruptError",
    "CheckpointMissingError",
    "ResumeDispatchStatus",
    "ResumeRequestCorruptError",
    "ResumeRequestRecord",
    "TaskResumeCheckpoint",
    "bump_resume_count",
    "checkpoint_dir_for",
    "checkpoint_path_for",
    "checkpoint_source_digest",
    "claim_resume_request",
    "consume_resume_request",
    "ledger_source_digest",
    "load_checkpoint",
    "load_resume_request",
    "publish_resume_request",
    "read_resume_requests",
    "record_resume_outcome",
    "resume_request_acknowledged",
    "resume_request_id",
    "resume_requests_path_for",
    "save_checkpoint",
    "scratchpad_sha256",
    "write_resume_signal",
]


SCHEMA_VERSION: int = 1
"""On-disk schema version. Bump on any backwards-incompatible change."""

CHECKPOINTS_SUBDIR: str = ".sdd/runtime/checkpoints"
"""Sub-path under the workdir where per-task checkpoint folders live."""

CHECKPOINT_FILENAME: str = "checkpoint.json"
"""File name written inside each task's checkpoint directory."""


class CheckpointMissingError(FileNotFoundError):
    """Raised when ``bernstein resume`` finds no checkpoint for the task."""


class CheckpointCorruptError(ValueError):
    """Raised when a checkpoint file exists but cannot be parsed."""


class TaskResumeCheckpoint(BaseModel):
    """Pydantic model for the per-task resume checkpoint.

    Strict by design: unknown fields are rejected so a drift between
    writer and reader fails fast instead of silently dropping data.
    """

    model_config = ConfigDict(extra="forbid", frozen=False)

    schema_version: int = Field(default=SCHEMA_VERSION, ge=1)
    task_id: str = Field(..., min_length=1)
    last_completed_step_id: str = ""
    trace_cursor: int = Field(default=0, ge=0)
    scratchpad_path: str | None = None
    scratchpad_sha256: str | None = None
    adapter: str = ""
    adapter_session_id: str = ""
    worktree_path: str | None = None
    resume_count: int = Field(default=0, ge=0)
    stall_reason: str | None = None
    merge_cursor: dict[str, Any] | None = None
    meta: dict[str, Any] = Field(default_factory=dict)
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())

    def touch(self) -> None:
        """Stamp ``updated_at`` with the current UTC time."""
        self.updated_at = datetime.now(UTC).isoformat()


# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------


def checkpoint_dir_for(workdir: Path, task_id: str) -> Path:
    """Return the directory where ``task_id``'s checkpoint should live."""
    return workdir / CHECKPOINTS_SUBDIR / task_id


def checkpoint_path_for(workdir: Path, task_id: str) -> Path:
    """Return the absolute path of ``task_id``'s checkpoint JSON file."""
    return checkpoint_dir_for(workdir, task_id) / CHECKPOINT_FILENAME


# ---------------------------------------------------------------------------
# Persist / load
# ---------------------------------------------------------------------------


def _resume_file_path(workdir: Path, task_id: str, suffix: str, *, ensure_dir: bool = False) -> Path:
    """Return a containment-checked file path directly under ``.sdd/runtime/resume``."""
    if not task_id or task_id in {".", ".."} or "/" in task_id or "\\" in task_id or "\x00" in task_id:
        raise PathContainmentError(f"Unsafe resume task_id {task_id!r}")
    directory = workdir / ".sdd" / "runtime" / "resume"
    if ensure_dir:
        directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{task_id}{suffix}"
    resolved_dir = directory.resolve()
    resolved_target = target.resolve()
    if (
        target.parent != directory
        or not resolved_target.is_relative_to(resolved_dir)
        or resolved_target.parent != resolved_dir
    ):
        raise PathContainmentError(f"Resume path for task {task_id!r} escapes {directory}")
    # Only the shared signal directory is created by this command.
    if ensure_dir and not target.parent.is_dir():
        raise FileNotFoundError(f"Resume signal directory does not exist: {target.parent}")
    return target


def write_resume_signal(workdir: Path, task_id: str, **fields: object) -> Path:
    """Publish one complete signal for a task in the existing resume directory."""
    target = _resume_file_path(workdir, task_id, ".signal", ensure_dir=True)
    write_atomic_json(target, {"task_id": task_id, **fields})
    return target


def save_checkpoint(workdir: Path, checkpoint: TaskResumeCheckpoint) -> Path:
    """Atomically persist ``checkpoint`` for the task it identifies.

    Args:
        workdir: Project root (resolves to ``<workdir>/.sdd/runtime/...``).
        checkpoint: Populated :class:`TaskResumeCheckpoint`.

    Returns:
        Path to the written checkpoint file.
    """
    checkpoint.touch()
    target_dir = checkpoint_dir_for(workdir, checkpoint.task_id)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / CHECKPOINT_FILENAME

    payload = checkpoint.model_dump(mode="json")
    data = json.dumps(payload, indent=2, sort_keys=True)

    # Write to a sibling temp file then atomic replace. We use the same
    # directory so the rename stays on a single filesystem.
    fd, tmp_path_str = tempfile.mkstemp(
        prefix=".checkpoint-",
        suffix=".tmp",
        dir=str(target_dir),
    )
    tmp_path = Path(tmp_path_str)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, target)
    except Exception:
        # Best-effort cleanup of the temp file; never mask the original
        # exception with a cleanup error.
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:  # pragma: no cover -- defensive
            logger.debug("Failed to remove temp checkpoint at %s", tmp_path, exc_info=True)
        raise
    logger.debug("Saved resume checkpoint for task=%s to %s", checkpoint.task_id, target)
    return target


def load_checkpoint(workdir: Path, task_id: str) -> TaskResumeCheckpoint:
    """Load the resume checkpoint for ``task_id``.

    Args:
        workdir: Project root.
        task_id: Task identifier.

    Returns:
        Populated :class:`TaskResumeCheckpoint`.

    Raises:
        CheckpointMissingError: No checkpoint file on disk.
        CheckpointCorruptError: The contents have invalid encoding, JSON or schema.
        OSError: The checkpoint could not be read.
    """
    path = checkpoint_path_for(workdir, task_id)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError as exc:
        raise CheckpointMissingError(
            f"No resume checkpoint for task {task_id!r} at {path}. "
            "Either the task has not produced a checkpoint yet or the "
            "directory was cleaned up. Run the task fresh with "
            f"`bernstein run` instead."
        ) from exc
    except UnicodeDecodeError as exc:
        raise CheckpointCorruptError(f"Checkpoint for task {task_id!r} at {path} is not valid UTF-8: {exc}") from exc
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CheckpointCorruptError(f"Checkpoint for task {task_id!r} at {path} is not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise CheckpointCorruptError(f"Checkpoint for task {task_id!r} at {path} is not a JSON object.")
    try:
        return TaskResumeCheckpoint.model_validate(data)
    except ValidationError as exc:
        raise CheckpointCorruptError(
            f"Checkpoint for task {task_id!r} at {path} failed schema validation: {exc}"
        ) from exc


def bump_resume_count(workdir: Path, task_id: str) -> TaskResumeCheckpoint:
    """Increment ``resume_count`` on disk and return the updated record.

    Used by ``bernstein resume`` immediately before re-spawning so the
    dashboard can flag flaky tasks even if the resume itself crashes.

    Raises:
        CheckpointMissingError / CheckpointCorruptError: Same contract as
        :func:`load_checkpoint`.
    """
    checkpoint = load_checkpoint(workdir, task_id)
    checkpoint.resume_count += 1
    save_checkpoint(workdir, checkpoint)
    return checkpoint


# ---------------------------------------------------------------------------
# Scratchpad helpers
# ---------------------------------------------------------------------------


def scratchpad_sha256(scratchpad_path: Path | None) -> str | None:
    """Return the SHA-256 of the scratchpad file, or ``None`` if absent.

    The hash is captured on every save so a downstream resume can detect
    post-checkpoint scratchpad tampering.
    """
    if scratchpad_path is None:
        return None
    try:
        data = scratchpad_path.read_bytes()
    except FileNotFoundError:
        return None
    except OSError as exc:
        logger.debug("scratchpad_sha256: read failed for %s: %s", scratchpad_path, exc)
        return None
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------------------
# Runtime resume requests (#7854)
# ---------------------------------------------------------------------------
#
# A ``<task_id>.signal`` file is a wake-up hint only: legacy readers understand
# its shape, so we keep every existing field and merely add ``request_id`` and
# ``source_digest``. The durable request/outcome state lives in a companion
# ``<task_id>.requests.json`` file. Workers recover from that record, never
# from the signal filename.

RESUME_REQUESTS_SUFFIX: str = ".requests.json"
"""Suffix of the companion file holding a task's resume request records."""

REQUEST_SCHEMA_VERSION: int = 1

SOURCE_TASK_CHECKPOINT: str = "task_checkpoint"
SOURCE_WORK_LEDGER: str = "work_ledger"


class ResumeDispatchStatus(StrEnum):
    """Persisted state of one local resume request."""

    QUEUED = "queued"
    CLAIMED = "claimed"
    RESUMED = "resumed"


class ResumeRequestCorruptError(ValueError):
    """Raised when a companion resume-request document cannot be trusted."""


@dataclass(frozen=True)
class ResumeRequestRecord:
    """One deterministic, task-scoped resume request and its outcome."""

    request_id: str
    task_id: str
    source: str
    source_digest: str
    dispatch_status: str
    run_id: str | None = None
    created_at: str = ""
    claimed_at: str | None = None
    resumed_at: str | None = None
    worker_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return the on-disk representation of this record."""
        return {
            "request_id": self.request_id,
            "task_id": self.task_id,
            "source": self.source,
            "source_digest": self.source_digest,
            "dispatch_status": self.dispatch_status,
            "run_id": self.run_id,
            "created_at": self.created_at,
            "claimed_at": self.claimed_at,
            "resumed_at": self.resumed_at,
            "worker_id": self.worker_id,
        }

    @classmethod
    def from_dict(cls, data: object) -> ResumeRequestRecord:
        """Parse one record, refusing incomplete or unknown-state entries."""
        if not isinstance(data, dict):
            raise ResumeRequestCorruptError("resume request entry is not an object")
        row = cast("dict[str, Any]", data)
        required = ("request_id", "task_id", "source", "source_digest", "dispatch_status")
        missing = [key for key in required if not isinstance(row.get(key), str) or not row[key]]
        if missing:
            raise ResumeRequestCorruptError(f"resume request entry missing fields: {', '.join(missing)}")
        status = str(row["dispatch_status"])
        if status not in {state.value for state in ResumeDispatchStatus}:
            raise ResumeRequestCorruptError(f"unknown resume request state {status!r}")
        return cls(
            request_id=str(row["request_id"]),
            task_id=str(row["task_id"]),
            source=str(row["source"]),
            source_digest=str(row["source_digest"]),
            dispatch_status=status,
            run_id=row.get("run_id") if isinstance(row.get("run_id"), str) else None,
            created_at=str(row.get("created_at") or ""),
            claimed_at=row.get("claimed_at") if isinstance(row.get("claimed_at"), str) else None,
            resumed_at=row.get("resumed_at") if isinstance(row.get("resumed_at"), str) else None,
            worker_id=row.get("worker_id") if isinstance(row.get("worker_id"), str) else None,
        )


def _canonical_digest(payload: dict[str, Any]) -> str:
    """SHA-256 over a canonical JSON projection (no timestamps, no nonce)."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def checkpoint_source_digest(checkpoint: TaskResumeCheckpoint) -> str:
    """Digest the attempt-identifying checkpoint fields.

    ``resume_count`` and ``updated_at`` are deliberately excluded: ``prepare_resume``
    bumps both *before* publishing, so hashing them would mint a fresh request id
    for every duplicate CLI invocation of the same interrupted attempt.
    """
    return _canonical_digest(
        {
            "task_id": checkpoint.task_id,
            "last_completed_step_id": checkpoint.last_completed_step_id,
            "trace_cursor": checkpoint.trace_cursor,
            "scratchpad_sha256": checkpoint.scratchpad_sha256,
            "adapter": checkpoint.adapter,
            "adapter_session_id": checkpoint.adapter_session_id,
            "worktree_path": checkpoint.worktree_path,
            "stall_reason": checkpoint.stall_reason,
            "merge_cursor": checkpoint.merge_cursor,
        }
    )


def ledger_source_digest(run_id: str, task_id: str, last_seq: int) -> str:
    """Digest one frontier task's last *task-level* ledger transition.

    ``TaskState.last_seq`` is the sequence of the last task transition for this
    task. Run-level ``KIND_RUN_RESUMED`` entries (with their fresh nonces) never
    advance it, so repeating ``bernstein ledger resume`` with no worker in
    between yields the same digest, while completed tasks are not re-requested.
    """
    return _canonical_digest({"run_id": run_id, "task_id": task_id, "last_seq": last_seq})


def resume_request_id(
    task_id: str,
    source_digest: str,
    *,
    source: str = SOURCE_TASK_CHECKPOINT,
    run_id: str | None = None,
) -> str:
    """Return the deterministic request id for ``(task_id, source_digest)``."""
    payload: dict[str, Any] = {"task_id": task_id, "source": source, "source_digest": source_digest}
    if run_id is not None:
        payload["run_id"] = run_id
    return _canonical_digest(payload)


def resume_requests_path_for(workdir: Path, task_id: str) -> Path:
    """Return the companion request-state path for ``task_id``."""
    return _resume_file_path(workdir, task_id, RESUME_REQUESTS_SUFFIX)


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _read_signal_payload(workdir: Path, task_id: str) -> dict[str, Any] | None:
    """Best-effort read of the wake-up hint; a broken hint must not block a new publish."""
    signal = _resume_file_path(workdir, task_id, ".signal")
    try:
        data = json.loads(signal.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        logger.debug("Could not read resume signal %s: %s", signal, exc)
        return None
    return cast("dict[str, Any]", data) if isinstance(data, dict) else None


def _record_from_signal(task_id: str, payload: dict[str, Any]) -> ResumeRequestRecord | None:
    """Derive an unacknowledged wake-up record from a signal.

    Legacy signals have no ``request_id``: their content digest is used instead.
    A modern signal whose request is missing from the companion file is also
    recovered here, so an interrupted two-file publish is still consumable.
    """
    source = (
        payload.get("source")
        if payload.get("source") in (SOURCE_TASK_CHECKPOINT, SOURCE_WORK_LEDGER)
        else (SOURCE_TASK_CHECKPOINT)
    )
    digest = payload.get("source_digest")
    request_id = payload.get("request_id")
    if not isinstance(digest, str) or not digest:
        digest = _canonical_digest({k: v for k, v in payload.items() if k not in ("request_id", "source_digest")})
    if not isinstance(request_id, str) or not request_id:
        request_id = resume_request_id(task_id, str(digest), source=str(source), run_id=payload.get("run_id"))
    return ResumeRequestRecord(
        request_id=request_id,
        task_id=task_id,
        source=str(source),
        source_digest=str(digest),
        dispatch_status=ResumeDispatchStatus.QUEUED.value,
        run_id=payload.get("run_id") if isinstance(payload.get("run_id"), str) else None,
    )


def read_resume_requests(workdir: Path, task_id: str) -> list[ResumeRequestRecord]:
    """Read all request records for ``task_id``, including legacy wake-up hints.

    A legacy ``<task_id>.signal`` without a request id contributes a derived,
    unacknowledged ``queued`` record. The new companion structure is written
    atomically by :func:`publish_resume_request` on the next publish.
    """
    records: list[ResumeRequestRecord] = []
    path = resume_requests_path_for(workdir, task_id)
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        raw = None
    if raw is not None:
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ResumeRequestCorruptError(f"Corrupt resume request file {path}: {exc}") from exc
        if not isinstance(data, dict):
            raise ResumeRequestCorruptError(f"Invalid resume request file {path}: expected a requests list")
        payload = cast("dict[str, Any]", data)
        raw_requests = payload.get("requests")
        if not isinstance(raw_requests, list):
            raise ResumeRequestCorruptError(f"Invalid resume request file {path}: expected a requests list")
        for item in cast("list[object]", raw_requests):
            record = ResumeRequestRecord.from_dict(item)
            if record.task_id == task_id and all(existing.request_id != record.request_id for existing in records):
                records.append(record)

    signal_payload = _read_signal_payload(workdir, task_id)
    if signal_payload is not None:
        derived = _record_from_signal(task_id, signal_payload)
        if derived is not None and all(existing.request_id != derived.request_id for existing in records):
            records.append(derived)
    return records


def _write_records(workdir: Path, task_id: str, records: list[ResumeRequestRecord]) -> None:
    path = _resume_file_path(workdir, task_id, RESUME_REQUESTS_SUFFIX, ensure_dir=True)
    write_atomic_json(
        path,
        {
            "schema_version": REQUEST_SCHEMA_VERSION,
            "task_id": task_id,
            "requests": [record.to_dict() for record in records],
        },
    )


def load_resume_request(workdir: Path, task_id: str, request_id: str) -> ResumeRequestRecord | None:
    """Return one request by id, or ``None`` when it has never been published."""
    return next(
        (record for record in read_resume_requests(workdir, task_id) if record.request_id == request_id),
        None,
    )


def resume_request_acknowledged(workdir: Path, task_id: str, request_id: str) -> bool:
    """Return whether a worker recorded ``resumed`` for this exact request."""
    record = load_resume_request(workdir, task_id, request_id)
    return record is not None and record.dispatch_status == ResumeDispatchStatus.RESUMED.value


def publish_resume_request(
    workdir: Path,
    task_id: str,
    *,
    source_digest: str,
    source: str = SOURCE_TASK_CHECKPOINT,
    run_id: str | None = None,
    signal_fields: dict[str, Any] | None = None,
) -> ResumeRequestRecord:
    """Publish (or re-publish) a queued request and its wake-up signal.

    Re-publishing the same request id never regresses a ``claimed`` or
    ``resumed`` record: the CLI may confirm a queued request while a worker is
    already handling it.
    """
    request_id = resume_request_id(task_id, source_digest, source=source, run_id=run_id)
    records = read_resume_requests(workdir, task_id)
    existing = next((record for record in records if record.request_id == request_id), None)
    if existing is not None:
        # Refresh the wake-up hint without regressing the persisted state.
        repeat_fields: dict[str, Any] = dict(signal_fields or {})
        repeat_fields.update(
            {
                "request_id": existing.request_id,
                "source_digest": existing.source_digest,
                "source": existing.source,
            }
        )
        if existing.run_id is not None:
            repeat_fields["run_id"] = existing.run_id
        write_resume_signal(workdir, task_id, **repeat_fields)
        return existing

    record = ResumeRequestRecord(
        request_id=request_id,
        task_id=task_id,
        source=source,
        source_digest=source_digest,
        dispatch_status=ResumeDispatchStatus.QUEUED.value,
        run_id=run_id,
        created_at=_utc_now(),
    )
    fields: dict[str, Any] = dict(signal_fields or {})
    fields.update({"request_id": request_id, "source_digest": source_digest, "source": source})
    if run_id is not None:
        fields["run_id"] = run_id
    # Publish the wake-up hint first: an interrupted signal write must leave
    # the previous request record untouched. A worker can recover a request
    # from the signal alone if the companion write is interrupted.
    write_resume_signal(workdir, task_id, **fields)
    _write_records(workdir, task_id, [*records, record])
    return record


def claim_resume_request(
    workdir: Path,
    task_id: str,
    request_id: str | None = None,
    *,
    source: str | None = None,
    run_id: str | None = None,
    worker_id: str | None = None,
) -> ResumeRequestRecord | None:
    """Atomically mark a queued request claimed.

    With no explicit ``request_id`` the newest queued request for ``source``
    and ``run_id`` (all sources/runs when omitted) is selected. Claiming an
    already-claimed record is idempotent so a restarted worker recovers from
    the persisted record; a ``resumed`` request is never claimed twice.
    """
    records = read_resume_requests(workdir, task_id)
    candidates = [
        (index, record)
        for index, record in enumerate(records)
        if (request_id is None or record.request_id == request_id)
        and (source is None or record.source == source)
        and (run_id is None or record.run_id == run_id)
    ]
    if request_id is None:
        queued = [item for item in candidates if item[1].dispatch_status == ResumeDispatchStatus.QUEUED.value]
        if queued:
            candidates = [queued[-1]]
        else:
            # No queued request: recover the newest claimed one after a restart.
            claimed = [
                item for item in reversed(candidates) if item[1].dispatch_status == ResumeDispatchStatus.CLAIMED.value
            ]
            candidates = claimed[:1]
    if not candidates:
        return None
    index, record = candidates[-1]
    if record.dispatch_status == ResumeDispatchStatus.RESUMED.value:
        return None
    if record.dispatch_status == ResumeDispatchStatus.CLAIMED.value:
        return record
    claimed_record = replace(
        record,
        dispatch_status=ResumeDispatchStatus.CLAIMED.value,
        claimed_at=_utc_now(),
        worker_id=worker_id,
    )
    records[index] = claimed_record
    _write_records(workdir, task_id, records)
    return claimed_record


def record_resume_outcome(
    workdir: Path,
    task_id: str,
    request_id: str,
    *,
    worker_id: str | None = None,
) -> ResumeRequestRecord | None:
    """Record that a worker actually resumed the claimed request."""
    records = read_resume_requests(workdir, task_id)
    for index, record in enumerate(records):
        if record.request_id != request_id:
            continue
        if record.dispatch_status == ResumeDispatchStatus.RESUMED.value:
            return record
        resumed = replace(
            record,
            dispatch_status=ResumeDispatchStatus.RESUMED.value,
            resumed_at=_utc_now(),
            worker_id=worker_id or record.worker_id,
        )
        records[index] = resumed
        _write_records(workdir, task_id, records)
        return resumed
    return None


def consume_resume_request(
    workdir: Path,
    task_id: str,
    runner: Callable[[], None],
    *,
    request_id: str | None = None,
    source: str | None = None,
    run_id: str | None = None,
    worker_id: str | None = None,
) -> ResumeRequestRecord | None:
    """Claim, execute, and acknowledge one resume request.

    Exposed for the single-task checkpoint resume path; the supervisor uses
    the same claim/outcome primitives around its frontier loop. An in-process
    failure still consumed the request, so it is acknowledged before the
    exception propagates. A hard kill leaves the record ``claimed`` for the
    restarted worker to recover.
    """
    record = claim_resume_request(
        workdir,
        task_id,
        request_id,
        source=source,
        run_id=run_id,
        worker_id=worker_id,
    )
    try:
        runner()
    except Exception:
        if record is not None:
            record_resume_outcome(workdir, task_id, record.request_id, worker_id=worker_id)
        raise
    if record is None:
        return None
    return record_resume_outcome(workdir, task_id, record.request_id, worker_id=worker_id)
