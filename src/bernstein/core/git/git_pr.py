"""Pull request and branching operations."""

from __future__ import annotations

import logging
import os
import py_compile
import shutil
import stat
import subprocess
import sys
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

from bernstein.core.git.git_basic import GitResult, run_git
from bernstein.core.telemetry import start_span

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class MergeResult:
    """Outcome of a merge attempt with conflict detection.

    Attributes:
        success: True if the merge completed without conflicts.
        conflicting_files: File paths with merge conflicts (empty on success).
        merge_diff: The diff of merged changes (empty on conflict).
        error: Error message if the merge failed for non-conflict reasons.
    """

    success: bool
    conflicting_files: list[str]
    merge_diff: str = ""
    error: str = ""


@dataclass(frozen=True)
class PullRequestResult:
    """Outcome of a GitHub PR creation attempt.

    Attributes:
        success: True if the PR was created.
        pr_url: URL of the created PR (empty on failure).
        error: Error message on failure.
    """

    success: bool
    pr_url: str = ""
    error: str = ""


# ------------------------------------------------------------------
# Pre-merge syntax validation
# ------------------------------------------------------------------


def _check_python_syntax(cwd: Path) -> list[str]:
    """Verify that all staged .py files have valid Python syntax.

    Uses ``py_compile.compile`` with ``doraise=True`` to catch syntax
    errors before a merge commit is created.  Returns a list of
    human-readable error strings (empty on success).

    Args:
        cwd: Repository root where the merge is staged.

    Returns:
        List of error descriptions, one per file with a syntax error.
    """
    from pathlib import Path as _Path

    # Get the list of files modified in the staged merge
    names_result = run_git(["diff", "--cached", "--name-only", "--diff-filter=ACMR"], cwd, timeout=15)
    errors: list[str] = []
    for raw_name in names_result.stdout.strip().splitlines():
        name = raw_name.strip()
        if not name.endswith(".py"):
            continue
        filepath = _Path(cwd) / name
        if not filepath.is_file():
            continue
        try:
            py_compile.compile(str(filepath), doraise=True)
        except py_compile.PyCompileError as exc:
            errors.append(f"{name}: {exc.msg}")
    return errors


# ------------------------------------------------------------------
# Branching
# ------------------------------------------------------------------


def merge_branch(
    cwd: Path,
    branch: str,
    *,
    message: str | None = None,
    no_ff: bool = True,
) -> GitResult:
    """Merge a branch into the current HEAD.

    Args:
        cwd: Repository root.
        branch: Branch to merge.
        message: Merge commit message.
        no_ff: If True, use ``--no-ff``.

    Returns:
        GitResult from the merge command.
    """
    with start_span("task.merge", {"branch": branch, "no_ff": no_ff}):
        cmd = ["merge"]
        if no_ff:
            cmd.append("--no-ff")
        cmd.append(branch)
        if message:
            cmd.extend(["-m", message])
        return run_git(cmd, cwd, timeout=60)


def merge_with_conflict_detection(
    cwd: Path,
    branch: str,
    *,
    message: str | None = None,
) -> MergeResult:
    """Merge a branch with explicit conflict detection and safe abort on failure.

    Performs ``git merge --no-commit --no-ff`` to stage the merge without
    committing.  If conflicts are detected, aborts the merge cleanly and
    returns the list of conflicting files so a resolver agent can act on them.

    Args:
        cwd: Repository root.
        branch: Branch to merge into the current HEAD.
        message: Commit message when the merge is clean.

    Returns:
        MergeResult indicating success or listing conflicting files.
    """
    with start_span("task.merge_with_conflict_detection", {"branch": branch}):
        # 1. Attempt the merge without committing
        merge_r = run_git(
            ["merge", "--no-commit", "--no-ff", branch],
            cwd,
            timeout=120,
        )

    if merge_r.ok:
        # Pre-commit syntax check: verify all modified .py files compile.
        syntax_errors = _check_python_syntax(cwd)
        if syntax_errors:
            run_git(["merge", "--abort"], cwd, timeout=10)
            error_summary = "; ".join(syntax_errors)
            logger.warning("Syntax check failed before merge commit: %s", error_summary)
            return MergeResult(
                success=False,
                conflicting_files=[],
                error=f"Python syntax errors blocked merge: {error_summary}",
            )

        # Clean merge — commit it
        msg = message or f"Merge {branch}"
        commit_r = run_git(["commit", "-m", msg], cwd, timeout=30)
        if commit_r.ok:
            diff = run_git(["diff", "HEAD~1", "--stat"], cwd, timeout=30).stdout
            return MergeResult(success=True, conflicting_files=[], merge_diff=diff)
        # Nothing to commit (branches already identical)
        run_git(["merge", "--abort"], cwd, timeout=10)
        return MergeResult(success=True, conflicting_files=[])

    # 2. Check if the failure is due to merge conflicts
    conflicts = _parse_conflict_files(cwd)
    if conflicts:
        # Abort the conflicted merge to restore clean state
        run_git(["merge", "--abort"], cwd, timeout=10)
        return MergeResult(success=False, conflicting_files=conflicts)

    # 3. Non-conflict failure (missing branch, unrelated histories, etc.)
    run_git(["merge", "--abort"], cwd, timeout=10)
    return MergeResult(
        success=False,
        conflicting_files=[],
        error=merge_r.stderr.strip() or "merge failed for unknown reason",
    )


def _parse_conflict_files(cwd: Path) -> list[str]:
    """Extract list of files with merge conflicts from git status.

    Looks for unmerged entries (UU, AA, DD, AU, UA, DU, UD) in porcelain
    output.

    Args:
        cwd: Repository root.

    Returns:
        List of conflicting file paths.
    """
    status = run_git(["status", "--porcelain"], cwd, timeout=10)
    conflicts: list[str] = []
    for line in status.stdout.splitlines():
        if len(line) < 4:
            continue
        xy = line[:2]
        # Unmerged status codes per git-status(1)
        if xy in ("UU", "AA", "DD", "AU", "UA", "DU", "UD"):
            conflicts.append(line[3:].strip())
    return conflicts


def branch_delete(cwd: Path, branch: str) -> GitResult:
    """Force-delete a local branch."""
    return run_git(["branch", "-D", branch], cwd, timeout=10)


def create_task_branch(cwd: Path, branch_name: str) -> GitResult:
    """Create and checkout a new branch from the current HEAD.

    Args:
        cwd: Repository root.
        branch_name: Name of the new branch (e.g. ``bernstein/task-abc123``).

    Returns:
        GitResult from ``git checkout -b <branch_name>``.
    """
    return run_git(["checkout", "-b", branch_name], cwd, timeout=10)


def create_branch(cwd: Path, branch_name: str, base: str = "main") -> GitResult:
    """Create a new branch from a given base without switching to it.

    Useful for creating task/, evolve/, or agent/ branches from main
    without disrupting the current checkout.

    Args:
        cwd: Repository root.
        branch_name: Name of the new branch.
        base: Base ref to branch from (default ``"main"``).

    Returns:
        GitResult from ``git branch <branch_name> <base>``.
    """
    return run_git(["branch", branch_name, base], cwd, timeout=10)


def delete_old_branches(
    cwd: Path,
    *,
    older_than_hours: int = 24,
    prefix: str = "bernstein/",
    remote: str | None = None,
) -> list[str]:
    """Delete local branches matching *prefix* whose last commit is older than the threshold.

    Args:
        cwd: Repository root.
        older_than_hours: Delete branches with HEAD commit older than this.
        prefix: Only consider branches starting with this string.
        remote: If set, also delete the branch on this remote.

    Returns:
        List of deleted branch names.
    """
    # List local branches matching the prefix
    r = run_git(
        ["branch", "--list", f"{prefix}*", "--format=%(refname:short) %(committerdate:unix)"],
        cwd,
        timeout=10,
    )
    if not r.ok or not r.stdout.strip():
        return []

    cutoff = time.time() - (older_than_hours * 3600)
    deleted: list[str] = []

    for line in r.stdout.strip().splitlines():
        parts = line.rsplit(" ", 1)
        if len(parts) != 2:
            continue
        branch, epoch_str = parts
        try:
            epoch = float(epoch_str)
        except ValueError:
            continue

        if epoch >= cutoff:
            continue

        # Delete locally
        del_r = run_git(["branch", "-D", branch.strip()], cwd, timeout=10)
        if del_r.ok:
            deleted.append(branch.strip())
            logger.info("Deleted old branch: %s (age > %dh)", branch.strip(), older_than_hours)
            # Optionally delete on remote
            if remote:
                run_git(["push", remote, "--delete", branch.strip()], cwd, timeout=30)

    return deleted


def push_branch(cwd: Path, branch: str, remote: str = "origin") -> GitResult:
    """Push a branch to remote, setting the upstream tracking ref.

    Args:
        cwd: Repository root.
        branch: Branch name to push.
        remote: Remote name (default ``"origin"``).

    Returns:
        GitResult from ``git push --set-upstream <remote> <branch>``.
    """
    return run_git(["push", "--set-upstream", remote, branch], cwd, timeout=60)


def push_head_as(cwd: Path, branch: str, remote: str = "origin") -> GitResult:
    """Push the current HEAD to a named remote branch via refspec.

    Use when the local branch name differs from the desired remote branch name.
    For example, push an ``agent/{session_id}`` worktree as
    ``bernstein/task-{id}`` on the remote without checking out a new branch.

    Args:
        cwd: Repository root (usually a worktree).
        branch: Desired remote branch name (e.g. ``"bernstein/task-abc123"``).
        remote: Remote name (default ``"origin"``).

    Returns:
        GitResult from ``git push --set-upstream <remote> HEAD:refs/heads/<branch>``.
    """
    return run_git(
        ["push", "--set-upstream", remote, f"HEAD:refs/heads/{branch}"],
        cwd,
        timeout=60,
    )


# ------------------------------------------------------------------
# Pull Requests (GitHub-specific)
# ------------------------------------------------------------------


def create_github_pr(
    cwd: Path,
    *,
    title: str,
    body: str,
    head: str,
    base: str = "main",
    labels: list[str] | None = None,
) -> PullRequestResult:
    """Create a GitHub pull request via the ``gh`` CLI.

    Labels are added as a best-effort post-creation step. If labels don't
    exist on the repo, the PR is still created successfully and a warning
    is logged.

    Args:
        cwd: Repository root (used as working directory for ``gh``).
        title: PR title.
        body: PR body / description.
        head: Source branch name.
        base: Target branch (default ``"main"``).
        labels: Optional list of label names to attach (best-effort).

    Returns:
        PullRequestResult with ``pr_url`` set on success.
    """
    # Create PR without labels first to avoid failure if labels don't exist
    cmd = [
        "gh",
        "pr",
        "create",
        "--title",
        title,
        "--body",
        body,
        "--head",
        head,
        "--base",
        base,
    ]
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        if result.returncode != 0:
            return PullRequestResult(success=False, error=result.stderr.strip())

        pr_url = result.stdout.strip()

        # Add labels separately (best-effort) - don't fail if labels don't exist
        if labels and pr_url:
            try:
                label_result = subprocess.run(
                    ["gh", "pr", "edit", pr_url, "--add-label", ",".join(labels)],
                    cwd=cwd,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=15,
                )
                if label_result.returncode != 0:
                    logger.warning(
                        "PR created but failed to add labels %s: %s",
                        labels,
                        label_result.stderr.strip(),
                    )
            except (subprocess.TimeoutExpired, OSError) as exc:
                logger.warning("PR created but failed to add labels %s: %s", labels, exc)

        return PullRequestResult(success=True, pr_url=pr_url)
    except (subprocess.TimeoutExpired, OSError) as exc:
        return PullRequestResult(success=False, error=str(exc))


def enable_pr_auto_merge(cwd: Path, pr_url_or_number: str) -> GitResult:
    """Enable auto-merge (squash) on a PR via ``gh pr merge --auto``.

    Args:
        cwd: Repository root.
        pr_url_or_number: PR URL or number string.

    Returns:
        GitResult with the exit code from ``gh pr merge --auto --squash``.
    """
    try:
        result = subprocess.run(
            ["gh", "pr", "merge", "--auto", "--squash", pr_url_or_number],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
        return GitResult(
            returncode=result.returncode,
            stdout=result.stdout,
            stderr=result.stderr,
        )
    except (subprocess.TimeoutExpired, OSError) as exc:
        return GitResult(returncode=1, stdout="", stderr=str(exc))


# ------------------------------------------------------------------
# Worktree
# ------------------------------------------------------------------


def worktree_add(cwd: Path, path: Path, branch: str) -> GitResult:
    """Create a git worktree at *path* on a new branch.

    Args:
        cwd: Repository root.
        path: Filesystem path for the worktree.
        branch: New branch name.
    """
    return run_git(
        ["worktree", "add", str(path), "-b", branch],
        cwd,
        timeout=30,
    )


def worktree_remove(cwd: Path, path: Path) -> GitResult:
    """Remove a worktree (force), with Windows retry logic.

    On Windows, file locks from recently-terminated processes or antivirus
    can prevent immediate deletion. This function retries up to 3 times with
    delays, then falls back to manual directory deletion if git fails.
    """
    max_attempts = 3 if sys.platform == "win32" else 1

    for attempt in range(max_attempts):
        result = run_git(
            ["worktree", "remove", "--force", str(path)],
            cwd,
            timeout=30,
        )
        if result.ok:
            return result

        # On Windows, retry after a short delay (file locks may release)
        if sys.platform == "win32" and attempt < max_attempts - 1:
            time.sleep(1.0)
            continue

        # Final fallback on Windows: manual deletion with permission override
        if sys.platform == "win32" and path.exists():
            # Extra delay for stubborn file locks (processes fully exiting)
            time.sleep(2.0)

            def _onerror(func, fpath, _exc_info):  # type: ignore[no-untyped-def]
                """Clear read-only flag and retry; ignore if still locked."""
                try:
                    os.chmod(fpath, stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
                    func(fpath)
                except OSError:
                    pass  # File still locked, skip it

            try:
                shutil.rmtree(path, onerror=_onerror)
                # Prune to clean up git's worktree list after manual deletion
                run_git(["worktree", "prune"], cwd, timeout=10)
                logger.debug("Worktree %s removed via manual deletion fallback", path)
                return GitResult(returncode=0, stdout="", stderr="")
            except Exception as exc:
                logger.debug("Manual worktree deletion failed for %s: %s", path, exc)
                # Check if directory is mostly gone (some locked files may remain)
                if not path.exists() or not any(path.iterdir()):
                    run_git(["worktree", "prune"], cwd, timeout=10)
                    return GitResult(returncode=0, stdout="", stderr="")

        return result

    return result


def worktree_list(cwd: Path) -> str:
    """Return raw ``git worktree list --porcelain`` output."""
    return run_git(["worktree", "list", "--porcelain"], cwd, timeout=10).stdout


def apply_diff(cwd: Path, diff: str) -> GitResult:
    """Apply a unified diff via ``git apply``.

    Args:
        cwd: Working directory (usually a worktree).
        diff: Unified diff content.
    """
    return run_git(
        ["apply", "--allow-empty", "-"],
        cwd,
        input_data=diff,
        timeout=30,
    )


# ------------------------------------------------------------------
# Bisect
# ------------------------------------------------------------------


def bisect_regression(
    cwd: Path,
    test_cmd: str,
    good_ref: str = "HEAD~10",
) -> str | None:
    """Find which commit introduced a test regression via ``git bisect``.

    Args:
        cwd: Repository root.
        test_cmd: Shell command to run as the bisect test.
        good_ref: Known-good reference (default HEAD~10).

    Returns:
        The first bad commit hash, or None if bisect failed.
    """
    import re

    try:
        run_git(["bisect", "start", "HEAD", good_ref], cwd, timeout=10)

        result = subprocess.run(
            ["git", "bisect", "run", *test_cmd.split()],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
        )

        # Parse the first bad commit from bisect output
        bad_commit: str | None = None
        for line in result.stdout.splitlines():
            m = re.search(r"([0-9a-f]{7,40}) is the first bad commit", line)
            if m:
                bad_commit = m.group(1)
                break

        return bad_commit

    except (subprocess.TimeoutExpired, OSError) as exc:
        logger.warning("bisect_regression failed: %s", exc)
        return None
    finally:
        run_git(["bisect", "reset"], cwd, timeout=10)
