"""Tests for :mod:`bernstein.core.persistence.ledger_git` (#2358).

Git-ref anchoring for the durable work ledger: the chain travels with the
repo under ``refs/bernstein/work-ledger/<run-id>``, chunked into blobs.
These tests pin:

* Deterministic tree identity (two anchors of the same chain share a tree).
* Fail-closed anchoring (a broken chain never reaches the ref).
* Divergence refusal at both anchor and materialize time.
* Round-trip via a real ``git clone`` plus ref fetch.
* The gc policy that squashes anchor history.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from bernstein.core.persistence.ledger_git import (
    LEDGER_REF_PREFIX,
    LedgerAnchor,
    LedgerDivergenceError,
    LedgerGitError,
    anchor_ledger,
    fetch_ledger_ref,
    gc_ledger_ref,
    ledger_ref,
    list_ledger_runs,
    materialize_ledger,
)
from bernstein.core.persistence.work_ledger import (
    KIND_RUN_OPEN,
    KIND_TASK_COMPLETED,
    KIND_TASK_SCHEDULED,
    KIND_TASK_STARTED,
    LedgerReader,
    WorkLedger,
    run_ledger_dir,
)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo = tmp_path / "origin"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    (repo / "README.md").write_text("hello\n", encoding="utf-8")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "init")
    return repo


def _seed_ledger(repo: Path, run_id: str = "run-a", *, tasks: int = 2) -> WorkLedger:
    ledger = WorkLedger.open(run_ledger_dir(repo / ".sdd", run_id))
    ledger.append(kind=KIND_RUN_OPEN, payload={"run_id": run_id})
    for n in range(1, tasks + 1):
        ledger.append(kind=KIND_TASK_SCHEDULED, task_id=f"t{n}")
        ledger.append(kind=KIND_TASK_STARTED, task_id=f"t{n}")
        ledger.append(kind=KIND_TASK_COMPLETED, task_id=f"t{n}")
    return ledger


class TestRefNaming:
    def test_ledger_ref_shape(self) -> None:
        assert ledger_ref("run-a") == f"{LEDGER_REF_PREFIX}run-a"

    def test_ledger_ref_rejects_hostile_run_id(self) -> None:
        with pytest.raises(LedgerGitError):
            ledger_ref("../../evil")


class TestAnchor:
    def test_anchor_writes_ref_and_meta(self, repo: Path) -> None:
        ledger = _seed_ledger(repo)
        anchor = anchor_ledger(repo, ledger.ledger_dir, run_id="run-a")
        assert isinstance(anchor, LedgerAnchor)
        assert anchor.head_hash == ledger.head_hash
        assert anchor.entry_count == 7
        assert _git(repo, "rev-parse", "--verify", anchor.ref)
        assert "run-a" in list_ledger_runs(repo)

    def test_anchor_tree_is_deterministic(self, repo: Path, tmp_path: Path) -> None:
        """Two anchors of the same chain produce the identical tree sha."""
        ledger = _seed_ledger(repo)
        first = anchor_ledger(repo, ledger.ledger_dir, run_id="run-a")

        other = tmp_path / "other"
        other.mkdir()
        _git(other, "init", "-b", "main")
        _git(other, "config", "user.email", "test@example.com")
        _git(other, "config", "user.name", "Test")
        bucket = ledger.ledger_dir / "000000.jsonl"
        mirror_dir = run_ledger_dir(other / ".sdd", "run-a")
        mirror_dir.mkdir(parents=True, exist_ok=True)
        (mirror_dir / "000000.jsonl").write_bytes(bucket.read_bytes())
        second = anchor_ledger(other, mirror_dir, run_id="run-a")

        assert first.tree_sha == second.tree_sha

    def test_anchor_refuses_broken_chain(self, repo: Path) -> None:
        ledger = _seed_ledger(repo)
        bucket = ledger.ledger_dir / "000000.jsonl"
        lines = bucket.read_text(encoding="utf-8").splitlines()
        row = json.loads(lines[3])
        row["task_id"] = "evil"
        lines[3] = json.dumps(row, sort_keys=True, separators=(",", ":"))
        bucket.write_text("\n".join(lines) + "\n", encoding="utf-8")

        with pytest.raises(LedgerGitError):
            anchor_ledger(repo, ledger.ledger_dir, run_id="run-a")

    def test_reanchor_extends_previous_commit(self, repo: Path) -> None:
        ledger = _seed_ledger(repo)
        first = anchor_ledger(repo, ledger.ledger_dir, run_id="run-a")
        ledger.append(kind=KIND_TASK_SCHEDULED, task_id="t9")
        second = anchor_ledger(repo, ledger.ledger_dir, run_id="run-a")
        parents = _git(repo, "rev-list", "--parents", "-n", "1", second.commit_sha).split()
        assert first.commit_sha in parents[1:]

    def test_reanchor_identical_chain_is_idempotent(self, repo: Path) -> None:
        ledger = _seed_ledger(repo)
        first = anchor_ledger(repo, ledger.ledger_dir, run_id="run-a")
        second = anchor_ledger(repo, ledger.ledger_dir, run_id="run-a")
        assert second.commit_sha == first.commit_sha

    def test_anchor_refuses_divergent_ref(self, repo: Path, tmp_path: Path) -> None:
        """AC: two divergent resumes are detected and refused."""
        ledger = _seed_ledger(repo)
        anchor_ledger(repo, ledger.ledger_dir, run_id="run-a")

        # Resume B: a second operator extends the same anchored head...
        clone = tmp_path / "clone"
        _git(repo.parent, "clone", str(repo), str(clone))
        _git(clone, "config", "user.email", "b@example.com")
        _git(clone, "config", "user.name", "B")
        fetch_ledger_ref(clone, "run-a", remote="origin")
        materialize_ledger(clone, "run-a", run_ledger_dir(clone / ".sdd", "run-a"))
        remote_ledger = WorkLedger.open(run_ledger_dir(clone / ".sdd", "run-a"))
        remote_ledger.append(kind=KIND_TASK_STARTED, task_id="tb")
        anchor_ledger(clone, remote_ledger.ledger_dir, run_id="run-a")
        _git(clone, "push", "origin", f"{ledger_ref('run-a')}:{ledger_ref('run-a')}", "--force")

        # ...while resume A extended its local chain differently.
        ledger.append(kind=KIND_TASK_STARTED, task_id="ta")
        with pytest.raises(LedgerDivergenceError) as excinfo:
            anchor_ledger(repo, ledger.ledger_dir, run_id="run-a")
        message = str(excinfo.value)
        assert "diverge" in message.lower()
        assert "entry 7" in message


class TestMaterialize:
    def test_clone_fetch_materialize_round_trip(self, repo: Path, tmp_path: Path) -> None:
        """AC: the ledger travels with the repo and rebuilds on any clone."""
        ledger = _seed_ledger(repo)
        anchor = anchor_ledger(repo, ledger.ledger_dir, run_id="run-a")

        clone = tmp_path / "machine-b"
        _git(repo.parent, "clone", str(repo), str(clone))
        fetch_ledger_ref(clone, "run-a", remote="origin")
        dest = run_ledger_dir(clone / ".sdd", "run-a")
        result = materialize_ledger(clone, "run-a", dest)

        assert result.action == "created"
        verification = LedgerReader(dest).verify(expected_head=anchor.head_hash)
        assert verification.ok
        assert verification.entries == anchor.entry_count

    def test_materialize_unchanged_when_identical(self, repo: Path) -> None:
        ledger = _seed_ledger(repo)
        anchor_ledger(repo, ledger.ledger_dir, run_id="run-a")
        result = materialize_ledger(repo, "run-a", ledger.ledger_dir)
        assert result.action == "unchanged"

    def test_materialize_fast_forwards_stale_local(self, repo: Path, tmp_path: Path) -> None:
        ledger = _seed_ledger(repo)
        stale = tmp_path / "stale"
        stale.mkdir()
        bucket = ledger.ledger_dir / "000000.jsonl"
        lines = bucket.read_text(encoding="utf-8").splitlines()
        (stale / "000000.jsonl").write_text("\n".join(lines[:3]) + "\n", encoding="utf-8")

        anchor = anchor_ledger(repo, ledger.ledger_dir, run_id="run-a")
        result = materialize_ledger(repo, "run-a", stale)
        assert result.action == "fast-forwarded"
        assert LedgerReader(stale).verify(expected_head=anchor.head_hash).ok

    def test_materialize_refuses_when_local_ahead(self, repo: Path, tmp_path: Path) -> None:
        ledger = _seed_ledger(repo)
        anchor_ledger(repo, ledger.ledger_dir, run_id="run-a")
        ledger.append(kind=KIND_TASK_SCHEDULED, task_id="t9")
        with pytest.raises(LedgerGitError, match="ahead"):
            materialize_ledger(repo, "run-a", ledger.ledger_dir)

    def test_materialize_refuses_divergent_local(self, repo: Path, tmp_path: Path) -> None:
        ledger = _seed_ledger(repo)
        anchor_ledger(repo, ledger.ledger_dir, run_id="run-a")

        divergent = tmp_path / "divergent"
        divergent.mkdir()
        bucket = ledger.ledger_dir / "000000.jsonl"
        lines = bucket.read_text(encoding="utf-8").splitlines()
        (divergent / "000000.jsonl").write_text("\n".join(lines[:3]) + "\n", encoding="utf-8")
        fork = WorkLedger.open(divergent)
        fork.append(kind=KIND_TASK_STARTED, task_id="rogue")

        with pytest.raises(LedgerDivergenceError) as excinfo:
            materialize_ledger(repo, "run-a", divergent)
        assert "entry 3" in str(excinfo.value)

    def test_materialize_missing_ref_raises(self, repo: Path, tmp_path: Path) -> None:
        with pytest.raises(LedgerGitError, match="no anchored ledger"):
            materialize_ledger(repo, "run-x", tmp_path / "dest")


class TestGc:
    def test_gc_squashes_anchor_history(self, repo: Path) -> None:
        ledger = _seed_ledger(repo)
        anchor_ledger(repo, ledger.ledger_dir, run_id="run-a")
        ledger.append(kind=KIND_TASK_SCHEDULED, task_id="t9")
        anchor_ledger(repo, ledger.ledger_dir, run_id="run-a")
        ledger.append(kind=KIND_TASK_STARTED, task_id="t9")
        latest = anchor_ledger(repo, ledger.ledger_dir, run_id="run-a")

        result = gc_ledger_ref(repo, "run-a")
        assert result.dropped_commits == 2
        history = _git(repo, "rev-list", ledger_ref("run-a")).splitlines()
        assert len(history) == 1
        # The squashed commit preserves the exact anchored tree.
        assert _git(repo, "rev-parse", f"{ledger_ref('run-a')}^{{tree}}") == latest.tree_sha

    def test_gc_missing_ref_raises(self, repo: Path) -> None:
        with pytest.raises(LedgerGitError, match="no anchored ledger"):
            gc_ledger_ref(repo, "run-x")
