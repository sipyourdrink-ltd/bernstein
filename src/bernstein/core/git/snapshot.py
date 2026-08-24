"""Per-tool-call git snapshots and stacked agent branches.

Bernstein agents perform many small writes per task. Without per-write
checkpoints, the only granularity for undo is the agent's final commit.
This module gives the orchestrator a cheap pre-write checkpoint that an
operator can rewind without touching neighbouring agents' work.

Design
------

A snapshot is the work tree captured as a single git tree object plus a
small metadata blob. The tree is written into a side ref namespace at
``refs/bernstein/snapshots/<id>`` so it never pollutes the user's branch
list and cannot accidentally be pushed by the default ``git push``
incantation. Because git deduplicates trees by content hash, repeated
snapshots between near-identical states cost a few hundred bytes each.

The implementation deliberately uses only ``git`` plumbing commands so
it works on any git >= 2.30 and does not depend on ``dulwich`` or other
optional Python git libraries. ``run_git`` is reused from
:mod:`bernstein.core.git.git_basic` so the subprocess discipline (utf-8,
errors=replace, captured stdout) matches the rest of the package.

Stacked branches
----------------

A task can produce multiple agent runs (a backend pass followed by a qa
pass, for example). Rather than letting each run target the same base
branch, the orchestrator stacks them: each run's branch is created from
the tip of the previous run's branch. This preserves the chronological
order in the eventual PR review and lets operators bisect a regression
to the specific run that introduced it. The :func:`stack_push` helper
records the parent/child link inside the snapshot ref namespace at
``refs/bernstein/stacks/<task_id>/<n>`` so the relationship survives a
worktree teardown.

Public surface
--------------

* :class:`Snapshot` - frozen dataclass of metadata returned by every
  read or write.
* :class:`SnapshotStore` - facade for ``take``, ``undo``, ``list``,
  ``diff``.
* :func:`stack_push` / :func:`stack_list` - branch stack helpers.

The hook integration lives in :mod:`bernstein.core.lifecycle.hooks` and
calls :meth:`SnapshotStore.take` on ``preToolUse`` for tool calls that
mutate files. That binding is optional - operators who do not want the
overhead can disable the hook via configuration.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from bernstein.core.git.git_basic import run_git

if TYPE_CHECKING:
    # Only reachable from annotations: ``SnapshotStore.list`` shadows the
    # builtin inside the class body, so return types have to name it
    # explicitly.
    import builtins

logger = logging.getLogger(__name__)


SNAPSHOT_REF_PREFIX: str = "refs/bernstein/snapshots/"
"""Namespace for per-tool-call snapshot refs."""

STACK_REF_PREFIX: str = "refs/bernstein/stacks/"
"""Namespace for stacked-branch ordering refs."""

DEFAULT_GC_DAYS: int = 30
"""Retention window after which snapshots are eligible for garbage collection."""

_SNAPSHOT_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
"""Allow only alphanumerics, dot, dash, underscore - matches git ref rules."""

_TASK_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
"""Same constraint applied to task IDs used in stack ref paths."""


class SnapshotError(RuntimeError):
    """Raised when a snapshot operation cannot complete safely."""


@dataclass(frozen=True)
class Snapshot:
    """One captured workspace state.

    Attributes:
        snapshot_id: Stable identifier used in the ref name; matches
            :data:`_SNAPSHOT_ID_RE`.
        tree_sha: Hex sha of the captured git tree object.
        ref: Full ref path, e.g. ``refs/bernstein/snapshots/<id>``.
        ts_ns: Wall-clock timestamp in nanoseconds.
        task_id: Optional Bernstein task ID for filtering.
        tool_call_id: Optional originating tool-call ID for lineage joins.
        agent_id: Optional Bernstein agent slug.
        label: Human-readable label (defaults to the snapshot ID).
    """

    snapshot_id: str
    tree_sha: str
    ref: str
    ts_ns: int
    task_id: str | None = None
    tool_call_id: str | None = None
    agent_id: str | None = None
    label: str = ""
    parent_snapshot: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-serialisable dict; used by the CLI."""
        return asdict(self)


@dataclass(frozen=True)
class StackEntry:
    """One branch on a task's stacked-branches ordering.

    The ordering is preserved by the ``<n>`` suffix on the ref name -
    ``refs/bernstein/stacks/<task_id>/001``, ``002``, ... - so a normal
    ``git for-each-ref --sort=refname`` enumerates them in chronological
    order.
    """

    task_id: str
    index: int
    branch: str
    parent_branch: str | None
    ref: str


def _validate_snapshot_id(snapshot_id: str) -> None:
    if not _SNAPSHOT_ID_RE.match(snapshot_id):
        raise SnapshotError(f"invalid snapshot_id {snapshot_id!r}: must match {_SNAPSHOT_ID_RE.pattern}")


def _validate_task_id(task_id: str) -> None:
    if not _TASK_ID_RE.match(task_id):
        raise SnapshotError(f"invalid task_id {task_id!r}: must match {_TASK_ID_RE.pattern}")


def _make_snapshot_id(task_id: str | None, tool_call_id: str | None, ts_ns: int) -> str:
    """Generate a deterministic snapshot ID.

    The ID encodes task + tool-call + timestamp so two concurrent agents
    cannot collide on the same ref path. We hash the parts into a
    12-char hex suffix to keep the ref short while preserving the
    timestamp prefix for human-readable sorting.
    """
    raw = f"{task_id or '-'}|{tool_call_id or '-'}|{ts_ns}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    # YYYYMMDDhhmmss prefix from the timestamp gives a sortable name
    # without requiring `git for-each-ref --sort=committerdate`. The
    # ``time.gmtime`` call is intentional: a UTC prefix avoids cross-TZ
    # ambiguity when an operator inspects refs on a remote machine.
    secs = ts_ns // 1_000_000_000
    prefix = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(secs))
    return f"{prefix}-{digest}"


def _ensure_repo(cwd: Path) -> None:
    """Raise :class:`SnapshotError` when *cwd* is not a git work tree."""
    result = run_git(["rev-parse", "--is-inside-work-tree"], cwd)
    if not result.ok:
        raise SnapshotError(f"{cwd} is not inside a git work tree: {result.stderr.strip()}")


def _write_tree(cwd: Path) -> str:
    """Write the current work tree as a git tree and return its sha.

    ``git write-tree`` operates on an index, not on the work tree, so
    we drive a throwaway index (via ``GIT_INDEX_FILE``) to avoid
    clobbering the agent's deliberate staging decisions. This is the
    same trick ``git stash`` uses internally.

    The temp index is seeded from HEAD (when one exists) so untracked
    deletions are recorded as removals against the parent tree; we
    then ``git add -A`` to capture every other tracked/untracked
    change. The throwaway index lives in ``.git`` and is unlinked in
    the ``finally`` block whether or not the plumbing succeeded.
    """
    import os
    import subprocess

    tmp_index = cwd / ".git" / f"bernstein-snapshot-index.{time.time_ns()}"
    merged_env = os.environ | {"GIT_INDEX_FILE": str(tmp_index)}
    try:
        # Seed the temp index from HEAD when one exists. Without HEAD
        # (fresh repo with no commits) we simply start from an empty
        # index, which is fine - ``git add -A`` will populate it.
        head_result = run_git(["rev-parse", "--verify", "HEAD"], cwd)
        if head_result.ok:
            seed = subprocess.run(
                ["git", "read-tree", "HEAD"],
                cwd=cwd,
                env=merged_env,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
            if seed.returncode != 0:
                raise SnapshotError(f"git read-tree HEAD failed: {seed.stderr.strip()}")
        # Stage everything (tracked + untracked + deletes) into the
        # temp index. We pass ``--`` to defend against pathological
        # filenames starting with a dash.
        add = subprocess.run(
            ["git", "add", "-A", "--", "."],
            cwd=cwd,
            env=merged_env,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if add.returncode != 0:
            raise SnapshotError(f"git add -A failed: {add.stderr.strip()}")
        write = subprocess.run(
            ["git", "write-tree"],
            cwd=cwd,
            env=merged_env,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if write.returncode != 0:
            raise SnapshotError(f"git write-tree failed: {write.stderr.strip()}")
        return write.stdout.strip()
    finally:
        if tmp_index.exists():
            try:
                tmp_index.unlink()
            except OSError as exc:  # pragma: no cover - best-effort cleanup
                logger.warning("failed to remove temp snapshot index %s: %s", tmp_index, exc)


def _update_ref(cwd: Path, ref: str, value: str) -> None:
    """Point *ref* at *value* via ``git update-ref``."""
    result = run_git(["update-ref", ref, value], cwd)
    if not result.ok:
        raise SnapshotError(f"git update-ref {ref} failed: {result.stderr.strip()}")


def _delete_ref(cwd: Path, ref: str) -> None:
    """Best-effort ``git update-ref -d``; swallow missing-ref errors."""
    run_git(["update-ref", "-d", ref], cwd)


def _resolve_ref(cwd: Path, ref: str) -> str | None:
    """Return the sha *ref* points at, or ``None`` when it does not exist."""
    result = run_git(["rev-parse", "--verify", ref], cwd)
    if not result.ok:
        return None
    return result.stdout.strip() or None


def _metadata_path(cwd: Path, snapshot_id: str) -> Path:
    """Filesystem location for the snapshot's metadata JSON.

    Storing the metadata as a tracked-by-git side file inside
    ``.git/bernstein/snapshots/`` keeps it inside the repo's
    ``.git`` directory so ``git clone`` of a worktree does not copy
    snapshot state to the cloner. The directory is created lazily.
    """
    base = cwd / ".git" / "bernstein" / "snapshots"
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{snapshot_id}.json"


def _write_metadata(cwd: Path, snapshot: Snapshot) -> None:
    _metadata_path(cwd, snapshot.snapshot_id).write_text(
        json.dumps(snapshot.to_dict(), indent=2, sort_keys=True), encoding="utf-8"
    )


def _read_metadata(cwd: Path, snapshot_id: str) -> Snapshot | None:
    path = _metadata_path(cwd, snapshot_id)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return Snapshot(
            snapshot_id=str(payload["snapshot_id"]),
            tree_sha=str(payload["tree_sha"]),
            ref=str(payload["ref"]),
            ts_ns=int(payload["ts_ns"]),
            task_id=payload.get("task_id"),
            tool_call_id=payload.get("tool_call_id"),
            agent_id=payload.get("agent_id"),
            label=str(payload.get("label", "")),
            parent_snapshot=payload.get("parent_snapshot"),
        )
    except (json.JSONDecodeError, OSError, KeyError, TypeError, ValueError) as exc:
        # Treat schema-invalid sidecars as unreadable so callers degrade to
        # the ref-backed snapshot instead of propagating KeyError / TypeError
        # out of list()/get() and breaking the surrounding command.
        logger.warning("could not read snapshot metadata %s: %s", path, exc)
        return None


def _stack_ref(task_id: str, index: int) -> str:
    return f"{STACK_REF_PREFIX}{task_id}/{index:03d}"


def _stack_metadata_path(cwd: Path, task_id: str, index: int) -> Path:
    base = cwd / ".git" / "bernstein" / "stacks" / task_id
    base.mkdir(parents=True, exist_ok=True)
    return base / f"{index:03d}.json"


class SnapshotStore:
    """Facade over ``refs/bernstein/snapshots/*`` and the metadata sidecars.

    The store is intentionally stateless; every method re-resolves refs
    via ``git`` so two processes (orchestrator + CLI) cannot disagree on
    what exists. Concurrency between writers is delegated to git's own
    ref-update locking - ``update-ref`` acquires ``packed-refs.lock`` so
    parallel writes to the same ref are serialised.
    """

    def __init__(self, cwd: Path) -> None:
        """Initialise the store rooted at *cwd*.

        Args:
            cwd: Path inside a git work tree. The constructor verifies
                the directory is a repo and raises :class:`SnapshotError`
                otherwise so callers fail fast at construction.
        """
        self.cwd = Path(cwd)
        _ensure_repo(self.cwd)

    # ------------------------------------------------------------------
    # Write
    # ------------------------------------------------------------------

    def take(
        self,
        *,
        task_id: str | None = None,
        tool_call_id: str | None = None,
        agent_id: str | None = None,
        label: str = "",
        parent_snapshot: str | None = None,
    ) -> Snapshot:
        """Capture the current work tree as a snapshot.

        Args:
            task_id: Bernstein task ID. Subject to :data:`_TASK_ID_RE`.
            tool_call_id: Originating tool-call ID, joined later by
                lineage to recover the (snapshot, tool_call) pair.
            agent_id: Bernstein agent slug.
            label: Human-readable label shown by ``bernstein git
                snapshots``.
            parent_snapshot: Optional pointer to the previous snapshot
                in the same agent run; lets the CLI render a parent
                chain.

        Returns:
            The created :class:`Snapshot`.

        Raises:
            SnapshotError: If ``task_id`` is provided but invalid, or
                the underlying git plumbing fails.
        """
        if task_id is not None:
            _validate_task_id(task_id)

        ts_ns = time.time_ns()
        snapshot_id = _make_snapshot_id(task_id, tool_call_id, ts_ns)
        _validate_snapshot_id(snapshot_id)

        tree_sha = _write_tree(self.cwd)
        ref = f"{SNAPSHOT_REF_PREFIX}{snapshot_id}"
        _update_ref(self.cwd, ref, tree_sha)

        snapshot = Snapshot(
            snapshot_id=snapshot_id,
            tree_sha=tree_sha,
            ref=ref,
            ts_ns=ts_ns,
            task_id=task_id,
            tool_call_id=tool_call_id,
            agent_id=agent_id,
            label=label or snapshot_id,
            parent_snapshot=parent_snapshot,
        )
        _write_metadata(self.cwd, snapshot)
        logger.debug(
            "snapshot.take id=%s tree=%s task=%s tool=%s",
            snapshot_id,
            tree_sha,
            task_id,
            tool_call_id,
        )
        return snapshot

    # ------------------------------------------------------------------
    # Read
    # ------------------------------------------------------------------

    def get(self, snapshot_id: str) -> Snapshot | None:
        """Return the snapshot with *snapshot_id* or ``None``.

        The metadata sidecar is the source of truth for human fields
        (``task_id`` etc.). If it is missing we reconstruct a minimal
        record from the ref alone so an operator can still ``undo``
        when the sidecar was purged manually.
        """
        _validate_snapshot_id(snapshot_id)
        ref = f"{SNAPSHOT_REF_PREFIX}{snapshot_id}"
        tree_sha = _resolve_ref(self.cwd, ref)
        if tree_sha is None:
            return None
        meta = _read_metadata(self.cwd, snapshot_id)
        if meta is not None:
            return meta
        # Sidecar was purged; synthesise a record so undo still works.
        return Snapshot(
            snapshot_id=snapshot_id,
            tree_sha=tree_sha,
            ref=ref,
            ts_ns=0,
            label=snapshot_id,
        )

    def list(
        self,
        *,
        task_id: str | None = None,
        limit: int | None = None,
    ) -> list[Snapshot]:
        """Enumerate snapshots, optionally filtering by ``task_id``.

        Results are sorted newest-first by ``ts_ns`` so the CLI can
        render them without extra work. Refs without metadata are
        included with ``ts_ns=0`` so they sort last; this matches the
        "missing metadata = older than tracked history" intuition.
        """
        if task_id is not None:
            _validate_task_id(task_id)
        result = run_git(
            ["for-each-ref", "--format=%(refname)", SNAPSHOT_REF_PREFIX],
            self.cwd,
        )
        if not result.ok:
            return []
        snapshots: list[Snapshot] = []
        for line in result.stdout.strip().splitlines():
            ref = line.strip()
            if not ref.startswith(SNAPSHOT_REF_PREFIX):
                continue
            snapshot_id = ref[len(SNAPSHOT_REF_PREFIX) :]
            snap = self.get(snapshot_id)
            if snap is None:
                continue
            if task_id is not None and snap.task_id != task_id:
                continue
            snapshots.append(snap)
        snapshots.sort(key=lambda s: s.ts_ns, reverse=True)
        if limit is not None:
            snapshots = snapshots[:limit]
        return snapshots

    # ------------------------------------------------------------------
    # Diff
    # ------------------------------------------------------------------

    def diff(self, a: str, b: str) -> str:
        """Return ``git diff --stat`` between two snapshot trees.

        We deliberately default to ``--stat`` rather than a full diff -
        the typical operator question is *"what changed between these
        two snapshots?"*, and rendering full patch text in a terminal
        is rarely useful. Callers that want a full diff can call
        ``run_git(["diff", a_tree, b_tree])`` directly.
        """
        snap_a = self.get(a)
        snap_b = self.get(b)
        if snap_a is None:
            raise SnapshotError(f"snapshot {a!r} not found")
        if snap_b is None:
            raise SnapshotError(f"snapshot {b!r} not found")
        result = run_git(["diff", "--stat", snap_a.tree_sha, snap_b.tree_sha], self.cwd)
        if not result.ok:
            raise SnapshotError(f"git diff failed: {result.stderr.strip()}")
        return result.stdout

    # ------------------------------------------------------------------
    # Undo
    # ------------------------------------------------------------------

    def undo(self, snapshot_id: str, *, allow_dirty: bool = False) -> Snapshot:
        """Restore the work tree to the snapshot's tree.

        Args:
            snapshot_id: ID of the snapshot to restore.
            allow_dirty: When ``False`` (the default), refuse the undo
                if there are uncommitted changes that are not already
                captured by a sibling snapshot. This is a deliberate
                guardrail: operators almost always want to take a fresh
                snapshot before rewinding so they can re-apply the
                discarded work later.

        Returns:
            The restored :class:`Snapshot`.

        Raises:
            SnapshotError: If the snapshot does not exist, the work
                tree is dirty and ``allow_dirty`` is ``False``, or the
                checkout fails.
        """
        snap = self.get(snapshot_id)
        if snap is None:
            raise SnapshotError(f"snapshot {snapshot_id!r} not found")

        if not allow_dirty:
            status = run_git(["status", "--porcelain"], self.cwd)
            if status.ok and status.stdout.strip():
                raise SnapshotError(
                    "work tree has uncommitted changes; pass allow_dirty=True or take a fresh snapshot before undoing"
                )

        # ``git read-tree --reset -u <tree>`` atomically replaces the
        # work tree contents with *tree* and resets the index. This is
        # the same mechanism ``git checkout`` uses internally but with
        # a tree-ish that isn't on any branch.
        result = run_git(["read-tree", "--reset", "-u", snap.tree_sha], self.cwd)
        if not result.ok:
            raise SnapshotError(f"git read-tree failed: {result.stderr.strip()}")
        logger.info("snapshot.undo id=%s tree=%s", snapshot_id, snap.tree_sha)
        return snap

    # ------------------------------------------------------------------
    # Delete / GC
    # ------------------------------------------------------------------

    def delete(self, snapshot_id: str) -> bool:
        """Remove a snapshot ref and metadata sidecar.

        Returns ``True`` when the ref existed and was deleted. Missing
        snapshots return ``False`` (no error) so the caller can use
        this as an idempotent cleanup primitive.
        """
        _validate_snapshot_id(snapshot_id)
        ref = f"{SNAPSHOT_REF_PREFIX}{snapshot_id}"
        existed = _resolve_ref(self.cwd, ref) is not None
        _delete_ref(self.cwd, ref)
        meta_path = _metadata_path(self.cwd, snapshot_id)
        if meta_path.exists():
            try:
                meta_path.unlink()
            except OSError as exc:  # pragma: no cover - best effort
                logger.warning("failed to remove metadata %s: %s", meta_path, exc)
        return existed

    def gc(self, *, older_than_days: int = DEFAULT_GC_DAYS) -> builtins.list[str]:
        """Delete snapshots older than *older_than_days* days.

        Returns the list of deleted snapshot IDs so callers can log a
        summary. We deliberately do not run ``git gc`` afterwards: the
        repository's normal maintenance schedule will reclaim the
        orphaned tree objects on the next pass.
        """
        # Negative retention windows would push the cutoff into the future
        # and delete every current snapshot. Fail fast instead of silently
        # interpreting the input as "delete everything".
        if older_than_days < 0:
            raise SnapshotError("older_than_days must be non-negative")
        cutoff_ns = time.time_ns() - older_than_days * 86_400 * 1_000_000_000
        deleted = [
            snap.snapshot_id
            for snap in self.list()
            if snap.ts_ns and snap.ts_ns < cutoff_ns and self.delete(snap.snapshot_id)
        ]
        return deleted


# ----------------------------------------------------------------------
# Stacked branches
# ----------------------------------------------------------------------


def stack_push(
    cwd: Path,
    *,
    task_id: str,
    branch: str,
    parent_branch: str | None = None,
) -> StackEntry:
    """Record *branch* as the next entry in a task's branch stack.

    The function is purely a metadata operation - it never creates the
    branch itself (the orchestrator already has worktree-creation code
    that picks branch names) and never moves HEAD. All it does is link
    *branch* to its predecessor so :func:`stack_list` can return a
    chronological view of the task's agent runs.

    Args:
        cwd: Repository root.
        task_id: Bernstein task ID. Subject to :data:`_TASK_ID_RE`.
        branch: Name of the branch just created for the new agent run.
        parent_branch: Name of the previous branch in the stack. If
            ``None`` we look at the latest existing stack entry for the
            task; this is the most common case.

    Returns:
        The created :class:`StackEntry`.
    """
    _validate_task_id(task_id)
    _ensure_repo(Path(cwd))

    existing = stack_list(cwd, task_id=task_id)
    index = len(existing) + 1
    resolved_parent = parent_branch
    if resolved_parent is None and existing:
        resolved_parent = existing[-1].branch

    ref = _stack_ref(task_id, index)
    # We store the parent branch sha to anchor the relationship even if
    # the branch is later renamed. Falling back to the branch name keeps
    # the entry usable on local-only repos that never resolve the ref.
    parent_sha = _resolve_ref(Path(cwd), f"refs/heads/{resolved_parent}") if resolved_parent else None
    branch_sha = _resolve_ref(Path(cwd), f"refs/heads/{branch}")
    if branch_sha is None:
        raise SnapshotError(f"branch {branch!r} does not exist")
    _update_ref(Path(cwd), ref, branch_sha)

    entry = StackEntry(
        task_id=task_id,
        index=index,
        branch=branch,
        parent_branch=resolved_parent,
        ref=ref,
    )
    _stack_metadata_path(Path(cwd), task_id, index).write_text(
        json.dumps(
            {
                "task_id": task_id,
                "index": index,
                "branch": branch,
                "parent_branch": resolved_parent,
                "parent_sha": parent_sha,
                "branch_sha": branch_sha,
                "ref": ref,
                "ts_ns": time.time_ns(),
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return entry


def stack_list(cwd: Path, *, task_id: str) -> list[StackEntry]:
    """Return the ordered list of stack entries for *task_id*.

    Sorted by index ascending so the caller can render the stack
    top-down (oldest at top, newest at bottom).
    """
    _validate_task_id(task_id)
    _ensure_repo(Path(cwd))
    prefix = f"{STACK_REF_PREFIX}{task_id}/"
    result = run_git(["for-each-ref", "--format=%(refname)", prefix], Path(cwd))
    if not result.ok:
        return []
    entries: list[StackEntry] = []
    for line in result.stdout.strip().splitlines():
        ref = line.strip()
        if not ref.startswith(prefix):
            continue
        suffix = ref[len(prefix) :]
        try:
            index = int(suffix)
        except ValueError:
            continue
        meta_path = _stack_metadata_path(Path(cwd), task_id, index)
        if not meta_path.exists():
            continue
        try:
            payload = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        entries.append(
            StackEntry(
                task_id=task_id,
                index=index,
                branch=str(payload.get("branch", "")),
                parent_branch=payload.get("parent_branch"),
                ref=ref,
            )
        )
    entries.sort(key=lambda e: e.index)
    return entries


def stack_clear(cwd: Path, *, task_id: str) -> int:
    """Delete every stack entry for *task_id*.

    Returns the number of entries removed. Used when a task is closed
    so the ref namespace does not grow unbounded.
    """
    _validate_task_id(task_id)
    entries = stack_list(cwd, task_id=task_id)
    for entry in entries:
        _delete_ref(Path(cwd), entry.ref)
        meta_path = _stack_metadata_path(Path(cwd), task_id, entry.index)
        if meta_path.exists():
            try:
                meta_path.unlink()
            except OSError as exc:  # pragma: no cover - best effort
                logger.warning("failed to remove stack metadata %s: %s", meta_path, exc)
    return len(entries)


__all__ = [
    "DEFAULT_GC_DAYS",
    "SNAPSHOT_REF_PREFIX",
    "STACK_REF_PREFIX",
    "Snapshot",
    "SnapshotError",
    "SnapshotStore",
    "StackEntry",
    "stack_clear",
    "stack_list",
    "stack_push",
]
