import threading
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from bernstein.core.git_basic import GitResult
from bernstein.core.worktree import WorktreeError, WorktreeManager, WorktreeSetupConfig


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    return root


@pytest.fixture
def manager(repo_root: Path) -> WorktreeManager:
    return WorktreeManager(repo_root=repo_root)


def test_worktree_manager_create_success(manager: WorktreeManager, repo_root: Path) -> None:
    session_id = "test-session"
    worktree_path = repo_root / ".sdd/worktrees" / session_id
    branch_name = f"agent/{session_id}"

    with patch("bernstein.core.worktree.worktree_add") as mock_add:
        mock_add.return_value = GitResult(0, "", "")

        path = manager.create(session_id)

        assert path == worktree_path
        mock_add.assert_called_once_with(repo_root.resolve(), worktree_path, branch_name)


def test_worktree_manager_create_already_exists(manager: WorktreeManager, repo_root: Path) -> None:
    session_id = "test-session"
    worktree_path = repo_root / ".sdd/worktrees" / session_id
    worktree_path.mkdir(parents=True)

    with pytest.raises(WorktreeError, match="already exists"):
        manager.create(session_id)


def test_worktree_manager_create_git_fail(manager: WorktreeManager, repo_root: Path) -> None:
    session_id = "test-session"

    with patch("bernstein.core.worktree.worktree_add") as mock_add:
        mock_add.return_value = GitResult(1, "", "some git error")

        with pytest.raises(WorktreeError, match="git worktree add failed"):
            manager.create(session_id)


def test_worktree_manager_create_branch_exists(manager: WorktreeManager, repo_root: Path) -> None:
    session_id = "test-session"

    with patch("bernstein.core.worktree.worktree_add") as mock_add:
        mock_add.return_value = GitResult(1, "", "fatal: 'agent/test-session' already exists")

        with pytest.raises(WorktreeError, match="Branch 'agent/test-session' already exists"):
            manager.create(session_id)


def test_worktree_manager_cleanup(manager: WorktreeManager, repo_root: Path) -> None:
    session_id = "test-session"
    worktree_path = repo_root / ".sdd/worktrees" / session_id
    branch_name = f"agent/{session_id}"

    with (
        patch("bernstein.core.worktree.worktree_remove") as mock_remove,
        patch("bernstein.core.worktree.branch_delete") as mock_delete,
    ):
        mock_remove.return_value = GitResult(0, "", "")
        mock_delete.return_value = GitResult(0, "", "")

        manager.cleanup(session_id)

        mock_remove.assert_called_once_with(repo_root.resolve(), worktree_path)
        mock_delete.assert_called_once_with(repo_root.resolve(), branch_name)


def test_worktree_manager_shutdown_event(manager: WorktreeManager) -> None:
    shutdown_event = threading.Event()
    shutdown_event.set()
    manager.set_shutdown_event(shutdown_event)

    with pytest.raises(WorktreeError, match="Orchestrator shutting down"):
        manager.create("session-1")


def test_worktree_setup_config_application(repo_root: Path) -> None:
    config = WorktreeSetupConfig(symlink_dirs=("node_modules",), copy_files=(".env",), setup_command="echo 'hello'")
    manager = WorktreeManager(repo_root=repo_root, setup_config=config)

    session_id = "test-session"
    worktree_path = repo_root / ".sdd/worktrees" / session_id

    # Create some source files in repo root
    (repo_root / "node_modules").mkdir()
    (repo_root / ".env").write_text("FOO=BAR")

    with patch("bernstein.core.worktree.worktree_add") as mock_add, patch("subprocess.run") as mock_run:

        def mock_add_side_effect(root: Path, path: Path, branch: str) -> GitResult:
            path.mkdir(parents=True)
            return GitResult(0, "", "")

        mock_add.side_effect = mock_add_side_effect
        mock_run.return_value = MagicMock(returncode=0)

        path = manager.create(session_id)

        assert path == worktree_path
        # Check symlink
        target_node_modules = worktree_path / "node_modules"
        assert target_node_modules.is_symlink()
        assert target_node_modules.readlink() == (repo_root / "node_modules").resolve()

        # Check copy
        target_env = worktree_path / ".env"
        assert target_env.is_file()
        assert target_env.read_text() == "FOO=BAR"

        # Check setup command
        mock_run.assert_called_once()
        args, kwargs = mock_run.call_args
        assert args[0] == "echo 'hello'"
        assert kwargs["cwd"] == worktree_path


def test_worktree_manager_create_locked_index(manager: WorktreeManager) -> None:
    session_id = "test-session"

    with patch("bernstein.core.worktree.worktree_add") as mock_add:
        # Simulate locked index error from git
        mock_add.return_value = GitResult(1, "", "fatal: Unable to create '.git/index.lock': File exists.")

        with pytest.raises(WorktreeError, match="git worktree add failed"):
            manager.create(session_id)


def test_worktree_manager_list_active(manager: WorktreeManager, repo_root: Path) -> None:
    base_dir = repo_root / ".sdd/worktrees"
    with patch("bernstein.core.worktree.worktree_list") as mock_list:
        mock_list.return_value = (
            f"worktree {repo_root.resolve()}\n"
            f"worktree {base_dir / 'session-1'}\n"
            f"worktree {base_dir / 'session-2'}\n"
            f"worktree /other/path\n"
        )

        active = manager.list_active()
        assert sorted(active) == ["session-1", "session-2"]
