"""Classify Bernstein worktrees as active / orphan / stale / corrupt.

The classifier is the single source of truth shared by

* the ``bernstein worktrees list`` CLI subcommand,
* ``bernstein worktrees gc`` reaper, and
* the TUI list pane refreshed every 10s.

Inputs (all read-only):

* ``git worktree list --porcelain`` in the project repo - definitive
  list of every git-registered worktree.
* ``.sdd/runtime/pids/<session_id>.json`` - task / worker PID record.
* ``.sdd/traces/<session_id>.jsonl`` - last-trace mtime for staleness.
* The on-disk worktree directory itself for size and ``.git`` presence.

The classifier never modifies state. ``reap_worktree`` performs the only
destructive action and is gated behind ``.sdd/runtime/worktree-gc.lock``.
"""

from __future__ import annotations

import errno
import json
import logging
import os
import shutil
import subprocess
import time
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

logger = logging.getLogger(__name__)

__all__ = [
    "GC_LOCK_RELPATH",
    "STALE_TRACE_AGE_S",
    "WORKTREE_GC_LIFECYCLE_EVENT",
    "WORKTREE_REAP_EVENT",
    "ClassifiedWorktree",
    "WorktreeFingerprint",
    "WorktreeState",
    "classify_worktrees",
    "format_size",
    "iter_worktree_dirs",
    "reap_worktree",
    "worktree_fingerprint",
    "worktrees_root",
]


#: Repo-relative path of the GC lock file. Single-file lock prevents two
#: concurrent operators (or an operator plus a daemon) from reaping the
#: same directory and corrupting git state.
GC_LOCK_RELPATH = ".sdd/runtime/worktree-gc.lock"

#: How old the last trace event must be before a dead-PID worktree is
#: considered ``stale``. Anything younger stays ``active`` so we never
#: race against an agent that briefly lost its PID file.
STALE_TRACE_AGE_S: int = 24 * 60 * 60

#: Lifecycle event identifier emitted for each reaped worktree.
#: Plugins subscribe to this string via the ``bernstein.core.lifecycle``
#: registry. Adding a brand-new enum entry would ripple through the
#: notify bridge, so the classifier uses a free-form event id instead.
WORKTREE_GC_LIFECYCLE_EVENT = "worktree.gc"

#: Branch a worktree's commits are checked against before the worktree is
#: considered free of unmerged work. When every commit on the worktree's
#: HEAD is reachable from this branch the work is already integrated and the
#: worktree is safe to reap. Mirrors ``git_hygiene.DEFAULT_TARGET_BRANCH``.
DEFAULT_INTEGRATION_BRANCH = "main"

#: Issue #1833 - audit event-type appended to the HMAC-chained audit log
#: (``.sdd/audit/``) for every reaped worktree. Distinct from the
#: best-effort lifecycle event above: the audit entry is tamper-evident,
#: signed, and written even when no plugin ``HookRegistry`` is installed.
WORKTREE_REAP_EVENT = "worktree.reap"

#: Timeout (seconds) for the per-worktree ``git`` calls used to capture a
#: pre-deletion fingerprint. Kept short so a slow/hung git on a corrupt
#: worktree degrades to "unknown" quickly rather than stalling GC.
_FINGERPRINT_GIT_TIMEOUT_S = 10


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


class WorktreeState(StrEnum):
    """Deterministic state assigned to every worktree.

    The four states are mutually exclusive; the classifier picks the
    first matching rule in this order: ``corrupt`` > ``orphan`` >
    ``stale`` > ``active``.
    """

    ACTIVE = "active"
    ORPHAN = "orphan"
    STALE = "stale"
    CORRUPT = "corrupt"


@dataclass(frozen=True, slots=True)
class ClassifiedWorktree:
    """One row in the ``bernstein worktrees list`` table.

    Attributes:
        path: Absolute filesystem path of the worktree directory.
        session_id: Directory basename - Bernstein uses the session id
            as the worktree slug, so this also identifies the owning
            task when one exists.
        task_id: Task identifier from the PID record, or ``None`` when
            no task record was found.
        state: Classified :class:`WorktreeState`.
        age_seconds: Wall-clock age of the worktree directory, computed
            from its ``ctime`` (creation when the FS reports it,
            metadata-change otherwise).
        size_bytes: Recursive size on disk in bytes (best effort -
            unreadable entries are skipped silently).
        pid: Worker PID read from the task record, or ``None``.
        pid_alive: Whether ``os.kill(pid, 0)`` succeeded. ``False`` when
            ``pid`` is ``None``.
        last_trace_mtime: Unix timestamp of the most recent trace
            event, or ``None`` if no trace file exists.
        has_unsaved_work: ``True`` when the worktree still holds work that
            a reap would destroy - a dirty working tree
            (``git status --porcelain`` non-empty), commits on the
            worktree branch that are not reachable from the integration
            branch, or - for a ``CORRUPT`` directory git cannot probe -
            tracked-looking content on disk. The probe runs *inside* each
            candidate worktree, so the guarantee holds independently per
            per-task git worktree.
    """

    path: Path
    session_id: str
    task_id: str | None
    state: WorktreeState
    age_seconds: float
    size_bytes: int
    pid: int | None
    pid_alive: bool
    last_trace_mtime: float | None
    has_unsaved_work: bool = False

    @property
    def is_reapable(self) -> bool:
        """Return ``True`` when the worktree is safe to delete.

        A worktree is reapable only when it is in a terminal state
        (``ORPHAN``/``STALE``/``CORRUPT``) *and* the classifier proved it
        carries no unsaved work. ``has_unsaved_work`` vetoes the reap
        regardless of state, so a directory holding the only copy of
        unmerged commits or uncommitted edits is never silently deleted.
        The ``bernstein worktrees gc --force-unsaved`` path overrides this
        veto explicitly; the classifier itself never does.
        """
        if self.has_unsaved_work:
            return False
        return self.state in (WorktreeState.ORPHAN, WorktreeState.STALE, WorktreeState.CORRUPT)


@dataclass(frozen=True, slots=True)
class WorktreeFingerprint:
    """Pre-deletion content fingerprint of a worktree (issue #1833).

    Captured *before* :func:`reap_worktree` destroys the directory so the
    audit entry proves what state the worktree was in at deletion time. A
    ``corrupt`` worktree may have no readable ``.git``; in that case both
    fields degrade to ``None`` (rendered ``"unknown"``/``null`` in the
    audit payload) rather than raising.

    Attributes:
        head_sha: Full git HEAD sha of the worktree, or ``None`` when git
            could not resolve it (corrupt/unreadable ``.git``).
        dirty: ``True`` if the worktree had uncommitted/unmerged changes,
            ``False`` if clean, ``None`` when the working-tree state could
            not be determined.
    """

    head_sha: str | None
    dirty: bool | None


# ---------------------------------------------------------------------------
# Public helpers
# ---------------------------------------------------------------------------


def worktrees_root(repo_root: Path) -> Path:
    """Return the directory under which Bernstein stores agent worktrees.

    The spec describes ``.sdd/runtime/worktrees/`` while the current
    codebase still writes to ``.sdd/worktrees/``. We honour the new
    location when it exists, otherwise fall back to the legacy path.
    """
    runtime = repo_root / ".sdd" / "runtime" / "worktrees"
    if runtime.is_dir():
        return runtime
    return repo_root / ".sdd" / "worktrees"


def iter_worktree_dirs(repo_root: Path) -> list[Path]:
    """Return every directory that looks like an agent worktree.

    Skips the ``.locks`` bookkeeping directory used by
    :mod:`bernstein.core.git.worktree`.
    """
    root = worktrees_root(repo_root)
    if not root.is_dir():
        return []
    entries: list[Path] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        if entry.name.startswith(".") or entry.name == "locks":
            continue
        entries.append(entry)
    return entries


def format_size(size_bytes: int) -> str:
    """Render a byte count as a short human-readable string."""
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(size_bytes)
    for unit in units:
        if size < 1024.0 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} {unit}"
            return f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size_bytes} B"


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


def classify_worktrees(
    repo_root: Path,
    *,
    now: float | None = None,
    stale_trace_age_s: int = STALE_TRACE_AGE_S,
) -> list[ClassifiedWorktree]:
    """Classify every Bernstein worktree under ``repo_root``.

    Args:
        repo_root: Absolute path to the repository root.
        now: Override the wall-clock for tests; defaults to ``time.time()``.
        stale_trace_age_s: Trace freshness threshold in seconds.

    Returns:
        One :class:`ClassifiedWorktree` per directory, sorted by name.
    """
    clock = time.time() if now is None else now
    rows: list[ClassifiedWorktree] = [
        _classify_one(
            path,
            repo_root=repo_root,
            now=clock,
            stale_trace_age_s=stale_trace_age_s,
        )
        for path in iter_worktree_dirs(repo_root)
    ]
    return rows


def _classify_one(
    path: Path,
    *,
    repo_root: Path,
    now: float,
    stale_trace_age_s: int,
) -> ClassifiedWorktree:
    session_id = path.name
    size_bytes = _dir_size(path)
    age_seconds = _dir_age(path, now=now)

    # 1. Corrupt - directory exists but git can't see a .git anchor.
    # Without a ``.git`` anchor we cannot run any git probe, so we fall back
    # to a filesystem check: an empty directory is safe to reap, while one
    # holding files may carry the only copy of an agent's output and is
    # surfaced for manual handling instead of being deleted blindly.
    git_anchor = path / ".git"
    if not git_anchor.exists():
        return ClassifiedWorktree(
            path=path,
            session_id=session_id,
            task_id=None,
            state=WorktreeState.CORRUPT,
            age_seconds=age_seconds,
            size_bytes=size_bytes,
            pid=None,
            pid_alive=False,
            last_trace_mtime=None,
            has_unsaved_work=_corrupt_dir_has_content(path),
        )

    # Load the task record, if any.
    pid_record = _read_pid_record(repo_root, session_id)
    task_id = pid_record.get("task_id") if pid_record else None
    pid = _coerce_pid(pid_record)
    alive = pid is not None and _process_alive(pid)
    last_trace_mtime = _last_trace_mtime(repo_root, session_id)

    # 2. Orphan - directory has no task record at all. A missing PID record
    # is exactly the crash-recovery case where committed-but-unmerged work
    # is most likely stranded, so probe the worktree before allowing a reap.
    if pid_record is None:
        return ClassifiedWorktree(
            path=path,
            session_id=session_id,
            task_id=None,
            state=WorktreeState.ORPHAN,
            age_seconds=age_seconds,
            size_bytes=size_bytes,
            pid=pid,
            pid_alive=alive,
            last_trace_mtime=last_trace_mtime,
            has_unsaved_work=_probe_unsaved_work(path, repo_root),
        )

    # 3. Active - task record exists and PID is alive.
    if alive:
        return ClassifiedWorktree(
            path=path,
            session_id=session_id,
            task_id=task_id if isinstance(task_id, str) else None,
            state=WorktreeState.ACTIVE,
            age_seconds=age_seconds,
            size_bytes=size_bytes,
            pid=pid,
            pid_alive=True,
            last_trace_mtime=last_trace_mtime,
        )

    # 4. Stale - task record exists but PID dead AND last trace > threshold.
    # If trace freshness is below the threshold we cannot prove staleness
    # yet, so leave the worktree marked ``active`` to be safe. The
    # operator can re-run ``gc`` later.
    trace_age = (now - last_trace_mtime) if last_trace_mtime is not None else float("inf")
    if trace_age > stale_trace_age_s:
        return ClassifiedWorktree(
            path=path,
            session_id=session_id,
            task_id=task_id if isinstance(task_id, str) else None,
            state=WorktreeState.STALE,
            age_seconds=age_seconds,
            size_bytes=size_bytes,
            pid=pid,
            pid_alive=False,
            last_trace_mtime=last_trace_mtime,
            has_unsaved_work=_probe_unsaved_work(path, repo_root),
        )

    return ClassifiedWorktree(
        path=path,
        session_id=session_id,
        task_id=task_id if isinstance(task_id, str) else None,
        state=WorktreeState.ACTIVE,
        age_seconds=age_seconds,
        size_bytes=size_bytes,
        pid=pid,
        pid_alive=False,
        last_trace_mtime=last_trace_mtime,
    )


# ---------------------------------------------------------------------------
# Reaper
# ---------------------------------------------------------------------------


def worktree_fingerprint(path: Path) -> WorktreeFingerprint:
    """Capture a worktree's git HEAD sha + dirty flag before deletion.

    Issue #1833: this is the load-bearing forensic capture - it MUST run
    before :func:`reap_worktree` removes the directory, and it MUST never
    crash. A ``corrupt`` worktree (no readable ``.git``) is exactly the
    case most likely to matter, so any git failure degrades to
    ``head_sha=None`` / ``dirty=None`` instead of raising.

    Args:
        path: Absolute path to the (still-present) worktree directory.

    Returns:
        A :class:`WorktreeFingerprint`; both fields are ``None`` when git
        cannot read the worktree.
    """
    # A corrupt worktree (no readable ``.git``) must NOT inherit the parent
    # orchestrator repo's HEAD: git discovery walks up the tree, so running
    # ``git`` with ``cwd`` inside ``.sdd/runtime/worktrees/<sid>`` would
    # otherwise resolve the enclosing repo. We only trust git output when
    # the worktree directory is itself the git top-level - otherwise we
    # degrade to "unknown", which is the correct forensic answer.
    if not _is_own_worktree_root(path):
        return WorktreeFingerprint(head_sha=None, dirty=None)
    head_sha = _git_head_sha(path)
    dirty = _git_is_dirty(path)
    return WorktreeFingerprint(head_sha=head_sha, dirty=dirty)


def _is_own_worktree_root(path: Path) -> bool:
    """Return ``True`` when ``path`` is itself a git work-tree top level.

    Guards against git's upward repo discovery attributing an enclosing
    repository's HEAD to a corrupt worktree that merely lives inside it.
    """
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=path,
            capture_output=True,
            text=True,
            check=False,
            timeout=_FINGERPRINT_GIT_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.debug("fingerprint: git show-toplevel failed for %s: %s", path, exc)
        return False
    if proc.returncode != 0:
        return False
    toplevel = proc.stdout.strip()
    if not toplevel:
        return False
    try:
        return Path(toplevel).resolve() == path.resolve()
    except OSError:
        return False


def _git_head_sha(path: Path) -> str | None:
    """Return the full HEAD sha of the worktree at ``path``, or ``None``.

    Returns ``None`` on any failure (git missing, detached/unborn HEAD,
    corrupt ``.git``, timeout) so the caller can degrade gracefully.
    """
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=path,
            capture_output=True,
            text=True,
            check=False,
            timeout=_FINGERPRINT_GIT_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.debug("fingerprint: git rev-parse failed for %s: %s", path, exc)
        return None
    if proc.returncode != 0:
        return None
    sha = proc.stdout.strip()
    return sha or None


def _git_is_dirty(path: Path) -> bool | None:
    """Return whether the worktree at ``path`` has uncommitted changes.

    ``True`` when ``git status --porcelain`` reports any tracked or
    untracked change, ``False`` when the tree is clean, and ``None`` when
    the state cannot be determined (git failure on a corrupt worktree).
    """
    try:
        proc = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=path,
            capture_output=True,
            text=True,
            check=False,
            timeout=_FINGERPRINT_GIT_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.debug("fingerprint: git status failed for %s: %s", path, exc)
        return None
    if proc.returncode != 0:
        return None
    return bool(proc.stdout.strip())


def _git_has_unmerged_commits(path: Path, integration_branch: str) -> bool | None:
    """Return whether the worktree branch carries commits not yet integrated.

    A worktree HEAD is "merged" when every one of its commits is reachable
    from ``integration_branch`` - i.e. ``git merge-base --is-ancestor HEAD
    <integration_branch>`` succeeds. That mirrors
    :func:`git_hygiene._is_branch_merged` and never reports false "ahead"
    commits for a branch that was merged but not fast-forwarded locally.

    When the integration branch is missing (a throw-away clone, a repo that
    renamed ``main``) the ancestor check cannot decide, so we fall back to
    "is HEAD ahead of its configured upstream" via
    ``git rev-list --count @{upstream}..HEAD``. If neither ref resolves we
    return ``None`` (undecided) and the caller preserves the worktree.

    Returns:
        ``True`` when the branch has unmerged/unpushed commits, ``False``
        when it is fully integrated, ``None`` when git could not decide.
    """
    head = _git_head_sha(path)
    if head is None:
        # No resolvable HEAD (unborn branch, detached empty tree). There is
        # no commit that a reap would strand, so report "merged".
        return False

    ancestor = _run_probe_git(path, ["merge-base", "--is-ancestor", "HEAD", integration_branch])
    if ancestor is not None and ancestor.returncode == 0:
        # Every HEAD commit is reachable from the integration branch.
        return False
    if ancestor is not None and ancestor.returncode == 1 and not ancestor.stderr.strip():
        # Definitive "not an ancestor" (exit 1, no error text) - unmerged.
        return True

    # The integration branch is unknown or git errored. Fall back to the
    # upstream-ahead heuristic so a missing ``main`` does not defeat the GC.
    ahead = _run_probe_git(path, ["rev-list", "--count", "@{upstream}..HEAD"])
    if ahead is not None and ahead.returncode == 0:
        count = ahead.stdout.strip()
        return count.isdigit() and int(count) > 0

    # Could not decide either way - preserve the worktree on uncertainty.
    return None


def _run_probe_git(path: Path, args: list[str]) -> subprocess.CompletedProcess[str] | None:
    """Run a read-only git probe inside ``path`` with the fingerprint timeout.

    Returns ``None`` (rather than raising) on any spawn/timeout failure so a
    hung or corrupt worktree degrades to "undecided" instead of stalling
    ``gc``.
    """
    try:
        return subprocess.run(
            ["git", *args],
            cwd=path,
            capture_output=True,
            text=True,
            check=False,
            timeout=_FINGERPRINT_GIT_TIMEOUT_S,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.debug("probe: git %s failed for %s: %s", " ".join(args), path, exc)
        return None


def _corrupt_dir_has_content(path: Path) -> bool:
    """Return ``True`` when a ``.git``-less directory still holds any file.

    A ``CORRUPT`` worktree cannot be probed with git, so we cannot prove it
    is empty of unmerged work. We treat a directory that contains any file
    (recursively, at any depth) as carrying possible unsaved work and leave
    it for manual handling; a genuinely empty directory is safe to reap.
    Errors walking the tree degrade to ``True`` (preserve) - never ``False``.
    """
    try:
        for _root, _dirs, files in os.walk(path, onerror=_raise_walk_error):
            if files:
                return True
    except OSError as exc:
        logger.debug("corrupt-probe: walk failed for %s: %s", path, exc)
        return True
    return False


def _raise_walk_error(exc: OSError) -> None:
    """``os.walk`` error callback that re-raises so the caller can preserve."""
    raise exc


def _probe_unsaved_work(path: Path, repo_root: Path, *, integration_branch: str = DEFAULT_INTEGRATION_BRANCH) -> bool:
    """Return ``True`` when reaping ``path`` would destroy unsaved work.

    The probe runs git *inside the worktree itself* so the guarantee is
    evaluated per-task git worktree, independently of every other worktree.
    Two cheap, local, read-only git calls are made:

    1. ``git status --porcelain`` - any tracked or untracked change.
    2. ``git merge-base --is-ancestor HEAD <integration_branch>`` (with an
       upstream-ahead fallback) - commits that exist only on this branch.

    A worktree whose directory is not its own git top level (git's upward
    discovery would otherwise resolve the enclosing orchestrator repo) is
    conservatively treated as carrying unsaved work, because we cannot probe
    it safely. Any git failure on a probe degrades to "preserve" - the GC
    only ever blocks deletion on uncertainty, it never deletes more.

    Args:
        path: Absolute path to the candidate worktree directory.
        repo_root: Repository root (unused by the probe today; kept so the
            signature can grow a per-repo integration-branch lookup without
            churning every call site).
        integration_branch: Branch HEAD must be contained in to count as
            merged. Defaults to :data:`DEFAULT_INTEGRATION_BRANCH`.

    Returns:
        ``True`` when the worktree holds uncommitted changes or unmerged
        commits (or could not be probed safely); ``False`` only when both
        probes proved the worktree clean and fully integrated.
    """
    del repo_root  # reserved for a future per-repo integration-branch lookup
    if not _is_own_worktree_root(path):
        # Cannot trust git output here without leaking the enclosing repo's
        # state; preserve rather than risk a wrong reap.
        return True

    dirty = _git_is_dirty(path)
    if dirty is None or dirty:
        # ``None`` (undecidable) is treated as dirty: preserve on doubt.
        return True

    unmerged = _git_has_unmerged_commits(path, integration_branch)
    # ``None`` (undecidable) is treated as unmerged: preserve on doubt.
    return unmerged is None or unmerged


def reap_worktree(
    repo_root: Path,
    worktree: ClassifiedWorktree,
    *,
    dry_run: bool = False,
) -> bool:
    """Delete the worktree directory and prune git state.

    The caller MUST hold the GC lock at :data:`GC_LOCK_RELPATH`. This
    function never acquires the lock on its own - leave that decision to
    the CLI / TUI driver so a batch reap takes the lock once.

    Args:
        repo_root: Absolute repository root.
        worktree: Classifier output for the directory to delete.
        dry_run: When ``True``, no filesystem mutation happens; the
            function returns ``True`` to mirror a real successful reap.

    Returns:
        ``True`` when the directory was removed (or would have been in
        dry-run mode); ``False`` if the directory was already gone.
    """
    target = worktree.path
    if not target.exists():
        logger.info("reap: %s already gone, skipping", target)
        return False

    if dry_run:
        logger.info("reap (dry-run): would remove %s", target)
        return True

    try:
        shutil.rmtree(target)
    except FileNotFoundError:
        return False
    except OSError as exc:
        logger.warning("reap: failed to remove %s: %s", target, exc)
        return False

    # Best-effort: tell git the worktree is gone.
    try:
        subprocess.run(
            ["git", "worktree", "prune"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.debug("reap: git worktree prune failed: %s", exc)

    return True


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _read_pid_record(repo_root: Path, session_id: str) -> dict[str, object] | None:
    pid_file = repo_root / ".sdd" / "runtime" / "pids" / f"{session_id}.json"
    if not pid_file.exists():
        return None
    try:
        data = json.loads(pid_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    return data


def _coerce_pid(record: dict[str, object] | None) -> int | None:
    if record is None:
        return None
    candidate = record.get("worker_pid") or record.get("pid")
    if candidate is None:
        return None
    try:
        value = int(candidate)  # type: ignore[call-overload]  # dynamic parse
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _process_alive(pid: int) -> bool:
    """Return ``True`` when ``pid`` is a live process.

    Uses ``os.kill(pid, 0)`` and treats ``EPERM`` as alive (the process
    exists but we lack permission to signal it).
    """
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError as exc:
        if exc.errno == errno.ESRCH:
            return False
        return exc.errno == errno.EPERM
    return True


def _last_trace_mtime(repo_root: Path, session_id: str) -> float | None:
    trace_file = repo_root / ".sdd" / "traces" / f"{session_id}.jsonl"
    try:
        return trace_file.stat().st_mtime
    except OSError:
        return None


def _dir_size(path: Path) -> int:
    total = 0
    for root, _dirs, files in os.walk(path, onerror=lambda _exc: None):
        for name in files:
            try:
                total += (Path(root) / name).stat().st_size
            except OSError:
                continue
    return total


def _dir_age(path: Path, *, now: float) -> float:
    try:
        stat = path.stat()
    except OSError:
        return 0.0
    # ``st_birthtime`` is more accurate where available (macOS, BSD);
    # fall back to ``st_ctime`` on Linux.
    birth = getattr(stat, "st_birthtime", None)
    created = birth if isinstance(birth, (int, float)) else stat.st_ctime
    return max(0.0, now - float(created))
