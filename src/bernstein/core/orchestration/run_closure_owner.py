"""Bind a foreground orchestrator PID to the run a recovery observer closes.

The owner record is not itself closure evidence.  It is the attribution input
that lets the existing watchdog or the next singleton startup say which exact
run belonged to a positively dead PID.  The observer verifies the named
journal before committing its inference to the authenticated audit chain.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from bernstein.core.persistence.atomic_write import write_atomic_json
from bernstein.core.replay.journal import contained_run_journal, load_events, run_journal_path, verify_journal
from bernstein.core.security.audit_chain import AuditChainStore
from bernstein.core.security.run_closure import RunClosureError, close_run

logger = logging.getLogger(__name__)

_OWNER_FILE = "spawner-run-owner.json"


@dataclass(frozen=True, slots=True)
class SpawnerRunOwner:
    """Run identity written by the foreground orchestrator process."""

    run_id: str
    pid: int
    started_journal_head: str
    started_journal_event_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "pid": self.pid,
            "started_journal_head": self.started_journal_head,
            "started_journal_event_count": self.started_journal_event_count,
        }


def owner_path(sdd_dir: Path) -> Path:
    """Return the singleton foreground owner record path."""
    return Path(sdd_dir) / "runtime" / _OWNER_FILE


def owner_history_path(sdd_dir: Path, run_id: str) -> Path:
    """Return the run-contained durable owner record path."""
    return run_journal_path(Path(sdd_dir), run_id).with_name("closure-owner.json")


def write_spawner_run_owner(
    *,
    sdd_dir: Path,
    run_id: str,
    journal_head: str,
    journal_event_count: int,
    pid: int | None = None,
) -> None:
    """Atomically publish this process as the owner of *run_id*."""
    owner = SpawnerRunOwner(
        run_id=run_id,
        pid=os.getpid() if pid is None else pid,
        started_journal_head=journal_head,
        started_journal_event_count=journal_event_count,
    )
    payload = owner.to_dict()
    # The singleton supports the live watchdog. The per-run copy prevents a
    # later run from erasing an unresolved orphan attribution when it takes
    # over the singleton slot.
    write_atomic_json(owner_history_path(sdd_dir, run_id), payload, indent=None, sort_keys=True)
    write_atomic_json(owner_path(sdd_dir), payload, indent=None, sort_keys=True)


def _read_owner(path: Path) -> SpawnerRunOwner | None:
    """Read one structurally valid owner record."""
    try:
        raw: object = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    fields = cast("dict[str, object]", raw)
    run_id = str(fields.get("run_id", "")).strip()
    pid = fields.get("pid")
    head = str(fields.get("started_journal_head", "")).strip()
    count = fields.get("started_journal_event_count")
    if (
        not run_id
        or not isinstance(pid, int)
        or isinstance(pid, bool)
        or pid <= 0
        or not head
        or not isinstance(count, int)
        or isinstance(count, bool)
        or count <= 0
    ):
        return None
    return SpawnerRunOwner(run_id, pid, head, count)


def read_spawner_run_owner(sdd_dir: Path) -> SpawnerRunOwner | None:
    """Read a structurally valid owner record, otherwise return ``None``."""
    return _read_owner(owner_path(sdd_dir))


def list_spawner_run_owners(sdd_dir: Path) -> list[SpawnerRunOwner]:
    """Return durable owner records without following escaped run dirs."""
    runs_root = Path(sdd_dir) / "runs"
    if not runs_root.is_dir():
        return []
    owners: list[SpawnerRunOwner] = []
    for entry in runs_root.iterdir():
        journal_path = contained_run_journal(runs_root, entry.name)
        if journal_path is None:
            continue
        owner = _read_owner(journal_path.with_name("closure-owner.json"))
        if owner is not None and owner.run_id == entry.name:
            owners.append(owner)
    return owners


def reconcile_spawner_run_owner(*, workdir: Path, owner: SpawnerRunOwner) -> bool:
    """Authenticate a terminal inference for one caller-proven dead owner."""
    root = Path(workdir)
    sdd_dir = root / ".sdd"
    journal_path = run_journal_path(sdd_dir, owner.run_id)
    verification = verify_journal(journal_path)
    if not verification.ok:
        logger.warning(
            "Run closure recovery refused for %s: journal verification failed: %s",
            owner.run_id,
            verification.errors[:1],
        )
        return False
    events = load_events(journal_path)
    if not events:
        return False
    started = events[0]
    if (
        started.get("event") != "run_started"
        or str(started.get("run_id", "")) != owner.run_id
        or str(started.get("event_hash", "")) != owner.started_journal_head
        or owner.started_journal_event_count != 1
    ):
        logger.warning("Run closure recovery refused for %s: owner record does not match journal start", owner.run_id)
        return False

    final = events[-1]
    outcome = "abandoned"
    if final.get("event") == "run_completed":
        recorded = str(final.get("outcome", "completed"))
        if recorded in {"completed", "failed", "cancelled"}:
            outcome = recorded
    try:
        close_run(
            chain=AuditChainStore(sdd_dir / "audit"),
            run_id=owner.run_id,
            outcome=outcome,
            actor="recovery_watchdog",
            run_journal_head=str(final.get("event_hash", "")),
            run_journal_event_count=len(events),
        )
    except RunClosureError as exc:
        logger.warning("Run closure recovery refused for %s: %s", owner.run_id, exc)
        return False
    logger.info(
        "Recovered authenticated %s closure for run %s after observing owner PID %d dead",
        outcome,
        owner.run_id,
        owner.pid,
    )
    return True


def reconcile_positively_dead_owner(*, workdir: Path, dead_pid: int) -> bool:
    """Close the exact run owned by *dead_pid* after positive death evidence.

    The caller owns the death test.  This function refuses a PID mismatch, a
    missing or divergent journal, and an owner record that does not match the
    journal's authenticated start row.  ``run_completed`` recovers a clean
    completion whose process died after journaling but before closure; every
    other verified prefix is recorded as ``abandoned``.

    Returns ``True`` only when an identical or newly appended closure exists.
    """
    owner = read_spawner_run_owner(Path(workdir) / ".sdd")
    if owner is None or owner.pid != dead_pid:
        return False
    return reconcile_spawner_run_owner(workdir=workdir, owner=owner)


__all__ = [
    "SpawnerRunOwner",
    "list_spawner_run_owners",
    "owner_history_path",
    "owner_path",
    "read_spawner_run_owner",
    "reconcile_positively_dead_owner",
    "reconcile_spawner_run_owner",
    "write_spawner_run_owner",
]
