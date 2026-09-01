"""What did one task change? Resolved from the task's worktree branch.

``bernstein undo`` today finds commits by matching ``task:<task_id>``
against the last 50 commit subjects. Nothing in the tree writes that
string - the agent commit prompts emit ``[WIP] <title>`` and
``feat: <summary>`` - so the subject scan answers "nothing to undo" for
every real task. The lineage spine cannot answer either: a spine entry
carries ``artifact_path``/``content_hash``/``actor``/``step_id`` and no
task id, and CLI-adapter subprocess writes never cross that boundary.

The task's own worktree branch does know. These tests build a real repo
with a real linked worktree and pin the resolver against it:

1. the resolved set is exactly the paths the task's branch changed,
2. every path carries its pre- and post-change blob hash,
3. a path changed only on the integration branch is absent,
4. an unknown task id is refused rather than answered with an empty set,
5. ``bernstein undo --dry-run`` prints that set and leaves the tree
   byte-identical,
6. ``--dry-run`` without a task id is refused.

Test 3 is the load-bearing one: every later conflict check compares the
task's set against what other work touched, so an integration-only path
leaking into the set would make the reversal revert work the task never
did. The two-dot spec ``main..agent/<sid>`` reports exactly that path as
a deletion; only the three-dot spec excludes it.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.cli.commands.undo_cmd import undo_cmd
from bernstein.core.worktrees.change_set import (
    TaskChangeSetUnresolved,
    resolve_task_change_set,
)

SESSION_ID = "backend-abc123"
TASK_ID = "task-abc123"

# ---------------------------------------------------------------------------
# Real-git fixture
# ---------------------------------------------------------------------------


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Run git in *cwd* with a deterministic identity."""
    return subprocess.run(
        [
            "git",
            "-C",
            str(cwd),
            "-c",
            "user.email=test@bernstein",
            "-c",
            "user.name=test",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        check=True,
        capture_output=True,
        text=True,
    )


def _blob(cwd: Path, rev: str, path: str) -> str:
    """Return the blob sha of *path* at *rev*."""
    return _git(cwd, "rev-parse", f"{rev}:{path}").stdout.strip()


@pytest.fixture()
def task_repo(tmp_path: Path) -> Path:
    """A repo whose task worktree changed three files, main a fourth.

    Layout after this fixture:

    * ``main``: seed commit (``seed.txt``, ``mod.txt``, ``del.txt``) plus a
      later commit adding ``integ.txt`` - work the task never touched.
    * ``agent/<session>``: branched off the seed commit; adds
      ``added.txt``, modifies ``mod.txt``, deletes ``del.txt``.
    * ``.sdd/runtime/pids/<session>.json``: the record that binds the
      session to :data:`TASK_ID`.

    The task's commit subject is deliberately ``feat: add feature`` - the
    shape agents actually produce, carrying no ``task:<id>`` marker.
    """
    subprocess.run(["git", "init", "-q", "-b", "main", str(tmp_path)], check=True)
    (tmp_path / "seed.txt").write_text("seed\n")
    (tmp_path / "mod.txt").write_text("old\n")
    (tmp_path / "del.txt").write_text("gone\n")
    _git(tmp_path, "add", "seed.txt", "mod.txt", "del.txt")
    _git(tmp_path, "commit", "-q", "-m", "seed")

    worktrees = tmp_path / ".sdd" / "runtime" / "worktrees"
    worktrees.mkdir(parents=True, exist_ok=True)
    wt = worktrees / SESSION_ID
    _git(tmp_path, "worktree", "add", "-q", "-b", f"agent/{SESSION_ID}", str(wt), "main")

    (wt / "added.txt").write_text("new\n")
    (wt / "mod.txt").write_text("new content\n")
    (wt / "del.txt").unlink()
    _git(wt, "add", "-A", "added.txt", "mod.txt", "del.txt")
    _git(wt, "commit", "-q", "-m", "feat: add feature")

    # Work that landed on the integration branch after the task forked.
    (tmp_path / "integ.txt").write_text("other\n")
    _git(tmp_path, "add", "integ.txt")
    _git(tmp_path, "commit", "-q", "-m", "chore: unrelated main work")

    pids = tmp_path / ".sdd" / "runtime" / "pids"
    pids.mkdir(parents=True, exist_ok=True)
    (pids / f"{SESSION_ID}.json").write_text(
        json.dumps({"task_id": TASK_ID, "worker_pid": os.getpid()}),
        encoding="utf-8",
    )
    return tmp_path


# ---------------------------------------------------------------------------
# 1-4: change-set resolution
# ---------------------------------------------------------------------------


def test_change_set_names_exactly_the_paths_the_task_changed(task_repo: Path) -> None:
    """The resolved set is the task's three paths, in path order.

    The task's commit subject carries no ``task:<id>`` marker, so a
    resolver built on commit subjects would return nothing here.
    """
    subjects = _git(task_repo, "log", "--pretty=format:%s", f"agent/{SESSION_ID}").stdout
    assert f"task:{TASK_ID}" not in subjects, "fixture must not hand the resolver a subject marker"

    change_set = resolve_task_change_set(task_repo, TASK_ID)

    assert change_set.task_id == TASK_ID
    assert change_set.session_id == SESSION_ID
    assert change_set.branch == f"agent/{SESSION_ID}"
    assert [p.path for p in change_set.paths] == ["added.txt", "del.txt", "mod.txt"]
    assert [p.change_type for p in change_set.paths] == ["added", "deleted", "modified"]


def test_change_set_records_pre_and_post_blob_hashes_for_each_path(task_repo: Path) -> None:
    """Each path carries the blob it came from and the blob it became.

    An added path has no pre-image and a deleted path no post-image; both
    are ``None`` rather than git's all-zero sentinel, so a caller cannot
    mistake "no such blob" for a real content hash.
    """
    change_set = resolve_task_change_set(task_repo, TASK_ID)
    by_path = {p.path: p for p in change_set.paths}
    base = change_set.merge_base
    branch = f"agent/{SESSION_ID}"

    assert by_path["added.txt"].pre_hash is None
    assert by_path["added.txt"].post_hash == _blob(task_repo, branch, "added.txt")

    assert by_path["mod.txt"].pre_hash == _blob(task_repo, base, "mod.txt")
    assert by_path["mod.txt"].post_hash == _blob(task_repo, branch, "mod.txt")
    assert by_path["mod.txt"].pre_hash != by_path["mod.txt"].post_hash

    assert by_path["del.txt"].pre_hash == _blob(task_repo, base, "del.txt")
    assert by_path["del.txt"].post_hash is None


def test_integration_only_path_is_absent_from_the_change_set(task_repo: Path) -> None:
    """A path changed only on ``main`` is not part of the task's set.

    Load-bearing: ``integ.txt`` exists on the integration branch and not
    on the task branch. Diffed two-dot it shows up as a deletion the task
    never made, and a reversal built on that set would restore a file the
    task never removed while reporting a clean revert.
    """
    two_dot = _git(task_repo, "diff", "--name-only", "--no-renames", f"main..agent/{SESSION_ID}").stdout.split()
    assert "integ.txt" in two_dot, "fixture must reproduce the two-dot trap"

    change_set = resolve_task_change_set(task_repo, TASK_ID)

    assert "integ.txt" not in [p.path for p in change_set.paths]


def test_unknown_task_id_is_refused_not_answered_with_an_empty_set(task_repo: Path) -> None:
    """No worktree for the task means "cannot tell", not "changed nothing".

    An empty set is a task that touched no files; a task we cannot find is
    not that, and returning one for the other would let a reversal report
    success having reverted nothing.
    """
    with pytest.raises(TaskChangeSetUnresolved) as excinfo:
        resolve_task_change_set(task_repo, "task-that-never-ran")

    assert "task-that-never-ran" in str(excinfo.value)


# ---------------------------------------------------------------------------
# 5-6: the CLI dry run
# ---------------------------------------------------------------------------


def test_dry_run_prints_the_change_set_and_leaves_the_tree_byte_identical(
    task_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--dry-run`` reports the set and mutates nothing.

    ``git status --porcelain`` must come back byte-for-byte the same, and
    the printed report must name the task's paths and not the
    integration-only one.
    """
    monkeypatch.chdir(task_repo)
    before = _git(task_repo, "status", "--porcelain").stdout

    result = CliRunner().invoke(undo_cmd, [TASK_ID, "--dry-run"])

    after = _git(task_repo, "status", "--porcelain").stdout
    assert after == before, "dry run touched the working tree"
    assert result.exit_code == 0, result.output
    for path in ("added.txt", "del.txt", "mod.txt"):
        assert path in result.output
    assert "integ.txt" not in result.output
    # HEAD must be untouched too - no revert commit was created.
    assert _git(task_repo, "log", "--pretty=format:%s", "-n", "1").stdout == "chore: unrelated main work"


def test_dry_run_without_a_task_id_is_refused(
    task_repo: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``--all --dry-run`` fails loudly: a change set is per task.

    ``--all`` names no single task, so there is no recorded change set to
    print. Refusing with a non-zero exit keeps that from reading as "this
    session changed nothing".
    """
    monkeypatch.chdir(task_repo)

    result = CliRunner().invoke(undo_cmd, ["--all", "--dry-run"])

    assert result.exit_code != 0
    assert "--dry-run" in result.output
