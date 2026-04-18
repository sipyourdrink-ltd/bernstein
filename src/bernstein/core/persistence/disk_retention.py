"""Disk-level retention janitor for per-run artifacts (audit-081).

Retains the newest N run directories and WAL files, deleting older ones.
Called from the orchestrator cleanup path so that long-running bernstein
instances do not accumulate unbounded per-run state on disk.

Kept files:
  - ``.sdd/runs/<run_id>/`` — newest
    :attr:`JanitorDefaults.run_retention_count` directories by mtime.
  - ``.sdd/runtime/wal/<run_id>.wal.jsonl`` — newest
    :attr:`JanitorDefaults.wal_retention_count` files (plus their
    rotated ``.N`` backups) by mtime.

Replay tooling (``bernstein replay <run_id>``) must check whether the
requested run directory survived retention; retention never deletes the
active run.
"""

from __future__ import annotations

import contextlib
import logging
import shutil
from dataclasses import dataclass, field
from pathlib import Path  # noqa: TC003 (runtime use in Path.iterdir / Path.rglob call sites)

from bernstein.core.defaults import JANITOR

logger = logging.getLogger(__name__)


@dataclass
class RetentionResult:
    """Structured outcome of a retention sweep.

    Attributes:
        runs_scanned: Number of ``.sdd/runs/<id>`` directories examined.
        runs_deleted: List of run IDs removed during the sweep.
        wal_scanned: Number of WAL run groups examined.
        wal_deleted: List of WAL file names removed during the sweep.
        errors: Human-readable error messages for any deletion failures.
    """

    runs_scanned: int = 0
    runs_deleted: list[str] = field(default_factory=list[str])
    wal_scanned: int = 0
    wal_deleted: list[str] = field(default_factory=list[str])
    errors: list[str] = field(default_factory=list[str])


def prune_old_runs(
    sdd_dir: Path,
    *,
    retention_count: int | None = None,
    active_run_id: str | None = None,
) -> tuple[int, list[str], list[str]]:
    """Keep the newest ``retention_count`` run directories, delete the rest.

    Args:
        sdd_dir: The ``.sdd`` directory root.
        retention_count: How many run directories to keep. Defaults to
            :attr:`JanitorDefaults.run_retention_count`.
        active_run_id: If set, always preserve the directory for this run.

    Returns:
        Tuple of (scanned, deleted_run_ids, errors).
    """
    keep = retention_count if retention_count is not None else JANITOR.run_retention_count
    runs_dir = sdd_dir / "runs"
    deleted: list[str] = []
    errors: list[str] = []
    if not runs_dir.is_dir():
        return 0, deleted, errors

    entries = [p for p in runs_dir.iterdir() if p.is_dir()]
    scanned = len(entries)
    # Sort by mtime descending — newest first.
    entries.sort(key=lambda p: p.stat().st_mtime, reverse=True)

    # Always keep the active run, even if it is the oldest.
    protected: set[str] = {active_run_id} if active_run_id else set()
    kept: list[Path] = []
    victims: list[Path] = []
    for entry in entries:
        if entry.name in protected or len(kept) < keep:
            kept.append(entry)
        else:
            victims.append(entry)

    for victim in victims:
        try:
            shutil.rmtree(victim)
            deleted.append(victim.name)
        except OSError as exc:
            errors.append(f"rmtree {victim.name}: {exc}")
            logger.warning("disk_retention: failed to delete %s: %s", victim, exc)

    return scanned, deleted, errors


def prune_old_wal_files(
    sdd_dir: Path,
    *,
    retention_count: int | None = None,
    active_run_id: str | None = None,
) -> tuple[int, list[str], list[str]]:
    """Keep the newest ``retention_count`` WAL files + rotated backups.

    Only files matching ``<run_id>.wal.jsonl`` (plus any rotated ``.N``
    backups) are considered. ``idempotency.jsonl`` and its rotations are
    left untouched — that file is the IdempotencyStore's source of truth
    across runs.

    Args:
        sdd_dir: The ``.sdd`` directory root.
        retention_count: How many WAL run groups to keep. Defaults to
            :attr:`JanitorDefaults.wal_retention_count`.
        active_run_id: If set, always preserve WAL files for this run.

    Returns:
        Tuple of (scanned, deleted_file_names, errors).
    """
    keep = retention_count if retention_count is not None else JANITOR.wal_retention_count
    wal_dir = sdd_dir / "runtime" / "wal"
    deleted: list[str] = []
    errors: list[str] = []
    if not wal_dir.is_dir():
        return 0, deleted, errors

    # Group rotated backups with their live file so retention counts runs,
    # not individual .N rollovers. A "run" here is identified by the live
    # filename stem (e.g. ``20260417-123.wal.jsonl`` and its
    # ``20260417-123.wal.jsonl.1`` backups share stem ``20260417-123``).
    groups: dict[str, list[Path]] = {}
    for candidate in wal_dir.iterdir():
        if not candidate.is_file():
            continue
        name = candidate.name
        if name == "idempotency.jsonl" or name.startswith("idempotency.jsonl."):
            continue
        if ".wal.jsonl" not in name:
            continue
        stem = name.split(".wal.jsonl", 1)[0]
        groups.setdefault(stem, []).append(candidate)

    scanned = len(groups)
    # Sort groups by newest mtime across live + backups, descending.
    sorted_stems = sorted(
        groups.keys(),
        key=lambda s: max(p.stat().st_mtime for p in groups[s]),
        reverse=True,
    )

    protected_stem = active_run_id or ""
    kept_count = 0
    victims: list[Path] = []
    for stem in sorted_stems:
        if stem == protected_stem or kept_count < keep:
            kept_count += 1
            continue
        victims.extend(groups[stem])

    for victim in victims:
        try:
            victim.unlink()
            deleted.append(victim.name)
        except OSError as exc:
            errors.append(f"unlink {victim.name}: {exc}")
            logger.warning("disk_retention: failed to delete %s: %s", victim, exc)

    return scanned, deleted, errors


def run_retention(
    sdd_dir: Path,
    *,
    active_run_id: str | None = None,
    run_retention_count: int | None = None,
    wal_retention_count: int | None = None,
) -> RetentionResult:
    """Sweep both runs/ and runtime/wal/ in a single pass (audit-081).

    Args:
        sdd_dir: The ``.sdd`` directory root.
        active_run_id: Run ID that must not be deleted regardless of age.
        run_retention_count: Override for
            :attr:`JanitorDefaults.run_retention_count`.
        wal_retention_count: Override for
            :attr:`JanitorDefaults.wal_retention_count`.

    Returns:
        :class:`RetentionResult` summarising scanned and deleted items.
    """
    result = RetentionResult()
    with contextlib.suppress(OSError):
        runs_scanned, runs_deleted, runs_errors = prune_old_runs(
            sdd_dir,
            retention_count=run_retention_count,
            active_run_id=active_run_id,
        )
        result.runs_scanned = runs_scanned
        result.runs_deleted.extend(runs_deleted)
        result.errors.extend(runs_errors)

    with contextlib.suppress(OSError):
        wal_scanned, wal_deleted, wal_errors = prune_old_wal_files(
            sdd_dir,
            retention_count=wal_retention_count,
            active_run_id=active_run_id,
        )
        result.wal_scanned = wal_scanned
        result.wal_deleted.extend(wal_deleted)
        result.errors.extend(wal_errors)

    if result.runs_deleted or result.wal_deleted:
        logger.info(
            "disk_retention: pruned %d run dirs, %d wal files",
            len(result.runs_deleted),
            len(result.wal_deleted),
        )
    return result
