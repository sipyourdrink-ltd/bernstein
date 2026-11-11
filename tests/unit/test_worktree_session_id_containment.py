"""``WorktreeManager.create`` keeps every worktree inside ``.sdd/worktrees``.

The session id becomes both a directory name under the worktree base and part
of the agent branch name, so it is validated as a slug before anything touches
the filesystem, and the final path is resolved and checked to sit under the
base directory.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
from bernstein.core.git_basic import GitResult
from bernstein.core.worktree import WorktreeError, WorktreeManager


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    return root


@pytest.mark.parametrize(
    "session_id",
    [
        "../escape",
        "../../outside-repo",
        "nested/child",
        "/absolute/path",
        "..",
        "",
        "x" * 65,
        "HEAD",
        "-leading-dash",
        "trailing.",
        "has space",
    ],
)
def test_create_rejects_unsafe_session_id(repo_root: Path, session_id: str) -> None:
    manager = WorktreeManager(repo_root=repo_root)

    with patch("bernstein.core.git.worktree.worktree_add") as mock_add:
        mock_add.return_value = GitResult(0, "", "")
        with pytest.raises(WorktreeError):
            manager.create(session_id)

    mock_add.assert_not_called()
    assert not (repo_root.parent / "escape").exists()
    assert not (repo_root.parent / "outside-repo").exists()


def test_create_rejects_a_path_that_resolves_outside_the_base(repo_root: Path, tmp_path: Path) -> None:
    """A dangling symlink named like a valid session id must not redirect the worktree."""
    manager = WorktreeManager(repo_root=repo_root)
    base = repo_root / ".sdd" / "worktrees"
    base.mkdir(parents=True)
    (base / "backend-1234abcd").symlink_to(tmp_path / "elsewhere")

    with patch("bernstein.core.git.worktree.worktree_add") as mock_add:
        mock_add.return_value = GitResult(0, "", "")
        with pytest.raises(WorktreeError, match="outside"):
            manager.create("backend-1234abcd")

    mock_add.assert_not_called()
    assert not (tmp_path / "elsewhere").exists()


def test_create_accepts_spawner_style_session_id(repo_root: Path) -> None:
    manager = WorktreeManager(repo_root=repo_root)

    with patch("bernstein.core.git.worktree.worktree_add") as mock_add:
        mock_add.return_value = GitResult(0, "", "")
        path = manager.create("backend-resume-1a2b3c4d")

    assert path.name == "backend-resume-1a2b3c4d"
    assert path.resolve().parent == (repo_root / ".sdd" / "worktrees").resolve()
    mock_add.assert_called_once()
