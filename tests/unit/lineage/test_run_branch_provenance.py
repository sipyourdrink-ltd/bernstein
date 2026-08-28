"""What a run's branch carries must be recorded however it got there.

The merge hook records work that arrives through the orchestrator's merge.
These tests cover the ways work reaches a run branch without one -- a direct
commit, and a supervisor folding a worktree in outside the orchestrator --
because a hook that only sees merges leaves those runs with a spine holding
nothing the run produced.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

import pytest

from bernstein.core.lineage.merge_provenance import (
    RUN_BRANCH_STEP_PREFIX,
    record_run_branch_artifacts,
    run_branch_range,
)
from bernstein.core.lineage.spine import LineageSpine

KEY = b"k" * 32


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True, check=True).stdout.strip()


def _commit(repo: Path, path: str, body: str, message: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", message)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A trunk with history, plus a run branch off its tip.

    The trunk carries a commit of its own so a test can tell "what this run
    added" apart from "everything in the repository".
    """
    root = tmp_path / "proj"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    _commit(root, "trunk.txt", "trunk\n", "seed")
    _commit(root, "src/existing.py", "def old():\n    return 0\n", "trunk work")
    _git(root, "checkout", "-q", "-b", "run-20260828T120000Z")
    return root


def _rows(repo: Path, run_id: str = "run-1") -> list:
    return list(LineageSpine(repo / ".sdd" / "lineage", run_id=run_id, hmac_key=KEY).iter_entries())


def _record(repo: Path, run_id: str = "run-1") -> object:
    return record_run_branch_artifacts(
        worktree_root=repo,
        actor="orchestrator",
        lineage_root=repo / ".sdd" / "lineage",
        run_id=run_id,
        hmac_key=KEY,
        default_branch="main",
    )


def test_a_direct_commit_on_the_run_branch_is_recorded(repo: Path) -> None:
    """The case the merge hook cannot see: no merge ever happens.

    An agent commits straight onto the run branch. With a merge-only hook
    this run's spine holds nothing it produced.
    """
    _commit(repo, "src/feature.py", "def f():\n    return 1\n", "agent work")

    _record(repo)

    rows = _rows(repo)
    assert {r.artifact_path for r in rows} == {"src/feature.py"}
    assert rows[0].content_hash == "sha256:" + __import__("hashlib").sha256(b"def f():\n    return 1\n").hexdigest()


def test_a_merge_made_outside_the_orchestrator_is_recorded(repo: Path) -> None:
    """A supervisor folding a worktree in with plain git still gets rows.

    This is how work lands when something other than the orchestrator does
    the merge, so the recording must not depend on the merge having gone
    through the orchestrator's own path.
    """
    _git(repo, "checkout", "-q", "-b", "agent/backend-1")
    _commit(repo, "src/from_agent.py", "x = 1\n", "agent work")
    _git(repo, "checkout", "-q", "run-20260828T120000Z")
    _git(repo, "merge", "--no-ff", "--no-edit", "-m", "merge work from agent worktree backend-1", "agent/backend-1")

    _record(repo)

    assert {r.artifact_path for r in _rows(repo)} == {"src/from_agent.py"}


def test_trunk_history_is_not_recorded_as_this_runs_work(repo: Path) -> None:
    """The range is the merge-base, not the repository's first commit.

    Recording the whole branch would attribute every file in the repository
    to whichever run happened to finalize, which is worse than no rows: it
    is a chain that verifies while describing something that never happened.
    """
    _commit(repo, "src/feature.py", "def f():\n    return 1\n", "agent work")

    _record(repo)

    paths = {r.artifact_path for r in _rows(repo)}
    assert paths == {"src/feature.py"}
    assert "trunk.txt" not in paths
    assert "src/existing.py" not in paths


def test_a_path_the_merge_hook_already_recorded_is_not_recorded_twice(repo: Path) -> None:
    """Both hooks may fire for one run; a path must still count once.

    Double counting would inflate every per-run statistic drawn from the
    spine, and the inflation would look exactly like real coverage.
    """
    _commit(repo, "src/feature.py", "def f():\n    return 1\n", "agent work")

    first = _record(repo)
    second = _record(repo)

    assert len(first.recorded) == 1  # type: ignore[attr-defined]
    assert second.recorded == []  # type: ignore[attr-defined]
    assert len(_rows(repo)) == 1


def test_a_path_that_changed_again_is_recorded_again(repo: Path) -> None:
    """Dedup is on bytes, not on the path alone.

    A file edited twice in one run has two states worth attesting, and
    skipping the second would silently drop the version that actually
    landed.
    """
    _commit(repo, "src/feature.py", "v1\n", "first")
    _record(repo)
    _commit(repo, "src/feature.py", "v2\n", "second")
    _record(repo)

    hashes = {r.content_hash for r in _rows(repo)}
    assert len(hashes) == 2


def test_the_chain_does_not_record_its_own_storage(repo: Path) -> None:
    """A spine that attests its own files changes what it attests as it writes.

    A repository that tracks the state directory would otherwise have each
    pass record the previous pass's rows, growing without bound and burying
    the run's actual output. Reproduces by committing the state directory,
    which ``git add -A`` does the moment a recording has run.
    """
    _commit(repo, "src/feature.py", "v1\n", "first")
    _record(repo)
    _commit(repo, "src/feature.py", "v2\n", "second")  # sweeps .sdd/ in too
    _record(repo)

    paths = {r.artifact_path for r in _rows(repo)}
    assert paths == {"src/feature.py"}


def test_every_row_names_the_branch_head_it_was_read_at(repo: Path) -> None:
    """A row a verifier cannot tie back to a git object is unfalsifiable."""
    _commit(repo, "src/feature.py", "def f():\n    return 1\n", "agent work")
    _record(repo)

    head = _git(repo, "rev-parse", "HEAD")
    assert all(r.step_id == f"{RUN_BRANCH_STEP_PREFIX}{head}" for r in _rows(repo))


def test_no_trunk_ref_reports_an_unknown_range_rather_than_the_whole_tree(tmp_path: Path) -> None:
    """A clone with no trunk ref must not attribute every file to the run.

    Diffing from the empty tree makes the range the entire repository, so a
    shallow or single-branch clone - which has no ``origin/main`` and is the
    normal shape in CI - would record one spine row per tracked file and
    claim the run produced all of them. That is a false provenance record,
    not a conservative one, and it costs a lock-guarded write per file.

    Silence is still the thing to avoid: the range comes back empty and the
    caller logs why, rather than returning nothing quietly.
    """
    root = tmp_path / "orphan"
    root.mkdir()
    _git(root, "init", "-q", "-b", "run-only")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    _commit(root, "only.txt", "x\n", "sole commit")

    base, head = run_branch_range(root, default_branch="main")

    assert head == _git(root, "rev-parse", "HEAD")
    assert base == ""


def test_an_unknown_range_records_nothing_and_says_so(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    """The caller must leave a reason, so an empty spine is not a mystery."""
    root = tmp_path / "orphan-caller"
    root.mkdir()
    _git(root, "init", "-q", "-b", "run-only")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "T")
    _commit(root, "only.txt", "x\n", "sole commit")

    with caplog.at_level(logging.WARNING):
        result = record_run_branch_artifacts(
            worktree_root=root,
            actor="orchestrator",
            lineage_root=root / ".sdd" / "lineage",
            run_id="run-unknown-range",
            hmac_key=b"k" * 32,
            default_branch="main",
        )

    assert result.unknown_range is True
    assert result.recorded == []
    assert "not knowable" in caplog.text
