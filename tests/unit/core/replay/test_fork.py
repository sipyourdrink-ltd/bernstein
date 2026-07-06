"""Unit tests for ``core.replay.fork`` (issue #2295).

Fork-from-step reads the canonical event journal, finds the snapshot sha
recorded at step N, checks that snapshot commit out into a fresh
worktree, and starts a new run whose journal parent-links the fork point.

Covers:

* AC2 - fork records the parent run id and fork step in the child journal.
* AC3 - the snapshot sha stored in the journal at step N matches the ref
  actually created.
* AC4 - two forks from the same step produce isolated worktrees.
* AC5 - a tampered snapshot ref is detected because its sha no longer
  matches the journal-recorded sha.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from bernstein.core.replay.fork import (
    ForkError,
    fork_run,
    record_snapshot_event,
)
from bernstein.core.replay.journal import EventJournal, load_events
from bernstein.core.sandbox.snapshot import (
    commit_worktree_snapshot,
    snapshot_ref_name,
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "README.md").write_text("# repo\n", encoding="utf-8")
    _git(root, "add", "README.md")
    _git(root, "commit", "-q", "-m", "initial")
    return root


def _make_worktree(repo: Path, name: str) -> Path:
    wt = repo / ".sdd" / "worktrees" / name
    wt.parent.mkdir(parents=True, exist_ok=True)
    _git(repo, "worktree", "add", "-q", str(wt), "HEAD")
    return wt


def _seed_parent_run(
    repo: Path,
    sdd_dir: Path,
    run_id: str,
    *,
    fork_step: int,
    payload: bytes,
) -> tuple[EventJournal, str]:
    """Build a parent run journal with a real snapshot recorded at ``fork_step``.

    Returns the journal and the snapshot sha stored at ``fork_step``.
    """
    journal = EventJournal(run_id, sdd_dir)
    wt = _make_worktree(repo, f"wt-{run_id}")
    snapshot_sha = ""
    for i in range(fork_step + 2):
        if i == fork_step:
            (wt / "state.txt").write_bytes(payload)
            snapshot_sha = commit_worktree_snapshot(
                repo,
                wt,
                run_id=run_id,
                step_index=i,
            )
            record_snapshot_event(journal, snapshot_sha=snapshot_sha, step_index=i)
        else:
            journal.record("tick", step_index=i)
    return journal, snapshot_sha


def test_fork_records_parent_lineage(repo: Path, tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    _journal, snapshot_sha = _seed_parent_run(repo, sdd, "parent-1", fork_step=1, payload=b"snapshot-bytes")

    result = fork_run(sdd, "parent-1", from_step=1, repo_root=repo)

    # AC2: child journal parent-links the fork point.
    assert result.parent_run_id == "parent-1"
    assert result.from_step == 1
    assert result.snapshot_sha == snapshot_sha
    child_events = load_events(sdd / "runs" / result.new_run_id / "journal.jsonl")
    assert child_events, "child run journal must record the fork"
    head = child_events[0]
    assert head["event"] == "fork"
    assert head["parent_run_id"] == "parent-1"
    assert head["fork_step"] == 1
    assert head["snapshot_sha"] == snapshot_sha


def test_fork_snapshot_sha_matches_ref(repo: Path, tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    _seed_parent_run(repo, sdd, "parent-2", fork_step=0, payload=b"x")

    result = fork_run(sdd, "parent-2", from_step=0, repo_root=repo)

    # AC3: the journal-recorded sha equals the ref actually created.
    ref = snapshot_ref_name("parent-2", 0)
    resolved = _git(repo, "rev-parse", ref).strip()
    assert result.snapshot_sha == resolved


def test_fork_restores_byte_identical_worktree(repo: Path, tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    payload = b"deterministic\x00\x01payload"
    _seed_parent_run(repo, sdd, "parent-3", fork_step=1, payload=payload)

    result = fork_run(sdd, "parent-3", from_step=1, repo_root=repo)

    restored = Path(result.worktree_path) / "state.txt"
    assert restored.read_bytes() == payload


def test_two_forks_are_isolated(repo: Path, tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    _seed_parent_run(repo, sdd, "parent-4", fork_step=0, payload=b"base")

    a = fork_run(sdd, "parent-4", from_step=0, repo_root=repo)
    b = fork_run(sdd, "parent-4", from_step=0, repo_root=repo)

    # AC4: distinct worktrees, distinct run ids, no shared mutable state.
    assert a.new_run_id != b.new_run_id
    assert Path(a.worktree_path) != Path(b.worktree_path)
    (Path(a.worktree_path) / "state.txt").write_bytes(b"mutated-in-a")
    assert (Path(b.worktree_path) / "state.txt").read_bytes() == b"base"


def test_tampered_ref_is_detected(repo: Path, tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    _seed_parent_run(repo, sdd, "parent-5", fork_step=0, payload=b"orig")

    # Repoint the snapshot ref at a different commit than the journal recorded.
    other = _make_worktree(repo, "tamper")
    (other / "state.txt").write_bytes(b"tampered")
    bad_sha = commit_worktree_snapshot(repo, other, run_id="tamper", step_index=99)
    _git(repo, "update-ref", snapshot_ref_name("parent-5", 0), bad_sha)

    # AC5: fork refuses because the ref sha no longer matches the journal.
    with pytest.raises(ForkError, match="tamper|mismatch|does not match"):
        fork_run(sdd, "parent-5", from_step=0, repo_root=repo)


def test_fork_missing_snapshot_step_raises(repo: Path, tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    journal = EventJournal("parent-6", sdd)
    journal.record("tick", step_index=0)
    journal.record("tick", step_index=1)

    with pytest.raises(ForkError, match="no snapshot"):
        fork_run(sdd, "parent-6", from_step=1, repo_root=repo)


def test_fork_unknown_run_raises(repo: Path, tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    with pytest.raises(ForkError, match="no journal|not found"):
        fork_run(sdd, "does-not-exist", from_step=0, repo_root=repo)
