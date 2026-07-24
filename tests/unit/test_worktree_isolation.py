"""Tests for AGENT-002 - worktree isolation validation."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from bernstein.core.worktree_isolation import (
    check_no_hardlink_leaks,
    check_sdd_not_shared,
    check_symlinks_read_only,
    validate_worktree_isolation,
)


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".sdd").mkdir()
    (root / ".sdd" / "state.json").write_text("{}")
    return root


@pytest.fixture
def worktree_path(tmp_path: Path) -> Path:
    wt = tmp_path / "worktrees" / "agent-abc123"
    wt.mkdir(parents=True)
    return wt


# ---------------------------------------------------------------------------
# check_sdd_not_shared
# ---------------------------------------------------------------------------


class TestSddNotShared:
    def test_no_sdd_passes(self, worktree_path: Path, repo_root: Path) -> None:
        violations = check_sdd_not_shared(worktree_path, repo_root)
        assert violations == []

    def test_real_sdd_passes(self, worktree_path: Path, repo_root: Path) -> None:
        (worktree_path / ".sdd").mkdir()
        violations = check_sdd_not_shared(worktree_path, repo_root)
        assert violations == []

    def test_symlinked_sdd_fails(self, worktree_path: Path, repo_root: Path) -> None:
        (worktree_path / ".sdd").symlink_to(repo_root / ".sdd")
        violations = check_sdd_not_shared(worktree_path, repo_root)
        assert len(violations) == 1
        assert "symlink" in violations[0].lower() or "parent repo" in violations[0].lower()


# ---------------------------------------------------------------------------
# check_symlinks_read_only
# ---------------------------------------------------------------------------


class TestSymlinksReadOnly:
    def test_no_symlinks_passes(self, worktree_path: Path, repo_root: Path) -> None:
        violations = check_symlinks_read_only(worktree_path, repo_root)
        assert violations == []

    def test_allowed_symlink_passes(self, worktree_path: Path, repo_root: Path) -> None:
        (repo_root / "node_modules").mkdir()
        (worktree_path / "node_modules").symlink_to(repo_root / "node_modules")
        violations = check_symlinks_read_only(worktree_path, repo_root, allowed_symlink_dirs=("node_modules",))
        assert violations == []

    def test_symlink_into_sdd_fails(self, worktree_path: Path, repo_root: Path) -> None:
        (worktree_path / "leaked").symlink_to(repo_root / ".sdd")
        violations = check_symlinks_read_only(worktree_path, repo_root)
        assert len(violations) == 1
        assert "mutable state" in violations[0].lower()


# ---------------------------------------------------------------------------
# check_no_hardlink_leaks
# ---------------------------------------------------------------------------


class TestHardlinkLeaks:
    def test_no_hardlinks_passes(self, worktree_path: Path, repo_root: Path) -> None:
        (worktree_path / ".sdd").mkdir()
        (worktree_path / ".sdd" / "clean.json").write_text("{}")
        violations = check_no_hardlink_leaks(worktree_path, repo_root)
        assert violations == []

    def test_hardlink_detected(self, worktree_path: Path, repo_root: Path) -> None:
        # Create a file in repo .sdd and hardlink it into worktree .sdd
        (worktree_path / ".sdd").mkdir()
        source = repo_root / ".sdd" / "state.json"
        target = worktree_path / ".sdd" / "state.json"
        try:
            os.link(source, target)
        except OSError:
            pytest.skip("Hardlinks not supported on this filesystem")

        violations = check_no_hardlink_leaks(worktree_path, repo_root)
        assert len(violations) == 1
        assert "hardlink" in violations[0].lower()

    def test_no_parent_dir_passes(self, worktree_path: Path, tmp_path: Path) -> None:
        # repo_root has no .sdd - nothing to leak
        repo_no_sdd = tmp_path / "empty_repo"
        repo_no_sdd.mkdir()
        violations = check_no_hardlink_leaks(worktree_path, repo_no_sdd)
        assert violations == []


# ---------------------------------------------------------------------------
# validate_worktree_isolation (combined)
# ---------------------------------------------------------------------------


class TestValidateIsolation:
    def test_clean_worktree_passes(self, worktree_path: Path, repo_root: Path) -> None:
        result = validate_worktree_isolation(worktree_path, repo_root)
        assert result.passed
        assert result.violations == []

    def test_symlinked_sdd_fails(self, worktree_path: Path, repo_root: Path) -> None:
        (worktree_path / ".sdd").symlink_to(repo_root / ".sdd")
        result = validate_worktree_isolation(worktree_path, repo_root)
        assert not result.passed
        assert len(result.violations) >= 1

    def test_allowed_dirs_passthrough(self, worktree_path: Path, repo_root: Path) -> None:
        (repo_root / "node_modules").mkdir()
        (worktree_path / "node_modules").symlink_to(repo_root / "node_modules")
        result = validate_worktree_isolation(worktree_path, repo_root, allowed_symlink_dirs=("node_modules",))
        assert result.passed

    def test_skip_hardlink_check(self, worktree_path: Path, repo_root: Path) -> None:
        result = validate_worktree_isolation(worktree_path, repo_root, check_hardlinks=False)
        assert result.passed


# ---------------------------------------------------------------------------
# NTFS junction handling (issue #2367)
# ---------------------------------------------------------------------------


class TestJunctionDetection:
    """Junctions must be treated like symlinks by the isolation checks.

    On Windows, ``Path.is_symlink()`` is False for NTFS junctions, so a
    junction from the worktree's ``.sdd/`` into the parent repo would
    bypass a symlink-only check.  The checks route through the platform
    layer's ``is_filesystem_link``, which also detects junctions.
    """

    def test_junction_sdd_flagged(
        self,
        worktree_path: Path,
        repo_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        sdd = worktree_path / ".sdd"
        sdd.mkdir()

        import bernstein.core.git.worktree_isolation as wi

        monkeypatch.setattr(wi, "is_filesystem_link", lambda p: Path(p) == sdd)
        violations = check_sdd_not_shared(worktree_path, repo_root)
        assert len(violations) == 1

    def test_junction_entry_into_parent_sdd_flagged(
        self,
        worktree_path: Path,
        repo_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        entry = worktree_path / "state"
        entry.mkdir()

        import bernstein.core.git.worktree_isolation as wi

        monkeypatch.setattr(wi, "is_filesystem_link", lambda p: Path(p) == entry)
        monkeypatch.setattr(
            wi,
            "_resolve_link_target",
            lambda p: (repo_root / ".sdd" / "state").resolve(),
        )
        violations = check_symlinks_read_only(worktree_path, repo_root)
        assert len(violations) == 1
        assert "mutable state" in violations[0]

    def test_plain_dirs_still_pass(self, worktree_path: Path, repo_root: Path) -> None:
        (worktree_path / ".sdd").mkdir()
        (worktree_path / "src").mkdir()
        result = validate_worktree_isolation(worktree_path, repo_root)
        assert result.passed


# ---------------------------------------------------------------------------
# Unresolvable links (fail-closed)
# ---------------------------------------------------------------------------


class TestUnresolvableLink:
    """``Path.resolve()`` can raise (``ELOOP`` symlink cycle, permission error,
    or a resolver-detected ``RuntimeError``).  ``_resolve_link_target`` must
    swallow those and return ``None`` so callers record a violation and the
    spawn aborts through ``create()``'s cleanup path, instead of letting a raw
    filesystem error escape and leak the worktree + lock (issue #2643).
    """

    def test_resolve_link_target_returns_none_on_oserror(self) -> None:
        import bernstein.core.git.worktree_isolation as wi

        fake = MagicMock()
        fake.resolve.side_effect = OSError("[Errno 62] Too many levels of symbolic links")
        assert wi._resolve_link_target(fake) is None

    def test_resolve_link_target_returns_none_on_runtime_error(self) -> None:
        import bernstein.core.git.worktree_isolation as wi

        fake = MagicMock()
        fake.resolve.side_effect = RuntimeError("Symlink loop")
        assert wi._resolve_link_target(fake) is None

    def test_resolve_link_target_passes_through_resolved_path(self, tmp_path: Path) -> None:
        import bernstein.core.git.worktree_isolation as wi

        real = tmp_path / "real"
        real.mkdir()
        assert wi._resolve_link_target(real) == real.resolve()

    def test_unresolvable_sdd_link_recorded_as_violation(
        self,
        worktree_path: Path,
        repo_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import bernstein.core.git.worktree_isolation as wi

        sdd = worktree_path / ".sdd"
        sdd.mkdir()
        monkeypatch.setattr(wi, "is_filesystem_link", lambda p: Path(p) == sdd)
        monkeypatch.setattr(wi, "_resolve_link_target", lambda _p: None)

        violations = check_sdd_not_shared(worktree_path, repo_root)
        assert len(violations) == 1
        assert "could not be resolved" in violations[0].lower()

    def test_unresolvable_symlink_entry_recorded_as_violation(
        self,
        worktree_path: Path,
        repo_root: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        import bernstein.core.git.worktree_isolation as wi

        entry = worktree_path / "state"
        entry.mkdir()
        monkeypatch.setattr(wi, "is_filesystem_link", lambda p: Path(p) == entry)
        monkeypatch.setattr(wi, "_resolve_link_target", lambda _p: None)

        violations = check_symlinks_read_only(worktree_path, repo_root)
        assert len(violations) == 1
        assert "could not be resolved" in violations[0].lower()
