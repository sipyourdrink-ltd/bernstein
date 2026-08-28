"""The merge path must fill the spine, not just be able to.

``test_merge_provenance.py`` proves the recorder works when called. These
tests prove the production merge path calls it -- the half that was missing
for the whole life of issue #2789, where the write boundary existed and
simply had no caller on the CLI-adapter path.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from bernstein.core.agents.spawner_merge import _run_merge_and_push
from bernstein.core.lineage.spine import LineageSpine, SpineStatus


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


class _Session:
    """Minimal stand-in for ``AgentSession`` on the merge path."""

    def __init__(self, session_id: str = "sess-1") -> None:
        self.id = session_id
        self.task_ids: list[str] = ["task-1"]
        self.role = "worker"


class _MergeResult:
    def __init__(self, success: bool) -> None:
        self.success = success
        self.conflicting_files: list[str] = []
        self.error = ""


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repo on a non-default branch with an agent branch ready to merge.

    Non-default because the merge path refuses to land agent work on a
    protected trunk, which would short-circuit these tests before they
    reach the recording.
    """
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    (root / "seed.txt").write_text("seed\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "seed")

    _git(root, "checkout", "-q", "-b", "agent/sess-1")
    (root / "src").mkdir()
    (root / "src" / "feature.py").write_text("def f():\n    return 1\n", encoding="utf-8")
    (root / "seed.txt").unlink()
    _git(root, "add", "-A")
    _git(root, "commit", "-qm", "agent work")

    _git(root, "checkout", "-q", "-b", "integration", "main")

    key_path = tmp_path / "audit.key"
    key_path.write_bytes(b"k" * 32)
    key_path.chmod(0o600)  # the loader refuses a group/world-readable key
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(key_path))
    return root


def _merge_fn(repo: Path) -> Any:
    def _merge(session_id: str, repo_root: Path) -> _MergeResult:
        subprocess.run(
            ["git", "merge", "--no-ff", "-q", "-m", "merge agent work", f"agent/{session_id}"],
            cwd=repo_root,
            capture_output=True,
            check=True,
        )
        return _MergeResult(success=True)

    return _merge


def test_a_successful_merge_writes_a_row_per_landed_path(repo: Path, tmp_path: Path) -> None:
    """The defect: a merged run left a spine with no artifact provenance."""
    _run_merge_and_push(
        _Session(),  # type: ignore[arg-type]
        repo,
        _merge_fn(repo),
        run_id="run-1",
    )

    rows = list(LineageSpine(repo / ".sdd" / "lineage", run_id="run-1", hmac_key=b"k" * 32).iter_entries())

    assert {r.artifact_path for r in rows} == {"src/feature.py", "seed.txt"}
    assert {r.actor for r in rows} == {"agent/sess-1"}


def test_without_a_run_id_the_merge_still_lands(repo: Path) -> None:
    """Provenance is not allowed to gate the merge.

    A run id can be absent (a caller that predates the wiring). That must
    cost the rows, never the merge -- the work is already in git.
    """
    result = _run_merge_and_push(
        _Session(),  # type: ignore[arg-type]
        repo,
        _merge_fn(repo),
        run_id="",
    )

    assert result is not None
    assert result.success
    assert _git(repo, "log", "-1", "--pretty=%s") == "merge agent work"


def test_an_unreadable_worktree_does_not_stop_the_merge_being_attempted(tmp_path: Path) -> None:
    """Reading the provenance base must not decide whether the merge runs.

    The base is read before the merge so a fast-forward can be recorded, and
    ``run_git`` cannot chdir into a path that is not there: an unguarded read
    raises before the merge function is ever called, turning a provenance aid
    into a merge gate and taking the error away from the merge function that
    reports it properly. Asserts the call happened, not merely that nothing
    raised -- a silently skipped merge would also not raise.
    """
    called: list[Path] = []

    def _merge(session_id: str, repo_root: Path) -> _MergeResult:
        called.append(repo_root)
        return _MergeResult(success=False)

    missing = tmp_path / "gone"  # never created

    _run_merge_and_push(
        _Session(),  # type: ignore[arg-type]
        missing,
        _merge,
        run_id="run-1",
    )

    assert called == [missing]


def test_a_failing_recorder_does_not_undo_a_landed_merge(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The merge is durable in git and the rows are re-derivable from it.

    So a provenance failure must be loud and survivable, never a rollback
    of work that already landed.
    """
    import bernstein.core.lineage.merge_provenance as mp

    def _boom(**_kwargs: Any) -> Any:
        msg = "spine unavailable"
        raise RuntimeError(msg)

    monkeypatch.setattr(mp, "record_merge_artifacts", _boom)

    result = _run_merge_and_push(
        _Session(),  # type: ignore[arg-type]
        repo,
        _merge_fn(repo),
        run_id="run-1",
    )

    assert result is not None
    assert result.success
    assert _git(repo, "log", "-1", "--pretty=%s") == "merge agent work"
    # And the failure was actually reached: no rows, so the assertions
    # above are not passing because the recorder quietly succeeded.
    assert not (repo / ".sdd" / "lineage" / "run-1" / "spine.jsonl").exists()


def test_the_recorded_spine_is_no_longer_seal_only(repo: Path) -> None:
    """End to end: a merged run's spine now carries artifact provenance."""
    _run_merge_and_push(
        _Session(),  # type: ignore[arg-type]
        repo,
        _merge_fn(repo),
        run_id="run-1",
    )

    verdict = LineageSpine(repo / ".sdd" / "lineage", run_id="run-1", hmac_key=b"k" * 32).verify()

    assert verdict.status is SpineStatus.OK
    assert verdict.count == 2
