"""Throwaway merge previews: evaluate a gate against the *merged* tree.

An agent commits inside its own worktree on ``agent/<session-id>``.  The run
checkout receives those commits only when the merge-back runs, and the
merge-back runs only after the quality gate has already produced a verdict.
Grading the run checkout therefore grades a tree that is missing exactly the
work under review, so a task whose acceptance signal names a file it produced
can never pass (issue #4367).

:func:`merge_preview` builds the tree the gate is actually asking about: the
agent branch merged onto the run branch, in a detached throwaway worktree.
The run branch is never moved, no ref is created, and the worktree is removed
on every exit path -- success, negative verdict, or exception.

A preview that cannot be built because the branch conflicts with the run
branch is its own outcome, distinct from a failing check: it raises
:class:`MergePreviewConflict` so callers can report a conflict rather than a
test failure.

Usage::

    with merge_preview(repo_root, "agent/qa-1", session_id="qa-1", task_id="t") as tree:
        passed, failed = verify_task_completion(task, tree)
"""

from __future__ import annotations

import logging
import re
import shutil
import uuid
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING

from bernstein.core.git.git_basic import run_git

if TYPE_CHECKING:
    from collections.abc import Iterator, Sequence

logger = logging.getLogger(__name__)

#: Directory (relative to the repo root) holding every preview worktree.
PREVIEW_DIRNAME = "merge-preview"

#: Seconds allowed for the ``worktree add`` / ``merge`` subprocesses.
PREVIEW_TIMEOUT_S = 120

#: Identity used for the preview merge commit.  Explicit so the preview never
#: depends on -- or is blocked by -- the operator's git config or signing setup.
_PREVIEW_GIT_CONFIG = [
    "-c",
    "user.name=bernstein merge-preview",
    "-c",
    "user.email=merge-preview@bernstein.invalid",
    "-c",
    "commit.gpgsign=false",
]

_SLUG_RE = re.compile(r"[^A-Za-z0-9._-]+")


class MergePreviewError(RuntimeError):
    """The merged tree could not be built, so no verdict can be computed on it."""


class MergePreviewConflict(MergePreviewError):
    """The agent branch conflicts with the run branch.

    Attributes:
        branch: The agent branch that could not be merged.
        conflicting_files: Paths git reported as unmerged.
    """

    def __init__(self, branch: str, conflicting_files: list[str]) -> None:
        self.branch = branch
        self.conflicting_files = conflicting_files
        listed = ", ".join(conflicting_files) if conflicting_files else "<unknown>"
        super().__init__(f"{branch} conflicts with the run branch in: {listed}")


def preview_worktree_path(repo_root: Path, *, session_id: str, task_id: str) -> Path:
    """Return a fresh, unique path for one preview worktree.

    Verification runs in parallel through the orchestrator's executor, so the
    path is scoped by task and session and carries a random suffix: two
    previews of the same task never share a directory.

    Args:
        repo_root: The run checkout.
        session_id: Agent session whose branch is being previewed.
        task_id: Task the verdict is being computed for.

    Returns:
        A path under ``<repo_root>/.sdd/runtime/merge-preview/`` that does not
        exist yet.
    """
    slug = _SLUG_RE.sub("-", f"{task_id}-{session_id}").strip("-")[:64] or "preview"
    return repo_root / ".sdd" / "runtime" / PREVIEW_DIRNAME / f"{slug}-{uuid.uuid4().hex[:12]}"


def _conflicting_files(preview: Path) -> list[str]:
    """Return the unmerged paths inside a preview whose merge just failed."""
    result = run_git(["diff", "--name-only", "--diff-filter=U"], preview, timeout=30)
    if not result.ok:
        return []
    return [line.strip() for line in result.stdout.splitlines() if line.strip()]


def _discard_preview(repo_root: Path, preview: Path) -> None:
    """Remove a preview worktree and its git admin entry.  Never raises."""
    try:
        removed = run_git(["worktree", "remove", "--force", str(preview)], repo_root, timeout=60)
        if not removed.ok:
            logger.warning(
                "merge_preview: worktree remove failed for %s: %s",
                preview,
                (removed.stderr or removed.stdout).strip(),
            )
            shutil.rmtree(preview, ignore_errors=True)
    except Exception as exc:  # pragma: no cover - defensive: cleanup never fails a run
        logger.warning("merge_preview: worktree remove raised for %s: %s", preview, exc)
        shutil.rmtree(preview, ignore_errors=True)
    with suppress(Exception):
        run_git(["worktree", "prune"], repo_root, timeout=30)


def _provision(
    repo_root: Path,
    preview: Path,
    symlink_dirs: Sequence[str],
    copy_files: Sequence[str],
) -> None:
    """Give the preview the same shared dirs and per-checkout files an agent worktree gets.

    Best-effort: a preview missing a symlink still answers most checks, and a
    provisioning failure must never be mistaken for a verdict.
    """
    if not symlink_dirs and not copy_files:
        return
    from bernstein.core.git.worktree import _copy_files, _symlink_dirs

    with suppress(Exception):
        _symlink_dirs(repo_root, preview, symlink_dirs)
    with suppress(Exception):
        _copy_files(repo_root, preview, copy_files)


@contextmanager
def merge_preview(
    repo_root: Path,
    branch: str,
    *,
    session_id: str,
    task_id: str,
    symlink_dirs: Sequence[str] = (),
    copy_files: Sequence[str] = (),
) -> Iterator[Path]:
    """Yield a detached worktree holding *branch* merged onto the run branch.

    The worktree is checked out detached at the run branch's current HEAD and
    the merge is committed inside it, so the merged history is what a caller
    inspects.  Because HEAD is detached nothing updates the run branch and no
    ref outlives the block.

    Args:
        repo_root: The run checkout.
        branch: Agent branch to merge in (``agent/<session-id>``).
        session_id: Agent session, used to name the preview directory.
        task_id: Task the verdict is being computed for.
        symlink_dirs: Shared directories to link in from the run checkout, the
            same ones an agent worktree receives. Without them a preview has
            no ``.venv`` / ``node_modules`` and every check that shells out to
            the toolchain fails for a reason that has nothing to do with the
            work under review.
        copy_files: Per-checkout files to copy in (``.env`` and friends), again
            matching what an agent worktree receives.

    Yields:
        Path to the merged tree.

    Raises:
        MergePreviewConflict: *branch* conflicts with the run branch.
        MergePreviewError: The preview could not be created or merged for any
            other reason (missing branch, git failure).  The caller must treat
            this as a negative verdict: falling back to the run checkout would
            grade a tree that does not contain the work under review.
    """
    root = Path(repo_root).resolve()
    preview = preview_worktree_path(root, session_id=session_id, task_id=task_id)
    preview.parent.mkdir(parents=True, exist_ok=True)

    created = run_git(
        ["worktree", "add", "--detach", str(preview), "HEAD"],
        root,
        timeout=PREVIEW_TIMEOUT_S,
    )
    if not created.ok:
        with suppress(Exception):
            run_git(["worktree", "prune"], root, timeout=30)
        shutil.rmtree(preview, ignore_errors=True)
        detail = (created.stderr or created.stdout).strip()
        raise MergePreviewError(f"could not create a merge preview for {branch}: {detail}")

    try:
        merged = run_git(
            [*_PREVIEW_GIT_CONFIG, "merge", "--no-ff", "--no-edit", branch],
            preview,
            timeout=PREVIEW_TIMEOUT_S,
        )
        if not merged.ok:
            conflicts = _conflicting_files(preview)
            with suppress(Exception):
                run_git(["merge", "--abort"], preview, timeout=30)
            if conflicts:
                raise MergePreviewConflict(branch, conflicts)
            detail = (merged.stderr or merged.stdout).strip()
            raise MergePreviewError(f"could not merge {branch} into the merge preview: {detail}")
        _provision(root, preview, symlink_dirs, copy_files)
        yield preview
    finally:
        _discard_preview(root, preview)


__all__ = [
    "PREVIEW_DIRNAME",
    "MergePreviewConflict",
    "MergePreviewError",
    "merge_preview",
    "preview_worktree_path",
]
