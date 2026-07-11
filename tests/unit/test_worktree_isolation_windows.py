"""End-to-end worktree-isolation coverage under a mocked Windows platform.

Acceptance criterion 4 of the Windows-parity work asks for a full goal to
run end to end on a Windows host with worktree isolation intact. A real
Windows host is one way to prove that; the observable contract it checks is
that the worktree-isolation spine plus the Windows worktree-management
helpers hold together:

* the isolation validator must reject an NTFS *junction* from a worktree's
  ``.sdd/`` into the parent repo -- the Windows-specific bypass that
  ``Path.is_symlink()`` misses and that would let two agents clobber each
  other's state;
* a clean, self-contained worktree must pass isolation unchanged; and
* teardown must use the Windows-robust removal path (extended-length
  prefix on long paths, read-only clearing, transient-lock retry) and
  leave no leaked tree.

These are pure-logic branches once the OS boundary (``is_filesystem_link``
junction probing, ``shutil.rmtree``) is mocked, so the contract is
exercised here on any host by flipping ``IS_WINDOWS`` and substituting a
junction probe. The production validator and removal logic are the real
ones; only the kernel boundary is faked.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from bernstein.core.worktree_isolation import validate_worktree_isolation

import bernstein.core.config.platform_compat as pc
import bernstein.core.git.worktree_isolation as wi


@pytest.fixture
def repo_root(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    (root / ".sdd").mkdir()
    (root / ".sdd" / "state.json").write_text("{}")
    return root


@pytest.fixture
def worktree_path(tmp_path: Path) -> Path:
    wt = tmp_path / "worktrees" / "agent-win01"
    wt.mkdir(parents=True)
    return wt


class TestWindowsWorktreeIsolationEndToEnd:
    """Full validate_worktree_isolation run under mocked Windows semantics."""

    def test_clean_windows_worktree_passes(
        self,
        repo_root: Path,
        worktree_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        # A fresh, worktree-local .sdd is the correct isolated shape.
        (worktree_path / ".sdd").mkdir()
        (worktree_path / ".sdd" / "state.json").write_text("{}")

        result = validate_worktree_isolation(worktree_path, repo_root)
        assert result.passed is True
        assert result.violations == []

    def test_junction_sdd_into_parent_is_rejected(
        self,
        repo_root: Path,
        worktree_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        # Simulate an NTFS junction: a real directory that is_symlink() would
        # report as False, but the platform layer's is_filesystem_link()
        # detects as a link, resolving into the parent repo's .sdd.
        sdd = worktree_path / ".sdd"
        sdd.mkdir()
        parent_sdd = repo_root / ".sdd"
        monkeypatch.setattr(wi, "is_filesystem_link", lambda p: Path(p) == sdd)
        monkeypatch.setattr(wi, "_resolve_link_target", lambda p: parent_sdd.resolve())

        result = validate_worktree_isolation(worktree_path, repo_root)
        assert result.passed is False
        assert any("parent repo" in v.lower() or "symlink" in v.lower() for v in result.violations)

    def test_windows_teardown_removes_tree_without_leak(
        self,
        worktree_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        # Keep the real posix path (the extended-length translation is
        # covered separately) so the real Windows removal loop runs against
        # a real tree and must leave nothing behind.
        monkeypatch.setattr(pc, "to_extended_length_path", lambda p: str(p))
        (worktree_path / ".sdd").mkdir()
        (worktree_path / ".sdd" / "state.json").write_text("{}")
        (worktree_path / "nested").mkdir()

        assert pc.robust_rmtree(worktree_path) is True
        assert not worktree_path.exists()

    def test_windows_teardown_retries_then_reports_failure_on_locked_tree(
        self,
        worktree_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        monkeypatch.setattr(pc, "to_extended_length_path", lambda p: str(p))
        monkeypatch.setattr(pc.time, "sleep", lambda _s: None)
        attempts = {"n": 0}

        def _locked(_target: object, **_kwargs: object) -> None:
            attempts["n"] += 1
            raise OSError("sharing violation")

        monkeypatch.setattr(pc.shutil, "rmtree", _locked)

        assert pc.robust_rmtree(worktree_path, max_attempts=3, retry_delay_s=0.0) is False
        # Retried up to the attempt ceiling rather than giving up on first lock.
        assert attempts["n"] == 3

    def test_long_worktree_path_gets_extended_length_prefix(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        long_path = "C:\\worktrees\\" + "a" * 300 + "\\.sdd"
        translated = pc.to_extended_length_path(long_path)
        assert translated.startswith("\\\\?\\")
        assert translated.endswith(".sdd")

    def test_long_unc_worktree_path_gets_unc_extended_prefix(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        unc = "\\\\server\\share\\" + "b" * 300
        translated = pc.to_extended_length_path(unc)
        assert translated.startswith("\\\\?\\UNC\\")

    def test_short_windows_path_is_left_unprefixed(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setattr(pc, "IS_WINDOWS", True)
        short = "C:\\wt\\.sdd"
        translated = pc.to_extended_length_path(short)
        assert not translated.startswith("\\\\?\\")
