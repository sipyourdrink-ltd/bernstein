"""Tests for worktree symlink support — happy path and failure handling.

Covers T482: symlink heavy directories (node_modules, .venv, build output)
from the main repository into agent worktrees to save disk and setup time.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from unittest.mock import patch

import pytest

from bernstein.core.git_basic import GitResult
from bernstein.core.worktree import WorktreeManager, WorktreeSetupConfig, setup_worktree_env

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    """Create a bare repo root directory (no git)."""
    root = tmp_path / "repo"
    root.mkdir()
    return root


@pytest.fixture
def worktree_path(tmp_path: Path) -> Path:
    """Create an empty worktree directory."""
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    return worktree


# ---------------------------------------------------------------------------
# TestWorktreeSymlinksCreate
# ---------------------------------------------------------------------------


class TestWorktreeSymlinksHappyPath:
    def test_single_symlink_directory(self, repo_root: Path, worktree_path: Path) -> None:
        """Happy path: one directory symlinked into the worktree."""
        (repo_root / "node_modules").mkdir()
        config = WorktreeSetupConfig(symlink_dirs=("node_modules",))

        setup_worktree_env(repo_root, worktree_path, config)

        target = worktree_path / "node_modules"
        assert target.is_symlink()
        assert target.readlink() == (repo_root / "node_modules").resolve()

    def test_multiple_symlink_directories(self, repo_root: Path, worktree_path: Path) -> None:
        """Happy path: several directories symlinked at once."""
        for name in ("node_modules", ".venv", "build"):
            (repo_root / name).mkdir()
        config = WorktreeSetupConfig(symlink_dirs=("node_modules", ".venv", "build"))

        setup_worktree_env(repo_root, worktree_path, config)

        for name in ("node_modules", ".venv", "build"):
            target = worktree_path / name
            assert target.is_symlink(), f"{name} should be a symlink"
            assert target.readlink() == (repo_root / name).resolve()

    def test_empty_symlink_dirs_does_nothing(self, repo_root: Path, worktree_path: Path) -> None:
        """When symlink_dirs is empty, no symlinks are created."""
        (repo_root / "node_modules").mkdir()
        config = WorktreeSetupConfig(symlink_dirs=())

        setup_worktree_env(repo_root, worktree_path, config)

        assert not (worktree_path / "node_modules").exists()

    def test_nested_subdirectory_symlink(self, repo_root: Path, worktree_path: Path) -> None:
        """Supports nested paths like \"dist/frontend/assets\" as a single entry."""
        nested = repo_root / "dist/frontend/assets"
        nested.mkdir(parents=True)
        config = WorktreeSetupConfig(symlink_dirs=("dist/frontend/assets",))

        setup_worktree_env(repo_root, worktree_path, config)

        target = worktree_path / "dist/frontend/assets"
        assert target.is_symlink()
        assert target.readlink() == (repo_root / "dist/frontend/assets").resolve()

    def test_source_is_file_symlinked_anyway(self, repo_root: Path, worktree_path: Path) -> None:
        """When source exists but is a file (not dir), symlink still created."""
        src_file = repo_root / "build"
        src_file.write_text("build artifact")
        config = WorktreeSetupConfig(symlink_dirs=("build",))

        setup_worktree_env(repo_root, worktree_path, config)

        target = worktree_path / "build"
        assert target.is_symlink()

    def test_symlink_target_already_exists_skipped(
        self,
        repo_root: Path,
        worktree_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """If target already exists as a real directory, skip and log."""
        (repo_root / "node_modules").mkdir()
        existing = worktree_path / "node_modules"
        existing.mkdir()  # Real directory already present
        config = WorktreeSetupConfig(symlink_dirs=("node_modules",))

        with caplog.at_level("DEBUG", logger="bernstein.core.worktree"):
            setup_worktree_env(repo_root, worktree_path, config)

        assert existing.exists()
        assert not existing.is_symlink()
        assert any("Skipping symlink" in r.message for r in caplog.records)

    def test_symlink_target_already_symlink_skipped(self, repo_root: Path, worktree_path: Path) -> None:
        """If target is already a symlink pointing to source, skip."""
        source = repo_root / "node_modules"
        source.mkdir()
        target = worktree_path / "node_modules"
        target.symlink_to(source)
        config = WorktreeSetupConfig(symlink_dirs=("node_modules",))

        setup_worktree_env(repo_root, worktree_path, config)

        assert target.is_symlink()
        assert target.readlink() == source.resolve()

    def test_symlink_points_to_repo_not_worktree(self, repo_root: Path, worktree_path: Path) -> None:
        """Verify symlink target resolves under repo_root, not inside worktree."""
        (repo_root / "node_modules").mkdir()
        config = WorktreeSetupConfig(symlink_dirs=("node_modules",))

        setup_worktree_env(repo_root, worktree_path, config)

        target = worktree_path / "node_modules"
        resolved_target = target.readlink()
        resolved_repo_root = repo_root.resolve()
        assert resolved_target.is_relative_to(resolved_repo_root)
        assert not resolved_target.is_relative_to(worktree_path.resolve())


class TestWorktreeSymlinksFailures:
    def test_source_missing_logs_debug_and_continues(
        self,
        repo_root: Path,
        worktree_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Missing source directory produces debug log, no crash."""
        config = WorktreeSetupConfig(symlink_dirs=("node_modules",))

        with caplog.at_level("DEBUG", logger="bernstein.core.worktree"):
            setup_worktree_env(repo_root, worktree_path, config)

        assert not (worktree_path / "node_modules").exists()
        assert any("Skipping symlink" in r.message for r in caplog.records)

    def test_cross_filesystem_symlink_warning(
        self,
        repo_root: Path,
        worktree_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Cross-filesystem symlinks may fail with EXDEV; caught and logged."""
        source = repo_root / "build"
        source.mkdir()
        config = WorktreeSetupConfig(symlink_dirs=("build",))

        def _fake_symlink(self_path: Path, target: Path) -> None:  # type: ignore[override]
            import errno

            raise OSError(errno.EXDEV, "Invalid cross-device link")

        with patch.object(Path, "symlink_to", _fake_symlink):
            setup_worktree_env(repo_root, worktree_path, config)

        assert any("Failed to symlink" in r.message for r in caplog.records)
        # Other directories in the list are unaffected; the worktree is still usable
        assert (worktree_path).exists()

    def test_other_symlinks_remain_unaffected_when_one_fails(
        self, repo_root: Path, worktree_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """If one symlink fails, others in the list continue."""
        for name in ("node_modules", ".venv", "build"):
            (repo_root / name).mkdir()
        config = WorktreeSetupConfig(symlink_dirs=("node_modules", ".venv", "build"))

        # Patch Path.symlink_to so that only ".venv" fails
        original_symlink_to = Path.symlink_to

        def _conditional_fail(self_path: Path, target: Path) -> None:  # type: ignore[override]
            if str(self_path).endswith(".venv"):
                import errno

                raise OSError(errno.EXDEV, "cross-device")
            return original_symlink_to(self_path, target)

        with patch.object(Path, "symlink_to", _conditional_fail):
            setup_worktree_env(repo_root, worktree_path, config)

        assert (worktree_path / "node_modules").is_symlink()
        assert not (worktree_path / ".venv").is_symlink()
        assert (worktree_path / "build").is_symlink()
        assert any("Failed to symlink" in r.message for r in caplog.records)

    def test_symlink_to_unresolvable_path_continues(
        self, repo_root: Path, worktree_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """OSError during symlink creation is caught, logged, and does not abort."""
        (repo_root / "node_modules").mkdir()
        config = WorktreeSetupConfig(symlink_dirs=("node_modules",))

        def _fail(_self: Path, _target: Path) -> None:  # type: ignore[override]
            raise PermissionError("a required privilege is not held")

        with patch.object(Path, "symlink_to", _fail):
            setup_worktree_env(repo_root, worktree_path, config)

        assert any("Failed to symlink" in r.message for r in caplog.records)
        assert not (worktree_path / "node_modules").is_symlink()
        # Worktree itself should still exist
        assert worktree_path.exists()


# ---------------------------------------------------------------------------
# Integration-level: WorktreeManager.create → setup_worktree_env
# ---------------------------------------------------------------------------


class TestWorktreeSymlinksIntegration:
    def test_create_with_setup_config_applies_symlinks(self, repo_root: Path) -> None:
        """WorktreeManager.create() calls setup_worktree_env when config provided."""
        (repo_root / "node_modules").mkdir()
        config = WorktreeSetupConfig(symlink_dirs=("node_modules",))
        manager = WorktreeManager(repo_root=repo_root, setup_config=config)

        session_id = "session-abc"
        worktree_path = repo_root / ".sdd/worktrees" / session_id

        with patch("bernstein.core.git.worktree.worktree_add") as mock_add:

            def _mkdir(_root: Path, path: Path, _branch: str) -> GitResult:
                path.mkdir(parents=True)
                return GitResult(0, "", "")

            mock_add.side_effect = _mkdir

            result = manager.create(session_id)

        assert result == worktree_path
        linked = worktree_path / "node_modules"
        assert linked.is_symlink()
        assert linked.readlink() == (repo_root / "node_modules").resolve()

    def test_create_without_setup_config_skips_symlinks(self, repo_root: Path) -> None:
        """No setup_config means no symlinks applied during create."""
        manager = WorktreeManager(repo_root=repo_root, setup_config=None)

        (repo_root / "node_modules").mkdir()

        with patch("bernstein.core.git.worktree.worktree_add") as mock_add:

            def _mkdir(_root: Path, path: Path, _branch: str) -> GitResult:
                path.mkdir(parents=True)
                return GitResult(0, "", "")

            mock_add.side_effect = _mkdir

            manager.create("session-xyz")

        worktree_path = repo_root / ".sdd/worktrees/session-xyz"
        assert not (worktree_path / "node_modules").exists()

    def test_symlink_persists_after_recreate(self, repo_root: Path) -> None:
        """After cleanup, a fresh create re-applies symlinks correctly."""
        (repo_root / "node_modules").mkdir()
        config = WorktreeSetupConfig(symlink_dirs=("node_modules",))
        manager = WorktreeManager(repo_root=repo_root, setup_config=config)

        session_id = "session-lifecycle"
        with (
            patch("bernstein.core.git.worktree.worktree_add") as mock_add,
            patch("bernstein.core.git.worktree.worktree_remove") as mock_rm,
            patch("bernstein.core.git.worktree.branch_delete") as mock_bd,
        ):
            mock_add.return_value = GitResult(0, "", "")
            mock_rm.return_value = GitResult(0, "", "")
            mock_bd.return_value = GitResult(0, "", "")

            path1 = manager.create(session_id)
            assert (path1 / "node_modules").is_symlink()

            manager.cleanup(session_id)
            # Remove the actual directory so create can succeed again
            worktree_path = repo_root / ".sdd/worktrees" / session_id
            if worktree_path.exists():
                shutil.rmtree(worktree_path, ignore_errors=True)

            # Recreate — should symlink again without error
            path2 = manager.create(session_id)
            assert (path2 / "node_modules").is_symlink()

    def test_symlinks_resolve_from_absolute_repo_root(self, tmp_path: Path) -> None:
        """Symlink targets resolve from repo_root even if passed via tmp_path."""
        repo_root = tmp_path / "real_repo"
        repo_root.mkdir()
        (repo_root / "build").mkdir()
        config = WorktreeSetupConfig(symlink_dirs=("build",))
        manager = WorktreeManager(repo_root=repo_root, setup_config=config)

        with patch("bernstein.core.git.worktree.worktree_add") as mock_add:

            def _mkdir(_root: Path, path: Path, _branch: str) -> GitResult:
                path.mkdir(parents=True)
                return GitResult(0, "", "")

            mock_add.side_effect = _mkdir

            manager.create("abs-test")

        wt = repo_root / ".sdd/worktrees/abs-test"
        assert (wt / "build").readlink().is_relative_to(repo_root.resolve())


# ---------------------------------------------------------------------------
# Windows caveats
# ---------------------------------------------------------------------------


class TestWorktreeSymlinksWindowsCaveats:
    def test_windows_symlink_fails_gracefully_without_dev_mode(
        self, repo_root: Path, worktree_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """On Windows, symlink_to raises OSError when dev mode or admin is missing."""
        (repo_root / "node_modules").mkdir()
        config = WorktreeSetupConfig(symlink_dirs=("node_modules",))

        def _win_fail(_self: Path, _target: Path) -> None:  # type: ignore[override]
            raise OSError("A required privilege is not held by the client")

        with patch.object(Path, "symlink_to", _win_fail):
            setup_worktree_env(repo_root, worktree_path, config)

        # Should log warning, not crash
        assert any("Failed to symlink" in r.message for r in caplog.records)
        assert not (worktree_path / "node_modules").is_symlink()

    def test_multiple_symlinks_all_fall_back_windows(self, repo_root: Path, worktree_path: Path) -> None:
        """If all symlinks fail (total Windows lockdown), worktree still exists."""
        for name in ("node_modules", ".venv"):
            (repo_root / name).mkdir()
        config = WorktreeSetupConfig(symlink_dirs=("node_modules", ".venv"))

        def _win_fail(_self: Path, _target: Path) -> None:  # type: ignore[override]
            raise PermissionError("Cannot create symlink")

        with patch.object(Path, "symlink_to", _win_fail):
            # No exception should escape
            setup_worktree_env(repo_root, worktree_path, config)

        assert worktree_path.exists()
        assert not (worktree_path / "node_modules").is_symlink()
        assert not (worktree_path / ".venv").is_symlink()

    def test_macos_symlinks_work_natively(self, repo_root: Path, worktree_path: Path) -> None:
        """macOS supports symlinks out of the box; no special handling needed."""
        (repo_root / "node_modules").mkdir()
        config = WorktreeSetupConfig(symlink_dirs=("node_modules",))

        setup_worktree_env(repo_root, worktree_path, config)

        assert (worktree_path / "node_modules").is_symlink()
