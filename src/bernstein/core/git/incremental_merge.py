"""Incremental merge support for long-running agents.

Allows an agent to merge a subset of its completed files into the main branch
*before* the full task finishes.  A test-writing agent can push the first five
test files while still writing the remaining five, reducing wall-clock time.

Public API
----------
- :func:`incremental_merge_files` — merge specific committed files from an
  agent's branch into the current branch of the main repo.
- :func:`get_incremental_merge_state` — read which files have already been
  merged for a given session.
- :class:`IncrementalMergeResult` — result of a single partial merge.
- :class:`IncrementalMergeState` — cumulative state across all partial merges
  for one agent session.

Design notes
------------
The merge is performed by running ``git checkout agent/<session_id> -- <files>``
in the *main* repo (not the worktree).  This copies the exact file contents
from the agent's branch without triggering a full merge commit.  A follow-up
``git commit`` records the snapshot with a descriptive message.

Files that have not been committed to the agent's branch yet are returned as
``uncommitted_files`` and skipped — the agent must commit them in its worktree
first.  Already-merged files (recorded in the state file) are also skipped to
prevent double-merging.

State persistence
-----------------
Merge state is written to ``.sdd/runtime/incremental_merges/<session_id>.json``
so it survives server restarts and can be consulted at final-merge time to
avoid re-applying already-merged changes.
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from bernstein.core.git_basic import run_git

if TYPE_CHECKING:
    from pathlib import Path

    pass

logger = logging.getLogger(__name__)

# One lock per session prevents concurrent partial-merge calls for the same
# session from racing each other.  A global lock would unnecessarily serialise
# merges from different sessions.
_SESSION_LOCKS: dict[str, threading.Lock] = {}
_SESSION_LOCKS_GUARD = threading.Lock()

_STATE_DIR_NAME = "incremental_merges"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class IncrementalMergeResult:
    """Result of a single :func:`incremental_merge_files` call.

    Attributes:
        success: True when at least one file was merged and committed.
        merged_files: Files successfully written to the main branch.
        skipped_already_merged: Files that were already merged in a prior
            partial-merge call (skipped to avoid double-applying).
        uncommitted_files: Files the agent has not yet committed to its branch
            (skipped — caller must commit them in the worktree first).
        conflicting_files: Files whose content conflicted during checkout.
        commit_sha: SHA of the incremental commit, or empty string if nothing
            was committed (e.g. all files skipped).
        error: Human-readable error message on failure, empty on success.
    """

    success: bool
    merged_files: list[str]
    skipped_already_merged: list[str]
    uncommitted_files: list[str]
    conflicting_files: list[str]
    commit_sha: str
    error: str


@dataclass
class IncrementalMergeState:
    """Cumulative partial-merge history for one agent session.

    Attributes:
        session_id: The agent session this state belongs to.
        merged_files: All files that have been incrementally merged so far
            (union across all partial-merge calls).
        merge_commits: Git SHAs of the incremental merge commits, in order.
        last_merged_ts: Unix timestamp of the most recent partial merge.
    """

    session_id: str
    merged_files: list[str] = field(default_factory=list)
    merge_commits: list[str] = field(default_factory=list)
    last_merged_ts: float = 0.0

    def to_dict(self) -> dict[str, object]:
        """Serialise to a JSON-friendly dict."""
        return {
            "session_id": self.session_id,
            "merged_files": self.merged_files,
            "merge_commits": self.merge_commits,
            "last_merged_ts": self.last_merged_ts,
        }

    @classmethod
    def from_dict(cls, data: dict[str, object]) -> IncrementalMergeState:
        """Deserialise from a dict read from the state file."""
        return cls(
            session_id=str(data.get("session_id", "")),
            merged_files=list(data.get("merged_files", [])),  # type: ignore[arg-type]
            merge_commits=list(data.get("merge_commits", [])),  # type: ignore[arg-type]
            last_merged_ts=float(data.get("last_merged_ts", 0.0)),  # type: ignore[arg-type]
        )


# ---------------------------------------------------------------------------
# State persistence helpers
# ---------------------------------------------------------------------------


def _state_path(runtime_dir: Path, session_id: str) -> Path:
    """Return the path of the state file for *session_id*."""
    return runtime_dir / _STATE_DIR_NAME / f"{session_id}.json"


def _load_state(runtime_dir: Path, session_id: str) -> IncrementalMergeState:
    """Load merge state from disk, returning an empty state on miss."""
    path = _state_path(runtime_dir, session_id)
    if not path.exists():
        return IncrementalMergeState(session_id=session_id)
    try:
        raw: object = json.loads(path.read_text())
        if isinstance(raw, dict):
            return IncrementalMergeState.from_dict(raw)  # type: ignore[arg-type]
    except Exception as exc:
        logger.warning("Failed to load incremental merge state for %s: %s", session_id, exc)
    return IncrementalMergeState(session_id=session_id)


def _save_state(runtime_dir: Path, state: IncrementalMergeState) -> None:
    """Persist *state* to disk (atomic write via temp file)."""
    state_dir = runtime_dir / _STATE_DIR_NAME
    state_dir.mkdir(parents=True, exist_ok=True)
    path = _state_path(runtime_dir, state.session_id)
    tmp_path = path.with_suffix(".tmp")
    try:
        tmp_path.write_text(json.dumps(state.to_dict(), indent=2))
        tmp_path.replace(path)
    except Exception as exc:
        logger.warning("Failed to save incremental merge state for %s: %s", state.session_id, exc)
        with contextlib.suppress(Exception):
            tmp_path.unlink(missing_ok=True)


def get_incremental_merge_state(runtime_dir: Path, session_id: str) -> IncrementalMergeState:
    """Return cumulative partial-merge state for *session_id*.

    Args:
        runtime_dir: Path to ``.sdd/runtime/``.
        session_id: The agent session identifier.

    Returns:
        :class:`IncrementalMergeState` loaded from disk; an empty state if no
        partial merges have been performed yet.
    """
    return _load_state(runtime_dir, session_id)


# ---------------------------------------------------------------------------
# Session lock
# ---------------------------------------------------------------------------


def _get_session_lock(session_id: str) -> threading.Lock:
    """Return (creating if needed) the per-session lock for *session_id*."""
    with _SESSION_LOCKS_GUARD:
        if session_id not in _SESSION_LOCKS:
            _SESSION_LOCKS[session_id] = threading.Lock()
        return _SESSION_LOCKS[session_id]


# ---------------------------------------------------------------------------
# Git helpers
# ---------------------------------------------------------------------------


def _files_committed_in_branch(workdir: Path, branch: str, files: list[str]) -> set[str]:
    """Return which of *files* are present (committed) in *branch*.

    Uses ``git ls-tree`` which is read-only and does not touch the index.

    Args:
        workdir: Main repository root.
        branch: Branch name to check (e.g. ``"agent/backend-abc123"``).
        files: Relative paths to test.

    Returns:
        Set of file paths that exist in *branch*.
    """
    if not files:
        return set()
    result = run_git(["ls-tree", "--name-only", "-r", branch, "--", *files], workdir)
    if not result.ok:
        logger.debug("ls-tree failed for branch %s: %s", branch, result.stderr.strip())
        return set()
    committed = {line.strip() for line in result.stdout.splitlines() if line.strip()}
    return committed


def _checkout_files_from_branch(workdir: Path, branch: str, files: list[str]) -> list[str]:
    """Checkout *files* from *branch* into the working tree of *workdir*.

    Runs ``git checkout <branch> -- <files>``.  Returns the subset of files
    that failed (non-empty means partial or full failure).

    Args:
        workdir: Main repository root.
        branch: Source branch (e.g. ``"agent/backend-abc123"``).
        files: Files to copy into the working tree.

    Returns:
        List of files that could not be checked out.
    """
    result = run_git(["checkout", branch, "--", *files], workdir)
    if result.ok:
        return []
    # On failure, report all requested files as failed
    logger.warning("git checkout %s -- files failed: %s", branch, result.stderr.strip())
    return list(files)


def _rev_parse(workdir: Path, ref: str) -> str:
    """Return the full SHA of *ref*, or empty string on failure."""
    result = run_git(["rev-parse", ref], workdir)
    return result.stdout.strip() if result.ok else ""


# ---------------------------------------------------------------------------
# Main public function
# ---------------------------------------------------------------------------


def incremental_merge_files(
    workdir: Path,
    runtime_dir: Path,
    session_id: str,
    files: list[str],
    message: str = "",
    *,
    merge_lock: threading.Lock | None = None,
) -> IncrementalMergeResult:
    """Merge specific committed files from an agent's branch into the current branch.

    Copies the exact file contents from ``agent/<session_id>`` into the main
    repo's working tree and creates a git commit.  Only files that are already
    committed in the agent's branch are processed; others are returned as
    ``uncommitted_files``.

    This is designed to be called from the task-server route while the agent
    is still running.  The final full merge at agent-completion time will still
    happen — but because the incremental-merge state is recorded, you can
    choose to exclude already-merged files from the final merge diff (the
    responsibility lies with the orchestrator's merge logic).

    Args:
        workdir: Main repository root (not the worktree).
        runtime_dir: Path to ``.sdd/runtime/`` for state persistence.
        session_id: Agent session ID (branch = ``agent/<session_id>``).
        files: Repo-relative file paths to merge.  Must be committed in the
            agent's branch; uncommitted files are skipped.
        message: Commit message.  Auto-generated if empty.
        merge_lock: Optional external lock to hold during the git operations.
            Pass the :class:`MergeQueue`'s ``merge_lock`` to serialise all
            merge activity (incremental + final) across sessions.

    Returns:
        :class:`IncrementalMergeResult` describing what was merged.
    """
    if not files:
        return IncrementalMergeResult(
            success=False,
            merged_files=[],
            skipped_already_merged=[],
            uncommitted_files=[],
            conflicting_files=[],
            commit_sha="",
            error="No files specified",
        )

    session_lock = _get_session_lock(session_id)
    branch = f"agent/{session_id}"

    with session_lock:
        # Load current state to find already-merged files
        state = _load_state(runtime_dir, session_id)
        already_merged = set(state.merged_files)

        # Partition requested files
        already_merged_requested = [f for f in files if f in already_merged]
        candidates = [f for f in files if f not in already_merged]

        if not candidates:
            return IncrementalMergeResult(
                success=True,
                merged_files=[],
                skipped_already_merged=already_merged_requested,
                uncommitted_files=[],
                conflicting_files=[],
                commit_sha="",
                error="",
            )

        # Verify which candidates are committed in the agent's branch
        committed = _files_committed_in_branch(workdir, branch, candidates)
        uncommitted = [f for f in candidates if f not in committed]
        to_checkout = [f for f in candidates if f in committed]

        if not to_checkout:
            return IncrementalMergeResult(
                success=False,
                merged_files=[],
                skipped_already_merged=already_merged_requested,
                uncommitted_files=uncommitted,
                conflicting_files=[],
                commit_sha="",
                error=(f"None of the requested files are committed in {branch}. Commit them in the worktree first."),
            )

        # Perform the checkout under the external merge_lock (if provided)
        def _do_merge() -> IncrementalMergeResult:
            failed_files = _checkout_files_from_branch(workdir, branch, to_checkout)
            conflicting = failed_files
            merged = [f for f in to_checkout if f not in failed_files]

            if not merged:
                return IncrementalMergeResult(
                    success=False,
                    merged_files=[],
                    skipped_already_merged=already_merged_requested,
                    uncommitted_files=uncommitted,
                    conflicting_files=conflicting,
                    commit_sha="",
                    error="All files conflicted during checkout",
                )

            # Stage the merged files
            run_git(["add", "--", *merged], workdir)

            # Build commit message
            commit_msg = message or (
                f"Incremental merge from {branch}: " + (", ".join(merged[:3]) + (" …" if len(merged) > 3 else ""))
            )
            commit_result = run_git(["commit", "-m", commit_msg], workdir)
            if not commit_result.ok:
                # Nothing to commit (files were already identical) — still a success
                if "nothing to commit" in commit_result.stdout or "nothing to commit" in commit_result.stderr:
                    logger.info(
                        "Incremental merge for %s: no changes to commit (files identical in both branches)",
                        session_id,
                    )
                    return IncrementalMergeResult(
                        success=True,
                        merged_files=merged,
                        skipped_already_merged=already_merged_requested,
                        uncommitted_files=uncommitted,
                        conflicting_files=conflicting,
                        commit_sha="",
                        error="",
                    )
                logger.warning(
                    "Incremental merge commit failed for %s: %s",
                    session_id,
                    commit_result.stderr.strip(),
                )
                return IncrementalMergeResult(
                    success=False,
                    merged_files=[],
                    skipped_already_merged=already_merged_requested,
                    uncommitted_files=uncommitted,
                    conflicting_files=conflicting,
                    commit_sha="",
                    error=f"git commit failed: {commit_result.stderr.strip()}",
                )

            commit_sha = _rev_parse(workdir, "HEAD")
            logger.info(
                "Incremental merge for session %s: %d file(s) merged, commit %s",
                session_id,
                len(merged),
                commit_sha[:8] if commit_sha else "?",
            )

            # Persist updated state
            state.merged_files = sorted(set(state.merged_files) | set(merged))
            if commit_sha:
                state.merge_commits.append(commit_sha)
            state.last_merged_ts = time.time()
            _save_state(runtime_dir, state)

            return IncrementalMergeResult(
                success=True,
                merged_files=merged,
                skipped_already_merged=already_merged_requested,
                uncommitted_files=uncommitted,
                conflicting_files=conflicting,
                commit_sha=commit_sha,
                error="",
            )

        if merge_lock is not None:
            with merge_lock:
                return _do_merge()
        return _do_merge()
