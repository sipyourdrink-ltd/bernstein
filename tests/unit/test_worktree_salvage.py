"""Tests for worktree cleanup salvage (audit-088).

These tests use a real git repo + real ``git worktree add`` because the salvage
codepath shells out to ``git`` for status, add, commit, branch -M, and diff —
mocking all of that would defeat the purpose.  Each test runs in ``tmp_path``
so no state escapes the sandbox.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bernstein.core.git.salvage import salvage_worktree
from bernstein.core.git.worktree import WorktreeManager


def _run(cmd: list[str], cwd: Path) -> None:
    """Run a git command, raising on failure — test helper."""
    subprocess.run(
        cmd,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


@pytest.fixture
def repo_with_worktree(tmp_path: Path) -> tuple[Path, Path, str]:
    """Create a real git repo with a real worktree checked out on a fresh branch.

    Returns:
        (repo_root, worktree_path, session_id)
    """
    repo_root = tmp_path / "repo"
    repo_root.mkdir()

    # Init with a named default branch so we do not depend on the user's
    # global ``init.defaultBranch`` setting.
    _run(["git", "init", "-b", "main"], repo_root)
    _run(["git", "config", "user.email", "test@example.com"], repo_root)
    _run(["git", "config", "user.name", "Test User"], repo_root)
    # commit.gpgsign may be on globally; turn it off so the test does not
    # block on a signing prompt.
    _run(["git", "config", "commit.gpgsign", "false"], repo_root)

    # Seed an initial commit so HEAD is valid (salvage needs HEAD to diff against).
    seed = repo_root / "README.md"
    seed.write_text("seed\n", encoding="utf-8")
    _run(["git", "add", "README.md"], repo_root)
    _run(["git", "commit", "-m", "seed"], repo_root)

    session_id = "test-salvage-session"
    worktree_path = repo_root / ".sdd" / "worktrees" / session_id
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    _run(
        ["git", "worktree", "add", "-b", f"agent/{session_id}", str(worktree_path)],
        repo_root,
    )
    # The worktree inherits config from the main repo for user.email/name +
    # commit.gpgsign, so no extra setup is needed inside the worktree.

    return repo_root, worktree_path, session_id


def _dirty_the_worktree(worktree_path: Path) -> tuple[str, str]:
    """Make both a tracked edit and an untracked addition.

    Returns:
        (tracked_content, untracked_content)
    """
    tracked_content = "seed\nmodified by agent\n"
    (worktree_path / "README.md").write_text(tracked_content, encoding="utf-8")

    untracked_content = "brand new file\n"
    (worktree_path / "new_feature.py").write_text(untracked_content, encoding="utf-8")

    return tracked_content, untracked_content


def test_salvage_on_clean_worktree_is_noop(repo_with_worktree: tuple[Path, Path, str]) -> None:
    """A clean worktree should report nothing to salvage and not touch disk."""
    repo_root, worktree_path, session_id = repo_with_worktree

    result = salvage_worktree(repo_root, worktree_path, session_id, push=False)

    assert result.had_changes is False
    assert result.salvaged is False
    assert result.branch is None
    assert result.patch_path is None
    # No salvage dir should have been created for a clean tree.
    assert not (repo_root / ".sdd" / "runtime" / "salvage").exists()


def test_salvage_creates_branch_and_filesystem_fallback(
    repo_with_worktree: tuple[Path, Path, str],
) -> None:
    """Dirty worktree -> salvage/<id> branch exists + filesystem fallback written."""
    repo_root, worktree_path, session_id = repo_with_worktree
    _dirty_the_worktree(worktree_path)

    # push=False because the fixture has no remote; we still want the branch path.
    result = salvage_worktree(repo_root, worktree_path, session_id, push=False)

    assert result.had_changes is True
    assert result.salvaged is True

    # 1. Salvage branch must exist locally.
    assert result.branch == f"salvage/{session_id}"
    branches = subprocess.run(
        ["git", "branch", "--list", result.branch],
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout
    assert result.branch in branches, f"salvage branch missing: {branches!r}"

    # 2. Filesystem fallback must also exist.
    assert result.patch_path is not None
    assert result.patch_path.is_dir()
    assert (result.patch_path / "diff.patch").is_file()
    assert (result.patch_path / "untracked.json").is_file()
    assert (result.patch_path / "README.txt").is_file()

    # 3. The recorded untracked list must mention our new file.
    assert "new_feature.py" in result.untracked_files

    # 4. Branch was not pushed (no remote).
    assert result.branch_pushed is False


def test_salvage_diff_is_recoverable_from_branch(
    repo_with_worktree: tuple[Path, Path, str],
) -> None:
    """The salvage branch's tree should contain both the modified + new file."""
    repo_root, worktree_path, session_id = repo_with_worktree
    _dirty_the_worktree(worktree_path)

    result = salvage_worktree(repo_root, worktree_path, session_id, push=False)
    assert result.salvaged
    assert result.branch is not None

    # The tracked edit should show up in ``git diff main...salvage/<id>``.
    diff = subprocess.run(
        ["git", "diff", f"main...{result.branch}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout
    assert "modified by agent" in diff, f"tracked edit missing from salvage diff: {diff!r}"
    # And the untracked file is now a new-file hunk on the branch.
    assert "new_feature.py" in diff, f"untracked file missing from salvage diff: {diff!r}"


def test_salvage_diff_is_recoverable_from_filesystem_fallback(
    repo_with_worktree: tuple[Path, Path, str],
) -> None:
    """Even without the branch, ``diff.patch`` + ``untracked.json`` recover the work."""
    import base64
    import json

    repo_root, worktree_path, session_id = repo_with_worktree
    _, untracked_content = _dirty_the_worktree(worktree_path)

    result = salvage_worktree(repo_root, worktree_path, session_id, push=False)
    assert result.patch_path is not None

    patch = (result.patch_path / "diff.patch").read_text(encoding="utf-8")
    assert "modified by agent" in patch, f"tracked diff not captured: {patch!r}"

    payload = json.loads((result.patch_path / "untracked.json").read_text(encoding="utf-8"))
    names = [entry["path"] for entry in payload["files"]]
    assert "new_feature.py" in names

    # base64-decoded bytes must match what we wrote into the worktree.
    new_feature_entry = next(e for e in payload["files"] if e["path"] == "new_feature.py")
    recovered = base64.b64decode(new_feature_entry["base64"]).decode("utf-8")
    assert recovered == untracked_content


def test_worktree_manager_cleanup_runs_salvage_before_remove(
    repo_with_worktree: tuple[Path, Path, str],
) -> None:
    """End-to-end: WorktreeManager.cleanup() salvages dirty state before deleting."""
    repo_root, worktree_path, session_id = repo_with_worktree
    _dirty_the_worktree(worktree_path)

    mgr = WorktreeManager(repo_root=repo_root, salvage_on_cleanup=True, salvage_push=False)
    mgr.cleanup(session_id)

    # 1. Worktree directory is gone.
    assert not worktree_path.exists(), "worktree should have been removed"

    # 2. salvage/<id> branch exists.
    branches = subprocess.run(
        ["git", "branch", "--list", f"salvage/{session_id}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout
    assert f"salvage/{session_id}" in branches, f"salvage branch missing: {branches!r}"

    # 3. Manager exposes the salvage result for observability.
    assert mgr.last_salvage is not None
    assert mgr.last_salvage.had_changes is True
    assert mgr.last_salvage.salvaged is True
    assert mgr.last_salvage.branch == f"salvage/{session_id}"

    # 4. Filesystem fallback is still there after the worktree is gone.
    assert mgr.last_salvage.patch_path is not None
    assert (mgr.last_salvage.patch_path / "diff.patch").is_file()


def test_worktree_manager_cleanup_with_salvage_disabled(
    repo_with_worktree: tuple[Path, Path, str],
) -> None:
    """When salvage is disabled, dirty state is lost (regression guard)."""
    repo_root, worktree_path, session_id = repo_with_worktree
    _dirty_the_worktree(worktree_path)

    mgr = WorktreeManager(repo_root=repo_root, salvage_on_cleanup=False)
    mgr.cleanup(session_id)

    assert not worktree_path.exists()
    # No salvage branch, no last_salvage.
    branches = subprocess.run(
        ["git", "branch", "--list", f"salvage/{session_id}"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout.strip()
    assert branches == "", f"expected no salvage branch when disabled, got {branches!r}"
    assert mgr.last_salvage is None
