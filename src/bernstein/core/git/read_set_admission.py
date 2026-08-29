"""Read-set admission check module.

This module provides functionality to check if the read set of a run has changed
since a base commit on a target branch, which is used for admission control in
the Bernstein orchestrator.
"""

from dataclasses import dataclass
from pathlib import Path

from bernstein.core.git.git_basic import run_git
from bernstein.core.replay.read_paths import derive_read_paths

NULL_COMMIT_HASH = "0000000000000000000000000000000000000000"


@dataclass(frozen=True)
class ChangedPath:
    """Represents a file path that has changed between two commits.

    Attributes:
        path: The file path relative to the worktree root.
        old_commit: The commit hash of the file at the base commit (or null hash if
            the file did not exist at base commit).
        new_commit: The commit hash of the file at the target branch (or null hash
            if the file does not exist at target branch).
    """

    path: str
    old_commit: str
    new_commit: str


class ReadSetAdmissionRefused(Exception):
    """Exception raised when the read set has changed and admission is refused."""

    pass


def check_read_set_changed(
    journal_path: str,
    worktree_root: str,
    base_commit: str,
    target_branch: str,
) -> list[ChangedPath]:
    """Check if any paths in the run's read set have changed since base_commit.

    Args:
        journal_path: Path to the event journal file for the run.
        worktree_root: Absolute path to the worktree directory.
        base_commit: Commit hash to check changes from.
        target_branch: Target branch name or commit hash to check changes to.

    Returns:
        List of ChangedPath objects for each file in the read set that has
        changed between base_commit and target_branch. Empty list if no changes.

    Raises:
        ReadPathDerivationError: If the journal cannot be read or parsed.
    """
    # Derive the set of paths read during the run
    read_paths_set = derive_read_paths(Path(journal_path), Path(worktree_root))
    read_paths = read_paths_set.read_paths

    # Get the set of files that have changed between base_commit and target_branch
    try:
        diff_result = run_git(
            ["diff", "--name-only", base_commit, target_branch],
            cwd=worktree_root,
        )
        # Split output into lines and filter out empty lines
        changed_files = set(line.strip() for line in diff_result.stdout.splitlines() if line.strip())
    except Exception as exc:
        # If we cannot compute the diff (e.g., unreadable journal, bad tree state),
        # this gate exists to refuse a merge whose read-set might have drifted.
        # A check that could not run does not know that nothing drifted, and
        # proceeding would turn every such failure into an admission. Instead,
        # refuse the action and name the reason: "read-set admission check could not run"
        raise ReadSetAdmissionRefused(f"Read-set admission check could not run: {exc}") from exc

    changed_paths: list[ChangedPath] = []
    for path in read_paths:
        if path in changed_files:
            # Get the commit hash of the file at base_commit
            try:
                old_commit_output = run_git(
                    ["log", "-1", "--format=%H", base_commit, "--", path],
                    cwd=worktree_root,
                )
                old_commit = old_commit_output.stdout.strip()
                if not old_commit:
                    old_commit = NULL_COMMIT_HASH
            except Exception:
                old_commit = NULL_COMMIT_HASH

            # Get the commit hash of the file at target_branch
            try:
                new_commit_output = run_git(
                    ["log", "-1", "--format=%H", target_branch, "--", path],
                    cwd=worktree_root,
                )
                new_commit = new_commit_output.stdout.strip()
                if not new_commit:
                    new_commit = NULL_COMMIT_HASH
            except Exception:
                new_commit = NULL_COMMIT_HASH

            changed_paths.append(
                ChangedPath(
                    path=path,
                    old_commit=old_commit,
                    new_commit=new_commit,
                )
            )

    return changed_paths
