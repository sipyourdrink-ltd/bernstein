"""Tests for SEC-007: git hook installation for file permission enforcement."""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
from bernstein.core.git_hooks import _HOOK_MARKER, GitHookInstaller

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_repo(tmp_path: Path) -> Path:
    """Create a minimal fake git repo structure."""
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "hooks").mkdir()
    return tmp_path


@pytest.fixture
def fake_worktree(tmp_path: Path) -> Path:
    """Create a fake worktree with .git file pointing to a gitdir."""
    gitdir = tmp_path / "actual_gitdir"
    gitdir.mkdir()
    (gitdir / "hooks").mkdir()

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {gitdir}", encoding="utf-8")
    return worktree


# ---------------------------------------------------------------------------
# Installation
# ---------------------------------------------------------------------------


class TestGitHookInstall:
    """Test pre-commit hook installation."""

    def test_install_in_regular_repo(self, fake_repo: Path) -> None:
        installer = GitHookInstaller(denied_paths=(".sdd/*", ".github/*"))
        hook_path = installer.install(fake_repo)

        assert hook_path.exists()
        assert hook_path.name == "pre-commit"
        content = hook_path.read_text()
        assert _HOOK_MARKER in content
        assert ".sdd/*" in content
        assert ".github/*" in content

    def test_hook_is_executable(self, fake_repo: Path) -> None:
        installer = GitHookInstaller(denied_paths=())
        hook_path = installer.install(fake_repo)

        mode = hook_path.stat().st_mode
        assert mode & stat.S_IXUSR

    def test_install_in_worktree(self, fake_worktree: Path) -> None:
        installer = GitHookInstaller(denied_paths=("src/secret/*",))
        hook_path = installer.install(fake_worktree)

        assert hook_path.exists()
        content = hook_path.read_text()
        assert "src/secret/*" in content

    def test_install_creates_hooks_dir(self, tmp_path: Path) -> None:
        git_dir = tmp_path / ".git"
        git_dir.mkdir()
        # No hooks dir yet

        installer = GitHookInstaller(denied_paths=())
        hook_path = installer.install(tmp_path)
        assert hook_path.exists()

    def test_install_nonexistent_path_raises(self) -> None:
        installer = GitHookInstaller()
        with pytest.raises(FileNotFoundError):
            installer.install("/nonexistent/path")

    def test_install_overwrites_existing_hook(self, fake_repo: Path) -> None:
        installer1 = GitHookInstaller(denied_paths=("old/*",))
        installer1.install(fake_repo)

        installer2 = GitHookInstaller(denied_paths=("new/*",))
        hook_path = installer2.install(fake_repo)

        content = hook_path.read_text()
        assert "new/*" in content


# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------


class TestGitHookUninstall:
    """Test pre-commit hook removal."""

    def test_uninstall_removes_bernstein_hook(self, fake_repo: Path) -> None:
        installer = GitHookInstaller(denied_paths=(".sdd/*",))
        installer.install(fake_repo)

        removed = installer.uninstall(fake_repo)
        assert removed
        assert not (fake_repo / ".git" / "hooks" / "pre-commit").exists()

    def test_uninstall_preserves_non_bernstein_hook(self, fake_repo: Path) -> None:
        hook_path = fake_repo / ".git" / "hooks" / "pre-commit"
        hook_path.write_text("#!/bin/sh\n# custom hook\nexit 0\n")

        installer = GitHookInstaller()
        removed = installer.uninstall(fake_repo)
        assert not removed
        assert hook_path.exists()

    def test_uninstall_no_hook_returns_false(self, fake_repo: Path) -> None:
        installer = GitHookInstaller()
        removed = installer.uninstall(fake_repo)
        assert not removed


# ---------------------------------------------------------------------------
# Is installed check
# ---------------------------------------------------------------------------


class TestGitHookIsInstalled:
    """Test installed hook detection."""

    def test_detects_installed_hook(self, fake_repo: Path) -> None:
        installer = GitHookInstaller(denied_paths=(".sdd/*",))
        installer.install(fake_repo)

        assert installer.is_installed(fake_repo)

    def test_no_hook_returns_false(self, fake_repo: Path) -> None:
        installer = GitHookInstaller()
        assert not installer.is_installed(fake_repo)

    def test_non_bernstein_hook_returns_false(self, fake_repo: Path) -> None:
        hook_path = fake_repo / ".git" / "hooks" / "pre-commit"
        hook_path.write_text("#!/bin/sh\nexit 0\n")

        installer = GitHookInstaller()
        assert not installer.is_installed(fake_repo)


# ---------------------------------------------------------------------------
# Hook content validation
# ---------------------------------------------------------------------------


class TestHookContent:
    """Test that generated hook scripts are well-formed."""

    def test_hook_has_shebang(self, fake_repo: Path) -> None:
        installer = GitHookInstaller(denied_paths=(".sdd/*",))
        hook_path = installer.install(fake_repo)
        content = hook_path.read_text()
        assert content.startswith("#!/usr/bin/env python3\n")

    def test_hook_contains_denied_patterns(self, fake_repo: Path) -> None:
        denied = (".sdd/*", ".github/*", "secrets/*")
        installer = GitHookInstaller(denied_paths=denied)
        hook_path = installer.install(fake_repo)
        content = hook_path.read_text()
        for pattern in denied:
            assert pattern in content

    def test_hook_has_main_guard(self, fake_repo: Path) -> None:
        installer = GitHookInstaller(denied_paths=())
        hook_path = installer.install(fake_repo)
        content = hook_path.read_text()
        assert 'if __name__ == "__main__"' in content
