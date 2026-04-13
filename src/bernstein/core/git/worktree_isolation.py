"""Worktree isolation validation after creation (AGENT-002).

Validates that a newly-created worktree is properly isolated:
1. .sdd/ directory is NOT shared (not a symlink into the parent repo).
2. Symlinks are read-only targets (point to directories, not individual state files).
3. No hardlinks leak mutable state from the parent repo.

If any check fails, the spawn should be aborted to prevent cross-agent
state corruption.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


class WorktreeIsolationError(Exception):
    """Raised when a worktree fails isolation validation.

    Attributes:
        violations: List of human-readable violation descriptions.
    """

    def __init__(self, violations: list[str]) -> None:
        self.violations = violations
        summary = "; ".join(violations)
        super().__init__(f"Worktree isolation violated: {summary}")


@dataclass(frozen=True)
class IsolationCheckResult:
    """Result of a worktree isolation check.

    Attributes:
        passed: True if the worktree passes all isolation checks.
        violations: List of human-readable violation descriptions.
    """

    passed: bool
    violations: list[str] = field(default_factory=list[str])


def check_sdd_not_shared(worktree_path: Path, repo_root: Path) -> list[str]:
    """Verify that .sdd/ in the worktree is not shared with the parent repo.

    The .sdd/ directory inside a worktree must be either absent (fine, will
    be created fresh) or a real directory local to the worktree.  It must NOT
    be a symlink into the parent repo's .sdd/ because that would let agents
    clobber each other's state files.

    Args:
        worktree_path: Path to the worktree directory.
        repo_root: Path to the parent repository root.

    Returns:
        List of violation descriptions (empty if clean).
    """
    violations: list[str] = []
    sdd_path = worktree_path / ".sdd"

    if not sdd_path.exists():
        return violations

    if sdd_path.is_symlink():
        link_target = sdd_path.resolve()
        parent_sdd = repo_root / ".sdd"
        if link_target == parent_sdd.resolve() or str(link_target).startswith(str(parent_sdd.resolve())):
            violations.append(f".sdd/ is a symlink to parent repo state: {sdd_path} -> {link_target}")
        else:
            violations.append(f".sdd/ is a symlink (should be a real directory): {sdd_path} -> {link_target}")

    return violations


def check_symlinks_read_only(
    worktree_path: Path,
    repo_root: Path,
    *,
    allowed_symlink_dirs: tuple[str, ...] = (),
) -> list[str]:
    """Verify that symlinks only point to allowed shared directories.

    Allowed symlinks (configured via ``WorktreeSetupConfig.symlink_dirs``)
    should point to shared read-heavy directories like ``node_modules`` or
    ``.venv``.  Any symlink outside the allowed set that points into the
    parent repo is suspicious.

    Args:
        worktree_path: Path to the worktree directory.
        repo_root: Path to the parent repository root.
        allowed_symlink_dirs: Directory names that are expected to be symlinked.

    Returns:
        List of violation descriptions (empty if clean).
    """
    violations: list[str] = []
    repo_root_resolved = repo_root.resolve()

    # Only check top-level entries; deep traversal would be too slow.
    if not worktree_path.exists():
        return violations

    for entry in worktree_path.iterdir():
        if not entry.is_symlink():
            continue

        rel_name = entry.name
        if rel_name in allowed_symlink_dirs:
            continue

        link_target = entry.resolve()
        # Symlinks pointing into the parent repo's mutable state dirs are dangerous
        mutable_dirs = (".sdd", ".git")
        for mutable in mutable_dirs:
            mutable_root = repo_root_resolved / mutable
            if str(link_target).startswith(str(mutable_root)):
                violations.append(
                    f"Symlink '{rel_name}' points into parent repo mutable state: {entry} -> {link_target}"
                )

    return violations


def _collect_multi_link_inodes(directory: Path) -> set[tuple[int, int]]:
    """Collect (device, inode) pairs for files with nlink > 1 in *directory*.

    Args:
        directory: Directory to scan recursively.

    Returns:
        Set of (device, inode) tuples for hardlinked files.
    """
    inodes: set[tuple[int, int]] = set()
    try:
        for f in directory.rglob("*"):
            if f.is_file() and not f.is_symlink():
                st = f.stat()
                if st.st_nlink > 1:
                    inodes.add((st.st_dev, st.st_ino))
    except OSError:
        pass
    return inodes


def _find_hardlink_violations(wt_dir: Path, parent_inodes: set[tuple[int, int]]) -> list[str]:
    """Check worktree files for inodes shared with the parent repo.

    Args:
        wt_dir: Worktree state directory to scan.
        parent_inodes: Known multi-linked inodes from the parent.

    Returns:
        List of violation descriptions.
    """
    violations: list[str] = []
    try:
        for wt_file in wt_dir.rglob("*"):
            if not wt_file.is_file() or wt_file.is_symlink():
                continue
            try:
                st = wt_file.stat()
            except OSError:
                continue
            if st.st_nlink > 1 and (st.st_dev, st.st_ino) in parent_inodes:
                violations.append(
                    f"Hardlink detected: {wt_file} shares inode with parent repo "
                    f"(dev={st.st_dev}, ino={st.st_ino}, nlink={st.st_nlink})"
                )
    except OSError:
        pass
    return violations


def check_no_hardlink_leaks(
    worktree_path: Path,
    repo_root: Path,
    *,
    check_dirs: tuple[str, ...] = (".sdd",),
) -> list[str]:
    """Verify that no hardlinks leak mutable state from the parent repo.

    Hardlinks share the same inode, so writes in one location are visible
    in the other.  This check scans state directories for files with
    nlink > 1 whose inode also appears in the parent repo's state dir.

    Only checks directories listed in ``check_dirs`` to avoid O(n) scanning
    the entire worktree.

    Args:
        worktree_path: Path to the worktree directory.
        repo_root: Path to the parent repository root.
        check_dirs: Directory names under the worktree to check for hardlinks.

    Returns:
        List of violation descriptions (empty if clean).
    """
    violations: list[str] = []

    for dir_name in check_dirs:
        wt_dir = worktree_path / dir_name
        parent_dir = repo_root / dir_name

        if not wt_dir.is_dir() or not parent_dir.is_dir():
            continue

        parent_inodes = _collect_multi_link_inodes(parent_dir)
        if parent_inodes:
            violations.extend(_find_hardlink_violations(wt_dir, parent_inodes))

    return violations


def validate_worktree_isolation(
    worktree_path: Path,
    repo_root: Path,
    *,
    allowed_symlink_dirs: tuple[str, ...] = (),
    check_hardlinks: bool = True,
) -> IsolationCheckResult:
    """Run all worktree isolation checks and return a combined result.

    This is the main entry point for post-creation validation.

    Args:
        worktree_path: Path to the worktree directory.
        repo_root: Path to the parent repository root.
        allowed_symlink_dirs: Directory names that are expected to be symlinked
            (passed through from WorktreeSetupConfig.symlink_dirs).
        check_hardlinks: Whether to run the hardlink check (can be slow on
            large state directories).

    Returns:
        IsolationCheckResult with pass/fail and any violations.
    """
    all_violations: list[str] = []

    all_violations.extend(check_sdd_not_shared(worktree_path, repo_root))
    all_violations.extend(
        check_symlinks_read_only(
            worktree_path,
            repo_root,
            allowed_symlink_dirs=allowed_symlink_dirs,
        )
    )
    if check_hardlinks:
        all_violations.extend(check_no_hardlink_leaks(worktree_path, repo_root))

    if all_violations:
        for v in all_violations:
            logger.warning("Worktree isolation violation: %s", v)

    return IsolationCheckResult(
        passed=len(all_violations) == 0,
        violations=all_violations,
    )
