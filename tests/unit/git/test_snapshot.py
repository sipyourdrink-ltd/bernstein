"""Tests for ``bernstein.core.git.snapshot``.

Each test creates a real throwaway repo under ``tmp_path``. We exercise
the real git plumbing because the module's whole point is to be a thin
wrapper around it - mocking ``subprocess`` would test nothing useful.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from bernstein.core.git.git_basic import GitResult

from bernstein.core.git.snapshot import (
    SNAPSHOT_REF_PREFIX,
    STACK_REF_PREFIX,
    Snapshot,
    SnapshotError,
    SnapshotStore,
    stack_clear,
    stack_list,
    stack_push,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(*args: str, cwd: Path) -> str:
    """Run ``git`` and return stdout; raise on non-zero exit."""
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """Create a minimal initialised repo with one commit at HEAD."""
    _git("init", "-q", "-b", "main", cwd=tmp_path)
    _git("config", "user.email", "test@example.com", cwd=tmp_path)
    _git("config", "user.name", "Test", cwd=tmp_path)
    _git("config", "commit.gpgsign", "false", cwd=tmp_path)
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")
    _git("add", "README.md", cwd=tmp_path)
    _git("commit", "-q", "-m", "initial", cwd=tmp_path)
    return tmp_path


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


def test_store_construction_rejects_non_repo(tmp_path: Path) -> None:
    """SnapshotStore raises when the path is not a git work tree."""
    with pytest.raises(SnapshotError):
        SnapshotStore(tmp_path)


def test_store_construction_accepts_real_repo(repo: Path) -> None:
    """SnapshotStore accepts a freshly initialised repo."""
    store = SnapshotStore(repo)
    assert store.cwd == repo


# ---------------------------------------------------------------------------
# take()
# ---------------------------------------------------------------------------


def test_take_captures_untracked_file_into_tree(repo: Path) -> None:
    """A snapshot includes untracked files even though the agent never staged them."""
    (repo / "new.txt").write_text("fresh\n", encoding="utf-8")
    store = SnapshotStore(repo)

    snap = store.take(task_id="T-123", tool_call_id="tc-1", agent_id="agent:claude")

    assert snap.task_id == "T-123"
    assert snap.tool_call_id == "tc-1"
    assert snap.agent_id == "agent:claude"
    assert snap.ref.startswith(SNAPSHOT_REF_PREFIX)
    # The tree must contain new.txt because untracked files are
    # supposed to be captured.
    listing = _git("ls-tree", "-r", snap.tree_sha, cwd=repo)
    assert "new.txt" in listing
    assert "README.md" in listing


def test_take_does_not_disturb_real_index(repo: Path) -> None:
    """Snapshotting must preserve the agent's existing staged state."""
    (repo / "staged.txt").write_text("staged\n", encoding="utf-8")
    _git("add", "staged.txt", cwd=repo)
    before = _git("diff", "--cached", "--name-only", cwd=repo).strip()

    SnapshotStore(repo).take(task_id="T-1")

    after = _git("diff", "--cached", "--name-only", cwd=repo).strip()
    assert before == after == "staged.txt"


def test_take_persists_metadata_sidecar(repo: Path) -> None:
    """The metadata JSON sidecar is written alongside the ref."""
    snap = SnapshotStore(repo).take(task_id="T-meta", label="before tool call")

    sidecar = repo / ".git" / "bernstein" / "snapshots" / f"{snap.snapshot_id}.json"
    assert sidecar.exists()
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    assert payload["task_id"] == "T-meta"
    assert payload["label"] == "before tool call"
    assert payload["tree_sha"] == snap.tree_sha


def test_take_rejects_invalid_task_id(repo: Path) -> None:
    """Task IDs with shell-meta or refspec-forbidden chars are rejected."""
    store = SnapshotStore(repo)
    with pytest.raises(SnapshotError):
        store.take(task_id="bad id with spaces")


def test_take_emits_unique_ids_for_back_to_back_calls(repo: Path) -> None:
    """Two captures in quick succession must not collide on the ref name."""
    store = SnapshotStore(repo)
    a = store.take(task_id="T-x", tool_call_id="tc-1")
    b = store.take(task_id="T-x", tool_call_id="tc-2")
    assert a.snapshot_id != b.snapshot_id
    assert a.ref != b.ref


# ---------------------------------------------------------------------------
# list()
# ---------------------------------------------------------------------------


def test_list_returns_newest_first(repo: Path) -> None:
    """``list()`` orders by ts_ns descending."""
    store = SnapshotStore(repo)
    first = store.take(task_id="T-a")
    second = store.take(task_id="T-a")

    snaps = store.list()
    assert [s.snapshot_id for s in snaps[:2]] == [second.snapshot_id, first.snapshot_id]


def test_list_filters_by_task_id(repo: Path) -> None:
    """A task filter excludes snapshots from other tasks."""
    store = SnapshotStore(repo)
    keep = store.take(task_id="T-keep")
    store.take(task_id="T-other")

    snaps = store.list(task_id="T-keep")
    assert [s.snapshot_id for s in snaps] == [keep.snapshot_id]


def test_list_respects_limit(repo: Path) -> None:
    """``limit`` truncates the result set without changing the order."""
    store = SnapshotStore(repo)
    for _ in range(3):
        store.take(task_id="T-lim")
    snaps = store.list(limit=2)
    assert len(snaps) == 2


def test_list_synthesises_record_when_metadata_missing(repo: Path) -> None:
    """A ref without its sidecar still appears in the list."""
    store = SnapshotStore(repo)
    snap = store.take(task_id="T-orphan")
    sidecar = repo / ".git" / "bernstein" / "snapshots" / f"{snap.snapshot_id}.json"
    sidecar.unlink()

    snaps = store.list()
    assert any(s.snapshot_id == snap.snapshot_id for s in snaps)


# ---------------------------------------------------------------------------
# undo()
# ---------------------------------------------------------------------------


def test_undo_restores_work_tree(repo: Path) -> None:
    """undo replaces every file with the snapshot's tree contents."""
    (repo / "later.txt").write_text("before\n", encoding="utf-8")
    store = SnapshotStore(repo)
    snap = store.take(task_id="T-u")

    # Mutate the work tree after the snapshot.
    (repo / "later.txt").write_text("after\n", encoding="utf-8")
    (repo / "extra.txt").write_text("appears\n", encoding="utf-8")

    # Stage and commit so the work tree is clean before undo (the
    # guardrail refuses to clobber uncommitted local changes).
    _git("add", "-A", cwd=repo)
    _git("commit", "-q", "-m", "later", cwd=repo)

    store.undo(snap.snapshot_id)

    assert (repo / "later.txt").read_text(encoding="utf-8") == "before\n"
    assert not (repo / "extra.txt").exists()


def test_undo_refuses_dirty_work_tree(repo: Path) -> None:
    """Without --force / allow_dirty we refuse to clobber uncommitted edits."""
    store = SnapshotStore(repo)
    snap = store.take(task_id="T-dirty")
    (repo / "uncommitted.txt").write_text("local\n", encoding="utf-8")

    with pytest.raises(SnapshotError, match="uncommitted"):
        store.undo(snap.snapshot_id)


def test_undo_allow_dirty_overrides_guardrail(repo: Path) -> None:
    """allow_dirty=True bypasses the dirty-work-tree refusal.

    The guardrail in :meth:`SnapshotStore.undo` checks
    ``git status --porcelain``; when ``allow_dirty=True`` we skip the
    check and let ``read-tree --reset -u`` proceed. The tracked file's
    contents must be replaced by what the snapshot captured.
    """
    store = SnapshotStore(repo)
    # README.md is already tracked by the repo fixture.
    snap = store.take(task_id="T-force")
    (repo / "README.md").write_text("modified locally\n", encoding="utf-8")

    store.undo(snap.snapshot_id, allow_dirty=True)
    # The snapshot captured README.md as "hello\n", so the dirty
    # tracked edit is gone.
    assert (repo / "README.md").read_text(encoding="utf-8") == "hello\n"


def test_undo_missing_snapshot_raises(repo: Path) -> None:
    """A non-existent snapshot ID surfaces as SnapshotError."""
    with pytest.raises(SnapshotError, match="not found"):
        SnapshotStore(repo).undo("does-not-exist")


# ---------------------------------------------------------------------------
# diff()
# ---------------------------------------------------------------------------


def test_diff_between_snapshots(repo: Path) -> None:
    """diff returns ``git diff --stat`` between two snapshot trees."""
    store = SnapshotStore(repo)
    first = store.take(task_id="T-d")

    (repo / "added.txt").write_text("new\n", encoding="utf-8")
    second = store.take(task_id="T-d")

    output = store.diff(first.snapshot_id, second.snapshot_id)
    assert "added.txt" in output


def test_diff_missing_id_raises(repo: Path) -> None:
    """diff raises when either ID does not exist."""
    store = SnapshotStore(repo)
    snap = store.take(task_id="T-x")
    with pytest.raises(SnapshotError):
        store.diff(snap.snapshot_id, "nope")


# ---------------------------------------------------------------------------
# delete() / gc()
# ---------------------------------------------------------------------------


def test_delete_removes_ref_and_sidecar(repo: Path) -> None:
    """delete clears both the ref and the metadata file."""
    store = SnapshotStore(repo)
    snap = store.take(task_id="T-del")
    sidecar = repo / ".git" / "bernstein" / "snapshots" / f"{snap.snapshot_id}.json"

    assert store.delete(snap.snapshot_id) is True
    assert store.get(snap.snapshot_id) is None
    assert not sidecar.exists()


def test_delete_missing_is_idempotent(repo: Path) -> None:
    """Deleting a non-existent snapshot returns False rather than raising."""
    assert SnapshotStore(repo).delete("nonexistent-1234") is False


def test_gc_drops_old_snapshots(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """gc removes snapshots whose ts_ns is older than the window."""
    store = SnapshotStore(repo)
    old = store.take(task_id="T-gc")
    young = store.take(task_id="T-gc")

    # Forcibly age the first snapshot by rewriting its sidecar.
    sidecar = repo / ".git" / "bernstein" / "snapshots" / f"{old.snapshot_id}.json"
    payload = json.loads(sidecar.read_text(encoding="utf-8"))
    payload["ts_ns"] = 1  # Jan 1 1970.
    sidecar.write_text(json.dumps(payload), encoding="utf-8")

    removed = store.gc(older_than_days=1)
    assert old.snapshot_id in removed
    assert young.snapshot_id not in removed


# ---------------------------------------------------------------------------
# Stacked branches
# ---------------------------------------------------------------------------


def test_stack_push_records_first_entry(repo: Path) -> None:
    """The first stack entry has no parent and ends up at index 1."""
    _git("checkout", "-q", "-b", "agent/run-1", cwd=repo)
    entry = stack_push(repo, task_id="T-stack", branch="agent/run-1")

    assert entry.index == 1
    assert entry.parent_branch is None
    assert entry.ref.startswith(STACK_REF_PREFIX)


def test_stack_push_chains_subsequent_entries(repo: Path) -> None:
    """The second entry's parent is the first entry's branch."""
    _git("checkout", "-q", "-b", "agent/run-1", cwd=repo)
    stack_push(repo, task_id="T-stack2", branch="agent/run-1")
    _git("checkout", "-q", "-b", "agent/run-2", cwd=repo)
    second = stack_push(repo, task_id="T-stack2", branch="agent/run-2")

    assert second.index == 2
    assert second.parent_branch == "agent/run-1"


def test_stack_list_orders_by_index(repo: Path) -> None:
    """stack_list returns entries in ascending index order."""
    _git("checkout", "-q", "-b", "agent/run-a", cwd=repo)
    stack_push(repo, task_id="T-order", branch="agent/run-a")
    _git("checkout", "-q", "-b", "agent/run-b", cwd=repo)
    stack_push(repo, task_id="T-order", branch="agent/run-b")

    entries = stack_list(repo, task_id="T-order")
    assert [e.index for e in entries] == [1, 2]
    assert [e.branch for e in entries] == ["agent/run-a", "agent/run-b"]


def test_stack_push_rejects_missing_branch(repo: Path) -> None:
    """stack_push fails fast if the branch does not exist locally."""
    with pytest.raises(SnapshotError, match="does not exist"):
        stack_push(repo, task_id="T-miss", branch="agent/ghost")


def test_stack_clear_removes_every_entry(repo: Path) -> None:
    """stack_clear empties the namespace for the task."""
    _git("checkout", "-q", "-b", "agent/run-c", cwd=repo)
    stack_push(repo, task_id="T-clear", branch="agent/run-c")
    _git("checkout", "-q", "-b", "agent/run-d", cwd=repo)
    stack_push(repo, task_id="T-clear", branch="agent/run-d")

    removed = stack_clear(repo, task_id="T-clear")
    assert removed == 2
    assert stack_list(repo, task_id="T-clear") == []


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_invalid_snapshot_id_rejected_by_get(repo: Path) -> None:
    """get() validates the snapshot_id format before touching git."""
    with pytest.raises(SnapshotError):
        SnapshotStore(repo).get("../etc/passwd")


def test_invalid_task_id_rejected_by_stack(repo: Path) -> None:
    """stack helpers reject task IDs that would escape the ref namespace."""
    with pytest.raises(SnapshotError):
        stack_list(repo, task_id="bad/../id")


# ---------------------------------------------------------------------------
# Snapshot dataclass
# ---------------------------------------------------------------------------


def test_snapshot_to_dict_round_trips() -> None:
    """Snapshot.to_dict produces a JSON-serialisable dict of every field."""
    snap = Snapshot(
        snapshot_id="abc",
        tree_sha="deadbeef",
        ref="refs/bernstein/snapshots/abc",
        ts_ns=42,
        task_id="T-1",
        tool_call_id="tc-1",
        agent_id="agent:x",
        label="lbl",
        parent_snapshot=None,
    )
    encoded = json.dumps(snap.to_dict(), sort_keys=True)
    decoded = json.loads(encoded)
    assert decoded["snapshot_id"] == "abc"
    assert decoded["ts_ns"] == 42
    assert decoded["label"] == "lbl"


# ---------------------------------------------------------------------------
# Every git the snapshot path runs is bounded
# ---------------------------------------------------------------------------
#
# ``_write_tree_snapshot`` needs a custom ``GIT_INDEX_FILE``, and wanting a
# custom environment used to mean dropping out of ``run_git`` to a bare
# ``subprocess.run`` - which took the 30s timeout with it. ``git add -A`` walks
# the whole work tree and any of these blocks on ``index.lock``, so an
# unbounded call is a snapshot that never returns, on the orchestrator's
# checkpoint path.


def test_no_raw_subprocess_git_in_the_snapshot_path() -> None:
    """The wrapper is where the timeout lives, so nothing may bypass it."""
    import ast
    import inspect

    from bernstein.core.git import snapshot as snapshot_module

    tree = ast.parse(inspect.getsource(snapshot_module))
    offenders = [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "run"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "subprocess"
    ]
    assert offenders == [], f"raw subprocess.run at lines {offenders}; use run_git so the call is bounded"


def test_run_git_passes_a_custom_environment_through(repo: Path) -> None:
    """The reason the raw calls existed - honoured by the wrapper instead.

    Asserted through observable behaviour: with ``GIT_INDEX_FILE`` pointed at
    a throwaway path, ``git add`` populates *that* file and leaves the repo's
    real index alone.
    """
    import os

    from bernstein.core.git.git_basic import run_git

    (repo / "scratch.txt").write_text("x", encoding="utf-8")
    tmp_index = repo / ".git" / "test-index"

    result = run_git(["add", "-A", "--", "."], repo, env=os.environ | {"GIT_INDEX_FILE": str(tmp_index)})

    assert result.ok, result.stderr
    assert tmp_index.exists(), "the custom GIT_INDEX_FILE was not used"
    assert _git("status", "--porcelain", cwd=repo).strip(), "the real index should still show the file as untracked"


def test_a_git_that_overruns_surfaces_as_snapshot_error(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A bound that escapes as ``TimeoutExpired`` is not an improvement.

    Every caller of this module guards on ``SnapshotError``; a hang swapped
    for an exception type nobody catches is a different crash, not a fix.
    """
    from bernstein.core.git import snapshot as snapshot_module

    # Built before the clock is sabotaged: construction runs git too, and the
    # point here is the snapshot, not the constructor.
    store = SnapshotStore(repo)

    def hang(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd=["git", "add", "-A"], timeout=300)

    monkeypatch.setattr(snapshot_module, "run_git", hang)

    with pytest.raises(SnapshotError, match="exceeded 300"):
        store.take(task_id="t-1")


def test_every_git_in_the_module_is_bounded_the_same_way(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Not only the calls inside one function's ``try``.

    The first draft guarded ``_write_tree_snapshot`` alone, and a timeout in
    ``_ensure_repo`` - which runs on every ``SnapshotStore`` construction -
    still escaped as ``TimeoutExpired``. Routing the whole module through one
    wrapper is what makes the bound uniform.
    """
    from bernstein.core.git import snapshot as snapshot_module

    def hang(*args: object, **kwargs: object) -> None:
        raise subprocess.TimeoutExpired(cmd=["git", "rev-parse"], timeout=30)

    monkeypatch.setattr(snapshot_module, "run_git", hang)

    with pytest.raises(SnapshotError, match="exceeded 30"):
        SnapshotStore(repo)


def test_each_git_call_gets_the_timeout_it_was_meant_to_have(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The 300s on ``git add`` is the real decision in this module.

    It is the difference between "a large repository is slow" and "a large
    repository's snapshots fail", and nothing held it in place: the overrun
    test matches ``"exceeded 300"``, but that 300 comes from the fake
    ``TimeoutExpired``, which raises identically for every git the code runs.
    Swap the two constants at the call sites and every other test here still
    passes.

    This captures the ``timeout=`` each call actually receives.
    """
    from bernstein.core.git import snapshot as snapshot_module

    seen: list[tuple[str, int]] = []
    real_run_git = snapshot_module.run_git

    def record(args: list[str], cwd: Path, **kwargs: object) -> GitResult:
        timeout = kwargs.get("timeout")
        seen.append((args[0], int(timeout) if isinstance(timeout, int) else -1))
        return real_run_git(args, cwd, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(snapshot_module, "run_git", record)
    (repo / "a.txt").write_text("x", encoding="utf-8")
    SnapshotStore(repo).take(task_id="t-1")

    by_command = dict(seen)
    assert by_command["add"] == snapshot_module._GIT_ADD_TIMEOUT_S
    assert by_command["add"] == 300, "the add bound is the decision this module makes; pin it"
    assert by_command["write-tree"] == snapshot_module._GIT_TIMEOUT_S
    assert by_command["write-tree"] == 30


def test_a_killed_git_add_does_not_orphan_its_lock_file(repo: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``subprocess.run``'s timeout path kills with SIGKILL, which git cannot catch.

    Git's lockfile cleanup runs on SIGINT/SIGTERM and at exit, so a ``git
    add`` killed at the bound leaves ``<tmp_index>.lock`` behind. That exit
    did not exist before this module had a timeout at all, so the cleanup
    block had never needed to cover it.
    """
    from bernstein.core.git import snapshot as snapshot_module

    created: list[Path] = []
    real_run_git = snapshot_module.run_git

    def leave_a_lock(args: list[str], cwd: Path, **kwargs: object) -> GitResult:
        if args[0] == "add":
            index = Path(str(kwargs.get("env", {}).get("GIT_INDEX_FILE", "")))  # type: ignore[union-attr]
            lock = index.with_name(index.name + ".lock")
            lock.write_text("", encoding="utf-8")
            created.append(lock)
            raise subprocess.TimeoutExpired(cmd=["git", "add", "-A"], timeout=300)
        return real_run_git(args, cwd, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(snapshot_module, "run_git", leave_a_lock)

    with pytest.raises(SnapshotError):
        SnapshotStore(repo).take(task_id="t-1")

    assert created, "the fake never ran - the test proves nothing"
    assert not created[0].exists(), f"orphaned lock file left behind: {created[0]}"
