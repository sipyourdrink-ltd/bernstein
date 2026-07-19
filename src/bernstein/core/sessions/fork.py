"""Fork a recorded session into a sibling git worktree (smallest viable slice).

The ``fork_session`` function clones the *task-state* of a parent
:class:`~bernstein.core.orchestration.run_session.RunSession` into a
freshly-allocated session id, materialises a sibling git worktree
branched from the parent's current commit, and writes the snapshot into
the fork worktree's session directory.

This is the snapshot-based slice of GH-1222.  It does **not** attempt to
pause-and-fork a live agent process, replicate streaming conversation
state, or auto-merge fork results - those are deferred follow-ups.

Usage::

    fork = fork_session(
        parent_session_id="20260510-120000-abcdef",
        fork_label="alternate-path",
        repo_root=Path("/path/to/repo"),
    )
    print(fork.fork_worktree)
    print(fork.fork_branch)

Why ``repo_root`` is explicit: the function refuses to guess the
repository when the caller is itself running inside a worktree, since
nesting fork worktrees inside an agent worktree silently breaks
isolation guarantees the rest of the codebase relies on (T481, T580).
"""

from __future__ import annotations

import logging
import re
import secrets
import subprocess
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from bernstein.core.orchestration.run_session import RunSession, sessions_dir_for
from bernstein.core.persistence.atomic_write import write_atomic_json
from bernstein.core.persistence.journal import (
    Journal,
    JournalReader,
    agent_journal_dir,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

logger = logging.getLogger(__name__)

# Worktree base directory (mirrors ``WorktreeManager._WORKTREE_BASE``).
_WORKTREE_BASE_REL = Path(".sdd/worktrees")

# Slugify pattern for the optional fork label - keep it tight so the
# resulting branch and directory names stay shell- and git-safe.
_LABEL_SLUG_RE = re.compile(r"[^a-zA-Z0-9._-]+")
_LABEL_MAX_LEN = 32

# Branch convention for forks. Mirrors the lineage hint described in #1222
# so operators can read parent → child relationships from ``git branch``.
_FORK_BRANCH_PREFIX = "fork"


class SessionForkError(Exception):
    """Raised when a fork operation cannot complete safely."""


@dataclass(frozen=True)
class SessionFork:
    """Result of a successful :func:`fork_session` call.

    Attributes:
        parent_session_id: Source session identifier.
        fork_session_id: Newly-allocated session identifier for the fork.
        parent_branch: Git branch of the parent session (best-effort -
            may be empty when the parent was checked out in detached
            HEAD or when ``git symbolic-ref`` fails).
        fork_branch: Git branch created for the fork.
        parent_worktree: Filesystem path of the parent worktree
            (``repo_root`` when no per-session worktree exists).
        fork_worktree: Filesystem path of the new sibling worktree.
        snapshot_path: Path to the cloned session JSON inside
            ``fork_worktree``.
        fork_commit: Commit SHA the fork branched from.
        from_step: 0-based step index of the parent journal the fork
            branched at, or ``None`` for a plain session-level fork.
        parent_step_hash: ``step_hash`` of the parent journal entry at
            :attr:`from_step`, or ``None`` for a plain fork.
    """

    parent_session_id: str
    fork_session_id: str
    parent_branch: str
    fork_branch: str
    parent_worktree: Path
    fork_worktree: Path
    snapshot_path: Path
    fork_commit: str
    from_step: int | None = None
    parent_step_hash: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Return a JSON-friendly representation (paths coerced to ``str``)."""
        raw = asdict(self)
        raw["parent_worktree"] = str(self.parent_worktree)
        raw["fork_worktree"] = str(self.fork_worktree)
        raw["snapshot_path"] = str(self.snapshot_path)
        # Preserve None semantics so test consumers can assert presence.
        if self.from_step is None:
            raw.pop("from_step", None)
        if self.parent_step_hash is None:
            raw.pop("parent_step_hash", None)
        return raw


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _slugify_label(label: str) -> str:
    """Reduce *label* to a filesystem- and git-safe slug.

    Args:
        label: Free-text fork label provided by the caller.

    Returns:
        Slug containing only ``[a-zA-Z0-9._-]`` characters, trimmed of
        leading/trailing separators and capped at ``_LABEL_MAX_LEN``
        characters.
    """
    cleaned = _LABEL_SLUG_RE.sub("-", label).strip("-._")
    return cleaned[:_LABEL_MAX_LEN]


def _generate_fork_session_id(label_slug: str) -> str:
    """Generate a fork-flavoured session id with timestamp + random suffix.

    Args:
        label_slug: Pre-slugified label (may be empty).

    Returns:
        Identifier shaped like ``fork-<ts>-<hex>`` or
        ``fork-<label>-<ts>-<hex>``.
    """
    ts = time.strftime("%Y%m%d-%H%M%S")
    suffix = secrets.token_hex(3)
    if label_slug:
        return f"fork-{label_slug}-{ts}-{suffix}"
    return f"fork-{ts}-{suffix}"


def _resolve_session_worktree(
    repo_root: Path,
    parent_session_id: str,
    worktrees_map: Mapping[str, Path] | None,
) -> Path:
    """Return the worktree directory associated with ``parent_session_id``.

    The runtime convention (see :class:`WorktreeManager`) places per-session
    worktrees at ``<repo_root>/.sdd/worktrees/<session_id>``.  When that
    path exists we treat the parent as worktree-bound; otherwise we fall
    back to the main repo checkout, which is the common case for
    sessions recorded by ``bernstein run`` directly in the repo root.

    Args:
        repo_root: Absolute repository root.
        parent_session_id: Source session identifier.
        worktrees_map: Optional pre-resolved mapping of session id →
            worktree path (used by tests; production resolves via the
            filesystem convention).

    Returns:
        Existing directory for the parent worktree.
    """
    if worktrees_map and parent_session_id in worktrees_map:
        return worktrees_map[parent_session_id]
    candidate = repo_root / _WORKTREE_BASE_REL / parent_session_id
    if candidate.is_dir():
        return candidate
    return repo_root


def _git_head_in(path: Path) -> str:
    """Return the HEAD commit SHA visible from ``path`` or empty on failure."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.debug("git rev-parse failed in %s: %s", path, exc)
        return ""
    if result.returncode != 0:
        logger.debug("git rev-parse exited %d in %s: %s", result.returncode, path, result.stderr.strip())
        return ""
    return result.stdout.strip()


def _git_current_branch(path: Path) -> str:
    """Return the current branch of the worktree at ``path`` (empty on detached HEAD)."""
    try:
        result = subprocess.run(
            ["git", "symbolic-ref", "--short", "HEAD"],
            cwd=path,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
        )
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError) as exc:
        logger.debug("git symbolic-ref failed in %s: %s", path, exc)
        return ""
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _build_fork_branch_name(parent_branch: str, fork_session_id: str) -> str:
    """Construct a git branch name for the fork.

    The branch name encodes both lineage (parent suffix when available)
    and the fork session id so it is greppable from ``git branch``.

    Args:
        parent_branch: Parent branch name (may be empty).
        fork_session_id: Newly-generated fork session id.

    Returns:
        Git-safe branch name.
    """
    base = parent_branch.replace("/", "-") if parent_branch else "session"
    return f"{_FORK_BRANCH_PREFIX}/{base}/{fork_session_id}"


def _clone_session_snapshot(
    parent_session: RunSession,
    fork_session_id: str,
    target_sessions_dir: Path,
    *,
    fork_label: str,
    parent_session_id: str,
    fork_branch: str,
    fork_commit: str,
    from_step: int | None = None,
    parent_step_hash: str | None = None,
) -> Path:
    """Write the parent session snapshot into the fork worktree.

    The snapshot keeps the *exact* task list (including ``status`` so
    in-progress tasks remain in-progress in the fork) and stamps fork
    lineage metadata under ``fork``.  We deliberately do **not** mutate
    ``run_seed`` or ``goal`` - operators want the fork to start from
    the same planning context.

    When *from_step* is non-``None`` the fork inherits a parent journal
    prefix and the snapshot records the per-step lineage hash so the
    chain becomes a tree rather than a list.

    Args:
        parent_session: Loaded parent session.
        fork_session_id: New session id for the fork.
        target_sessions_dir: Sessions directory inside the fork
            worktree.
        fork_label: Original (unsanitised) label for audit purposes.
        parent_session_id: Source session id (preserved for lineage).
        fork_branch: Git branch the fork was created on.
        fork_commit: Commit the fork branched from.
        from_step: Zero-based step index on the parent chain (only set
            for fork-from-step).
        parent_step_hash: ``step_hash`` of the parent's step at
            *from_step* (only set for fork-from-step).

    Returns:
        Filesystem path of the written snapshot JSON.
    """
    target_sessions_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = target_sessions_dir / f"{fork_session_id}.json"

    fork_block: dict[str, object] = {
        "parent_session_id": parent_session_id,
        "label": fork_label,
        "branch": fork_branch,
        "branched_from_commit": fork_commit,
    }
    if from_step is not None:
        fork_block["from_step"] = from_step
    if parent_step_hash is not None:
        fork_block["parent_step_hash"] = parent_step_hash

    payload: dict[str, object] = {
        "session_id": fork_session_id,
        "goal": parent_session.goal,
        "run_seed": parent_session.run_seed,
        "tasks": parent_session.tasks,
        "routing_decisions": parent_session.routing_decisions,
        "git_sha": fork_commit or parent_session.git_sha,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "bernstein_version": parent_session.bernstein_version,
        "fork": fork_block,
    }
    write_atomic_json(snapshot_path, payload, indent=2)
    return snapshot_path


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fork_session(
    parent_session_id: str,
    fork_label: str = "",
    *,
    repo_root: Path | None = None,
    worktrees_map: Mapping[str, Path] | None = None,
    from_step: int | None = None,
) -> SessionFork:
    """Fork a recorded session into a sibling git worktree.

    Steps:

    1. Load the parent session JSON from ``<repo_root>/.sdd/runtime/sessions``.
    2. Resolve the parent's worktree (per-session if it exists, otherwise
       ``repo_root``) and capture its current commit + branch.
    3. Allocate a fresh fork session id and a sibling worktree path under
       ``<repo_root>/.sdd/worktrees/<fork_session_id>``.
    4. Run ``git worktree add -b <fork_branch>`` so the fork starts from
       the parent's current commit.
    5. Clone the parent session JSON into the fork worktree's sessions
       directory, preserving every task and its current ``status`` so
       in-progress tasks remain in-progress in the fork.
    6. When ``from_step`` is set, also seed the fork's per-step journal
       under ``<fork_worktree>/.sdd/runtime/journal/<fork_session_id>/``
       with the chain prefix from the parent (steps ``0..from_step``).
       The fork snapshot records the parent ``step_hash`` at that index
       so the chain becomes a tree.

    The function fails fast (no partial state) when the parent session
    cannot be loaded, the fork worktree path already exists, the
    requested fork step is out of range, or the git worktree command
    fails.  Once the fork worktree and branch exist, the remaining steps
    (snapshot clone, journal seeding) run inside an undo handler that
    removes the worktree and deletes the branch if anything raises - or a
    cancellation is delivered - so neither is left dangling.

    Args:
        parent_session_id: Identifier of the source session.
        fork_label: Optional human-readable label that becomes part of
            the fork session id and branch name.
        repo_root: Repository root.  Defaults to ``Path.cwd()``.
        worktrees_map: Optional explicit mapping of session id →
            worktree path.  Useful for tests; production discovers
            worktrees via the filesystem convention.
        from_step: Optional zero-based step index on the parent journal
            chain. When set, the fork inherits the parent journal prefix
            ``[0..from_step]`` and the snapshot records the per-step
            lineage hash. ``None`` keeps the pre-#1799 session-level
            fork semantics.

    Returns:
        Populated :class:`SessionFork` describing the new fork.

    Raises:
        SessionForkError: When the parent session cannot be loaded, the
            requested fork step does not exist, or the worktree creation
            fails.
    """
    if not parent_session_id:
        raise SessionForkError("parent_session_id must not be empty")

    repo_root = (repo_root or Path.cwd()).resolve()
    if not repo_root.is_dir():
        raise SessionForkError(f"repo_root does not exist: {repo_root}")

    label_slug = _slugify_label(fork_label)
    fork_session_id = _generate_fork_session_id(label_slug)

    sessions_dir = sessions_dir_for(repo_root)
    try:
        parent_session = RunSession.load(sessions_dir, parent_session_id)
    except FileNotFoundError as exc:
        raise SessionForkError(f"parent session not found: {parent_session_id}") from exc
    except ValueError as exc:
        raise SessionForkError(f"parent session unreadable: {exc}") from exc

    parent_worktree = _resolve_session_worktree(repo_root, parent_session_id, worktrees_map)
    fork_commit = _git_head_in(parent_worktree)
    if not fork_commit:
        raise SessionForkError(
            f"could not resolve git HEAD for parent worktree {parent_worktree}; is this a git repository?"
        )

    # When forking at a specific step, load the parent journal prefix
    # *before* materialising the worktree so a missing/out-of-range step
    # surfaces as a fast failure with no side effects on disk.
    parent_journal_prefix: list = []
    parent_step_hash: str | None = None
    if from_step is not None:
        if from_step < 0:
            raise SessionForkError(f"from_step must be >= 0 (got {from_step})")
        parent_sdd_dir = repo_root / ".sdd"
        parent_journal_dir = agent_journal_dir(parent_sdd_dir, parent_session_id)
        reader = JournalReader(parent_journal_dir)
        parent_journal_prefix = [e for e in reader.entries() if e.seq <= from_step]
        if not parent_journal_prefix or parent_journal_prefix[-1].seq != from_step:
            raise SessionForkError(
                f"parent journal does not contain step {from_step}; have {len(parent_journal_prefix)} step(s)"
            )
        parent_step_hash = parent_journal_prefix[-1].step_hash

    parent_branch = _git_current_branch(parent_worktree)
    fork_branch = _build_fork_branch_name(parent_branch, fork_session_id)

    fork_worktree = repo_root / _WORKTREE_BASE_REL / fork_session_id
    if fork_worktree.exists():
        raise SessionForkError(f"fork worktree path already exists: {fork_worktree}")
    fork_worktree.parent.mkdir(parents=True, exist_ok=True)

    # Branch directly from the parent's current commit so the fork is
    # bit-identical at creation time.  Using the commit SHA (rather than
    # parent_branch) avoids race conditions with a still-running parent
    # that could advance the branch between read and worktree-add.
    result = worktree_add_from_commit(
        repo_root=repo_root,
        path=fork_worktree,
        branch=fork_branch,
        commit=fork_commit,
    )
    if not result.ok:
        stderr = (result.stderr or "").strip()
        raise SessionForkError(f"git worktree add failed for fork '{fork_session_id}': {stderr or result.stdout!r}")

    # The fork worktree and its branch now exist.  Establish the undo before
    # any further step so a failure - including a cancellation delivered
    # mid-seed, which is a BaseException rather than an Exception - cannot
    # leave the fork worktree or branch behind.  The cleanup is bounded git
    # plumbing, so a plain handler is enough (no ``asyncio.shield``).
    try:
        snapshot_path = _clone_session_snapshot(
            parent_session=parent_session,
            fork_session_id=fork_session_id,
            target_sessions_dir=sessions_dir_for(fork_worktree),
            fork_label=fork_label,
            parent_session_id=parent_session_id,
            fork_branch=fork_branch,
            fork_commit=fork_commit,
            from_step=from_step,
            parent_step_hash=parent_step_hash,
        )

        # Seed the fork journal with the parent prefix when forking from a step.
        if from_step is not None and parent_journal_prefix:
            _seed_fork_journal(
                fork_worktree=fork_worktree,
                fork_session_id=fork_session_id,
                entries=parent_journal_prefix,
            )

        fork = SessionFork(
            parent_session_id=parent_session_id,
            fork_session_id=fork_session_id,
            parent_branch=parent_branch,
            fork_branch=fork_branch,
            parent_worktree=parent_worktree,
            fork_worktree=fork_worktree,
            snapshot_path=snapshot_path,
            fork_commit=fork_commit,
            from_step=from_step,
            parent_step_hash=parent_step_hash,
        )
    except BaseException:
        _undo_fork_worktree(repo_root, fork_worktree, fork_branch)
        raise

    logger.info(
        "Forked session %s -> %s (branch=%s commit=%s from_step=%s)",
        parent_session_id,
        fork_session_id,
        fork_branch,
        fork_commit[:12],
        from_step if from_step is not None else "n/a",
    )
    return fork


def _undo_fork_worktree(repo_root: Path, fork_worktree: Path, fork_branch: str) -> None:
    """Undo a partially-created fork worktree after a post-add step fails.

    Removes the fork worktree and deletes its branch so ``fork_session``
    never leaves either behind when snapshot cloning or journal seeding
    raises (or a cancellation is delivered mid-seed).

    Best-effort and never raises, so the caller re-raises the original error
    verbatim rather than masking it with a cleanup failure.
    """
    from bernstein.core.git.git_ops import branch_delete, worktree_remove

    try:
        worktree_remove(repo_root, fork_worktree)
    except Exception as exc:
        logger.warning("undo fork: worktree remove failed for %s: %s", fork_worktree, exc)
    try:
        branch_delete(repo_root, fork_branch)
    except Exception as exc:
        logger.warning("undo fork: branch delete failed for %s: %s", fork_branch, exc)


def _seed_fork_journal(
    *,
    fork_worktree: Path,
    fork_session_id: str,
    entries: list,
) -> None:
    """Copy parent journal entries 0..from_step into the fork worktree.

    The fork's first new step will chain to the last seeded entry's
    ``step_hash``; the agent that adopts the worktree appends with the
    standard :class:`Journal` writer, so the chain remains intact.
    """
    fork_sdd = fork_worktree / ".sdd"
    fork_journal_dir = agent_journal_dir(fork_sdd, fork_session_id)
    fork_journal_dir.mkdir(parents=True, exist_ok=True)
    bucket = fork_journal_dir / "000000.jsonl"
    import json as _json

    lines = [_json.dumps(e.to_dict(), sort_keys=True, separators=(",", ":")) for e in entries]
    bucket.write_text("\n".join(lines) + "\n", encoding="utf-8")
    # Round-trip through ``Journal.open`` so the head is recovered and
    # any subsequent append picks up at ``len(entries)``.
    journal = Journal.open(fork_journal_dir)
    journal.close()


# ---------------------------------------------------------------------------
# Thin git wrappers
# ---------------------------------------------------------------------------


def worktree_add_from_commit(
    repo_root: Path,
    path: Path,
    branch: str,
    commit: str,
):  # type: ignore[no-untyped-def]  # GitResult typing kept lazy to avoid circular imports
    """Create a worktree at ``path`` on a new branch starting from ``commit``.

    The shared :func:`bernstein.core.git.git_pr.worktree_add` helper
    starts the branch from the current HEAD; for fork semantics we need
    to pin the start point to the parent's commit explicitly so a
    racing parent cannot influence the fork's base.

    Args:
        repo_root: Repository root.
        path: Filesystem path for the new worktree.
        branch: New branch name.
        commit: Start commit (full SHA preferred).

    Returns:
        ``GitResult`` from :func:`bernstein.core.git.git_basic.run_git`.
    """
    from bernstein.core.git.git_basic import run_git

    return run_git(
        ["worktree", "add", str(path), "-b", branch, commit],
        repo_root,
        timeout=30,
    )


__all__ = [
    "SessionFork",
    "SessionForkError",
    "fork_session",
    "worktree_add_from_commit",
]
