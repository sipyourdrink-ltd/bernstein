"""Fork-from-step on git-worktree snapshots (issue #2295).

Snapshot and resume are ``NotImplementedError`` across every cloud
sandbox backend, which blocks any session-rewind or fork-from-step
workflow. This module builds fork on the worktree snapshot primitive
(:mod:`bernstein.core.sandbox.snapshot`): a git commit is a cheap,
content-addressed snapshot, so an operator can rewind a run to any prior
step and branch a new run from it without waiting for cloud snapshot
APIs.

The flow, given a parent ``run_id`` and a journal step ``N``:

1. Read the parent's canonical event journal
   (``.sdd/runs/<run_id>/journal.jsonl``).
2. Find the snapshot event recorded at step ``N`` and read the snapshot
   commit sha it carries.
3. Resolve the ``refs/bernstein/snapshots/<run_id>/<N>`` ref and confirm
   it still points at the journal-recorded sha. A mismatch means the ref
   was tampered with (AC5) - the fork refuses.
4. Check that snapshot commit out into a fresh, isolated worktree.
5. Start a new run whose journal's first event parent-links the fork
   point (parent run id, fork step, snapshot sha), so the run chain
   becomes a tree rather than a list (AC2).

The snapshot sha stored in the journal and the ref actually created are
cross-verifiable (AC3): both are the same content-addressed commit, so a
verifier holding the parent journal and the snapshot ref can prove the
child branched from exactly that step.
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.git.git_basic import run_git
from bernstein.core.replay.journal import EventJournal, load_events
from bernstein.core.sandbox.snapshot import (
    SnapshotError,
    resume_worktree_snapshot,
    snapshot_ref_name,
)

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.security.audit_chain import AuditChainStore

logger = logging.getLogger(__name__)

#: Event type recorded in the journal at the step a snapshot is taken.
SNAPSHOT_EVENT = "snapshot"

#: Event type the child journal opens with to parent-link the fork point.
FORK_EVENT = "fork"

#: Where fork worktrees are checked out, relative to the repo root.
_WORKTREE_BASE = ".sdd/worktrees"


class ForkError(RuntimeError):
    """Raised when a fork-from-step operation cannot be completed."""


@dataclass(frozen=True)
class ForkResult:
    """Outcome of :func:`fork_run`.

    Attributes:
        new_run_id: The child run id the fork produced.
        parent_run_id: The run that was forked from.
        from_step: The journal step index the fork branched at.
        snapshot_sha: The content-addressed snapshot commit sha the child
            worktree was resumed from.
        worktree_path: Absolute path to the freshly checked-out worktree.
        child_head: The child journal's Merkle head after the fork event.
    """

    new_run_id: str
    parent_run_id: str
    from_step: int
    snapshot_sha: str
    worktree_path: str
    child_head: str


def record_snapshot_event(
    journal: EventJournal,
    *,
    snapshot_sha: str,
    step_index: int,
) -> None:
    """Record a snapshot commit sha into the run journal at ``step_index``.

    Called when a worktree snapshot is taken during a run so the snapshot
    id and the journal step become cross-verifiable (AC3). The recorded
    row carries the sha under ``snapshot_sha`` and the step under
    ``step_index``; :func:`fork_run` reads it back.

    Args:
        journal: The active run journal.
        snapshot_sha: The commit sha returned by the worktree snapshot.
        step_index: The journal step index the snapshot pins.
    """
    journal.record(SNAPSHOT_EVENT, snapshot_sha=snapshot_sha, step_index=step_index)


def _find_snapshot_sha(events: list[dict[str, Any]], from_step: int) -> str:
    """Return the snapshot sha recorded for ``from_step`` in *events*.

    Raises:
        ForkError: When no snapshot event pins ``from_step``.
    """
    for row in events:
        if row.get("event") != SNAPSHOT_EVENT:
            continue
        if int(row.get("step_index", -1)) != from_step:
            continue
        sha = str(row.get("snapshot_sha", ""))
        if sha:
            return sha
    raise ForkError(
        f"no snapshot recorded at step {from_step}; the run journal has no "
        f"snapshot event pinning that step, so there is nothing to fork from"
    )


def _resolve_ref(repo_root: Path, ref: str) -> str | None:
    """Resolve *ref* to a sha, or ``None`` when it does not resolve."""
    result = run_git(["rev-parse", "--verify", "--quiet", ref], repo_root, timeout=30)
    if result.returncode != 0:
        return None
    sha = result.stdout.strip()
    return sha or None


def fork_run(
    sdd_dir: Path,
    run_id: str,
    *,
    from_step: int,
    repo_root: Path,
    chain: AuditChainStore | None = None,
) -> ForkResult:
    """Fork *run_id* at journal step *from_step* into a new isolated run.

    Args:
        sdd_dir: The ``.sdd`` directory holding ``runs/<run_id>/``.
        run_id: The parent run to fork from.
        from_step: Journal step index the fork branches at. A snapshot
            must have been recorded at this step.
        repo_root: Repository root that owns the snapshot refs and object
            store.
        chain: Optional audit chain store. When provided, the fork is
            recorded as a ``replay.fork_snapshot`` HMAC-chain event so the
            fork lineage is independently attestable.

    Returns:
        A :class:`ForkResult` describing the child run and its worktree.

    Raises:
        ForkError: When the parent journal is missing, no snapshot was
            recorded at ``from_step``, the snapshot ref was tampered with
            (its sha no longer matches the journal), or the checkout
            fails.
    """
    journal_path = sdd_dir / "runs" / run_id / "journal.jsonl"
    if not journal_path.exists():
        raise ForkError(f"no journal for run {run_id!r} (looked at {journal_path})")

    events = load_events(journal_path)
    if not events:
        raise ForkError(f"run {run_id!r} journal is empty; nothing to fork")

    recorded_sha = _find_snapshot_sha(events, from_step)

    # AC5: the snapshot ref must still point at the journal-recorded sha.
    # A tampered ref (repointed at a different commit) is detected here
    # because the content-addressed sha no longer matches.
    ref = snapshot_ref_name(run_id, from_step)
    resolved = _resolve_ref(repo_root, ref)
    if resolved is None:
        raise ForkError(f"snapshot ref {ref} does not resolve; cannot fork run {run_id!r}")
    if resolved != recorded_sha:
        raise ForkError(
            f"snapshot ref tamper detected: {ref} resolves to {resolved} but the "
            f"journal recorded {recorded_sha} at step {from_step}"
        )

    new_run_id = f"fork-{run_id}-s{from_step}-{secrets.token_hex(4)}"
    dest_path = repo_root / _WORKTREE_BASE / new_run_id
    try:
        resume_worktree_snapshot(repo_root, recorded_sha, dest_path)
    except SnapshotError as exc:
        raise ForkError(f"failed to resume snapshot {recorded_sha} into {dest_path}: {exc}") from exc

    # AC2: the child journal's first event parent-links the fork point so
    # the run history is a tree, not a list.
    child_journal = EventJournal(new_run_id, sdd_dir)
    child_journal.record(
        FORK_EVENT,
        parent_run_id=run_id,
        fork_step=from_step,
        snapshot_sha=recorded_sha,
    )

    if chain is not None:
        from bernstein.core.security.audit_chain import record_fork_snapshot

        record_fork_snapshot(
            chain=chain,
            parent_run_id=run_id,
            fork_step=from_step,
            snapshot_sha=recorded_sha,
            new_run_id=new_run_id,
        )

    logger.info(
        "Forked run %s at step %d -> %s (snapshot %s)",
        run_id,
        from_step,
        new_run_id,
        recorded_sha[:12],
    )
    return ForkResult(
        new_run_id=new_run_id,
        parent_run_id=run_id,
        from_step=from_step,
        snapshot_sha=recorded_sha,
        worktree_path=str(dest_path),
        child_head=child_journal.head(),
    )


__all__ = [
    "FORK_EVENT",
    "SNAPSHOT_EVENT",
    "ForkError",
    "ForkResult",
    "fork_run",
    "record_snapshot_event",
]
