"""Files copied into an agent worktree must not be committable from it.

`copy_files` gives each worktree its own copy of untracked per-checkout inputs
(`.env` and friends). Nothing kept them out of the index, so an agent running
`git add -A` committed them to its branch, and the merge back into the parent
repository failed with an untracked-overwrite error naming a file the agent was
never asked to touch -- the same file, sitting untracked in the destination
work tree (#5966).

Real repositories and a real linked worktree here: the whole question is what
`git` does with these paths, and a mocked `git` cannot answer it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from bernstein.core.worktree import WorktreeSetupConfig, setup_worktree_env

from bernstein.core.git.local_exclude import resolve_info_exclude_path


def _git(cwd: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.name", "Bernstein Tests")
    _git(root, "config", "user.email", "tests@example.com")
    (root / "app.py").write_text("x = 1\n", encoding="utf-8")
    _git(root, "add", "app.py")
    _git(root, "commit", "-m", "base")
    # The per-checkout input: untracked in the parent, which is exactly why a
    # copy of it arriving through a merge cannot be written.
    (root / ".env").write_text("TOKEN=parent\n", encoding="utf-8")
    return root


def _worktree(repo_root: Path, name: str = "agent") -> Path:
    path = repo_root / ".sdd" / "worktrees" / name
    _git(repo_root, "worktree", "add", "-b", f"agent/{name}", str(path))
    return path


def test_copy_files_appended_to_git_exclude(repo: Path) -> None:
    """The copied name reaches the exclude file that applies to the worktree."""
    worktree = _worktree(repo)

    setup_worktree_env(repo, worktree, WorktreeSetupConfig(copy_files=(".env",)))

    assert (worktree / ".env").is_file(), "the copy itself must still happen"
    exclude_path = resolve_info_exclude_path(worktree)
    assert exclude_path is not None
    assert "/.env" in exclude_path.read_text(encoding="utf-8").splitlines()


def test_a_copied_file_is_not_staged_by_git_add_all(repo: Path) -> None:
    """The behaviour the exclude exists for, asserted through git itself."""
    worktree = _worktree(repo)

    setup_worktree_env(repo, worktree, WorktreeSetupConfig(copy_files=(".env",)))
    (worktree / "feature.py").write_text("y = 2\n", encoding="utf-8")
    _git(worktree, "add", "-A")

    staged = _git(worktree, "diff", "--cached", "--name-only").split()
    assert "feature.py" in staged, "the agent's real work must still be staged"
    assert ".env" not in staged


def test_entries_are_anchored_to_the_repository_root(repo: Path) -> None:
    """`/.env` and not `.env`, so a tracked `config/.env` is not hidden too."""
    worktree = _worktree(repo)
    (worktree / "config").mkdir()
    (worktree / "config" / ".env").write_text("TOKEN=tracked\n", encoding="utf-8")

    setup_worktree_env(repo, worktree, WorktreeSetupConfig(copy_files=(".env",)))
    _git(worktree, "add", "-A")

    staged = _git(worktree, "diff", "--cached", "--name-only").split()
    assert "config/.env" in staged
    assert ".env" not in staged


def test_a_file_already_present_in_the_worktree_is_excluded_too(repo: Path) -> None:
    """`_copy_files` skips an existing target; the exclude must not skip with it.

    That is the reported case: the file is in both trees already, which is what
    makes the merge fail.
    """
    worktree = _worktree(repo)
    (worktree / ".env").write_text("TOKEN=preexisting\n", encoding="utf-8")

    setup_worktree_env(repo, worktree, WorktreeSetupConfig(copy_files=(".env",)))
    _git(worktree, "add", "-A")

    assert (worktree / ".env").read_text(encoding="utf-8") == "TOKEN=preexisting\n"
    assert ".env" not in _git(worktree, "diff", "--cached", "--name-only").split()


def test_no_copy_files_writes_no_exclude_entries(repo: Path) -> None:
    """A config that copies nothing leaves the exclude file alone."""
    worktree = _worktree(repo)
    exclude_path = resolve_info_exclude_path(worktree)
    assert exclude_path is not None
    before = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""

    setup_worktree_env(repo, worktree, WorktreeSetupConfig(copy_files=()))

    after = exclude_path.read_text(encoding="utf-8") if exclude_path.exists() else ""
    assert after == before
