"""Tests for graveyard preservation of unmerged agent commits (audit-097).

When ``cleanup_all_stale`` encounters a PID-less worktree whose
``agent/<sid>`` branch has commits not reachable from ``main``, it must
preserve those commits to ``refs/graveyard/<sid>-<ts>`` and write a
portable bundle at ``.sdd/graveyard/<sid>-<ts>.bundle`` *before* the
destructive ``git worktree remove --force`` + ``git branch -D`` runs.

These tests use a real git repository in ``tmp_path`` rather than mocks
because the rescue path shells out for ``rev-list``, ``update-ref``, and
``bundle create``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bernstein.core.git.worktree import (
    WorktreeManager,
    _count_unmerged_commits,
    preserve_branch_to_graveyard,
    purge_graveyard,
)


def _run(cmd: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Init a real git repo on ``main`` with one seed commit."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _run(["git", "init", "-b", "main"], repo)
    _run(["git", "config", "user.email", "test@example.com"], repo)
    _run(["git", "config", "user.name", "Test User"], repo)
    _run(["git", "config", "commit.gpgsign", "false"], repo)

    (repo / "README.md").write_text("seed\n", encoding="utf-8")
    _run(["git", "add", "README.md"], repo)
    _run(["git", "commit", "-m", "seed"], repo)
    return repo


def _add_worktree_with_commits(repo: Path, session_id: str, commit_count: int) -> Path:
    """Create ``agent/<sid>`` with *commit_count* commits on top of main."""
    wt = repo / ".sdd" / "worktrees" / session_id
    wt.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "-b", f"agent/{session_id}", str(wt)], repo)

    for i in range(commit_count):
        (wt / f"file_{i}.txt").write_text(f"content {i}\n", encoding="utf-8")
        _run(["git", "add", f"file_{i}.txt"], wt)
        _run(["git", "commit", "-m", f"agent commit {i}"], wt)
    return wt


# ----------------------------------------------------------------------------
# _count_unmerged_commits
# ----------------------------------------------------------------------------


def test_count_unmerged_commits_returns_positive_for_branch_ahead(repo: Path) -> None:
    _add_worktree_with_commits(repo, "sid-1", commit_count=3)
    assert _count_unmerged_commits(repo, "agent/sid-1", base="main") == 3


def test_count_unmerged_commits_returns_zero_for_branch_at_main(repo: Path) -> None:
    wt = repo / ".sdd" / "worktrees" / "sid-empty"
    wt.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "-b", "agent/sid-empty", str(wt)], repo)
    assert _count_unmerged_commits(repo, "agent/sid-empty", base="main") == 0


def test_count_unmerged_commits_returns_zero_for_missing_branch(repo: Path) -> None:
    assert _count_unmerged_commits(repo, "agent/does-not-exist", base="main") == 0


# ----------------------------------------------------------------------------
# preserve_branch_to_graveyard
# ----------------------------------------------------------------------------


def test_preserve_branch_to_graveyard_creates_ref_and_bundle(repo: Path) -> None:
    _add_worktree_with_commits(repo, "crashy", commit_count=2)

    # Snapshot the tip SHA so we can confirm the graveyard ref matches.
    tip_sha = _run(["git", "rev-parse", "agent/crashy"], repo).stdout.strip()

    bundle_path = preserve_branch_to_graveyard(repo, "crashy")

    # Bundle was written at the documented location.
    assert bundle_path is not None
    assert bundle_path.parent == repo / ".sdd" / "graveyard"
    assert bundle_path.suffix == ".bundle"
    assert bundle_path.name.startswith("crashy-")
    assert bundle_path.is_file()
    assert bundle_path.stat().st_size > 0

    # Graveyard ref exists under refs/graveyard/crashy-<ts> and points at the
    # original tip SHA.
    result = _run(
        ["git", "for-each-ref", "--format=%(refname) %(objectname)", "refs/graveyard/"],
        repo,
    )
    refs = result.stdout.strip().splitlines()
    assert len(refs) == 1
    ref_name, ref_sha = refs[0].split()
    assert ref_name.startswith("refs/graveyard/crashy-")
    assert ref_sha == tip_sha

    # Commits are still reachable via the graveyard ref.
    assert _count_unmerged_commits(repo, ref_name, base="main") == 2


def test_preserve_branch_to_graveyard_missing_branch_returns_none(repo: Path) -> None:
    assert preserve_branch_to_graveyard(repo, "nonexistent") is None


# ----------------------------------------------------------------------------
# cleanup_all_stale — end-to-end behavior
# ----------------------------------------------------------------------------


def test_cleanup_all_stale_preserves_unmerged_commits_to_graveyard(repo: Path) -> None:
    """Crashed worktree with commits is rescued before the force-nuke."""
    _add_worktree_with_commits(repo, "crashed-with-work", commit_count=3)

    # Capture the tip SHA before cleanup so we can verify commits survive.
    tip_sha = _run(
        ["git", "rev-parse", "agent/crashed-with-work"],
        repo,
    ).stdout.strip()

    mgr = WorktreeManager(repo_root=repo, salvage_on_cleanup=False)
    cleaned = mgr.cleanup_all_stale()

    assert cleaned == 1

    # Worktree directory is gone.
    assert not (repo / ".sdd" / "worktrees" / "crashed-with-work").exists()

    # Graveyard ref points at the saved tip; commit is still reachable.
    refs = (
        _run(
            ["git", "for-each-ref", "--format=%(refname) %(objectname)", "refs/graveyard/"],
            repo,
        )
        .stdout.strip()
        .splitlines()
    )
    assert len(refs) == 1
    ref_name, ref_sha = refs[0].split()
    assert ref_name.startswith("refs/graveyard/crashed-with-work-")
    assert ref_sha == tip_sha

    # Bundle is on disk and non-empty.
    bundles = list((repo / ".sdd" / "graveyard").glob("crashed-with-work-*.bundle"))
    assert len(bundles) == 1
    assert bundles[0].stat().st_size > 0


def test_cleanup_all_stale_nukes_clean_worktree_without_graveyard(repo: Path) -> None:
    """Worktree with no unmerged commits is removed as before (no graveyard)."""
    wt = repo / ".sdd" / "worktrees" / "clean-crash"
    wt.parent.mkdir(parents=True, exist_ok=True)
    _run(["git", "worktree", "add", "-b", "agent/clean-crash", str(wt)], repo)
    # No commits after branch creation — branch tip == main tip.

    mgr = WorktreeManager(repo_root=repo, salvage_on_cleanup=False)
    cleaned = mgr.cleanup_all_stale()

    assert cleaned == 1
    assert not wt.exists()

    # No graveyard ref or bundle should be created for a clean branch.
    refs = _run(
        ["git", "for-each-ref", "--format=%(refname)", "refs/graveyard/"],
        repo,
    ).stdout.strip()
    assert refs == ""

    graveyard_dir = repo / ".sdd" / "graveyard"
    if graveyard_dir.exists():
        assert list(graveyard_dir.iterdir()) == []


# ----------------------------------------------------------------------------
# purge_graveyard
# ----------------------------------------------------------------------------


def test_purge_graveyard_keeps_recent_entries(repo: Path) -> None:
    _add_worktree_with_commits(repo, "recent", commit_count=1)
    preserve_branch_to_graveyard(repo, "recent")

    # Default 14-day window — fresh entry should NOT be purged.
    purged = purge_graveyard(repo, older_than_days=14)
    assert purged == 0

    refs = (
        _run(
            ["git", "for-each-ref", "--format=%(refname)", "refs/graveyard/"],
            repo,
        )
        .stdout.strip()
        .splitlines()
    )
    assert len(refs) == 1

    bundles = list((repo / ".sdd" / "graveyard").glob("*.bundle"))
    assert len(bundles) == 1


def test_purge_graveyard_zero_days_purges_everything(repo: Path) -> None:
    _add_worktree_with_commits(repo, "stale", commit_count=1)
    preserve_branch_to_graveyard(repo, "stale")

    # older_than_days=0 -> cutoff == now -> everything qualifies.
    purged = purge_graveyard(repo, older_than_days=0)
    assert purged >= 2  # 1 ref + 1 bundle

    refs = _run(
        ["git", "for-each-ref", "--format=%(refname)", "refs/graveyard/"],
        repo,
    ).stdout.strip()
    assert refs == ""

    bundles = list((repo / ".sdd" / "graveyard").glob("*.bundle"))
    assert bundles == []


def test_purge_graveyard_rejects_negative_age(repo: Path) -> None:
    with pytest.raises(ValueError, match=">= 0"):
        purge_graveyard(repo, older_than_days=-1)
