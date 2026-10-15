# pyright: reportPrivateUsage=false
"""Tests for the FileLockManager file-level locking system."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

import pytest
from bernstein.core.file_locks import (
    FileLock,
    FileLockManager,
    get_concurrency_safe_tools,
    get_concurrency_unsafe_tools,
    get_tool_definition,
    partition_tools_by_concurrency,
)

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def workdir(tmp_path: Path) -> Path:
    return tmp_path


@pytest.fixture()
def mgr(workdir: Path) -> FileLockManager:
    return FileLockManager(workdir)


# ---------------------------------------------------------------------------
# acquire
# ---------------------------------------------------------------------------


def test_acquire_empty_files_is_noop(mgr: FileLockManager) -> None:
    conflicts = mgr.acquire([], agent_id="a1", task_id="t1")
    assert conflicts == []
    assert mgr.all_locks() == []


def test_acquire_returns_empty_on_success(mgr: FileLockManager) -> None:
    conflicts = mgr.acquire(["src/foo.py"], agent_id="a1", task_id="t1")
    assert conflicts == []


def test_acquire_locks_file(mgr: FileLockManager) -> None:
    mgr.acquire(["src/foo.py"], agent_id="a1", task_id="t1", task_title="Fix foo")
    locks = mgr.all_locks()
    assert len(locks) == 1
    assert locks[0].file_path == "src/foo.py"
    assert locks[0].agent_id == "a1"
    assert locks[0].task_id == "t1"
    assert locks[0].task_title == "Fix foo"


def test_acquire_conflict_returns_conflicting_files(mgr: FileLockManager) -> None:
    mgr.acquire(["src/foo.py", "src/bar.py"], agent_id="a1", task_id="t1")
    conflicts = mgr.acquire(["src/foo.py", "src/baz.py"], agent_id="a2", task_id="t2")
    assert conflicts == ["src/foo.py"]


def test_acquire_conflict_does_not_acquire_any_locks(mgr: FileLockManager) -> None:
    mgr.acquire(["src/foo.py"], agent_id="a1", task_id="t1")
    conflicts = mgr.acquire(["src/foo.py", "src/new.py"], agent_id="a2", task_id="t2")
    assert len(conflicts) == 1
    # src/new.py must NOT have been locked since we returned early
    assert not mgr.is_locked("src/new.py")


def test_acquire_same_agent_is_idempotent(mgr: FileLockManager) -> None:
    mgr.acquire(["src/foo.py"], agent_id="a1", task_id="t1")
    conflicts = mgr.acquire(["src/foo.py"], agent_id="a1", task_id="t1")
    assert conflicts == []
    assert len(mgr.all_locks()) == 1


def test_acquire_multiple_files(mgr: FileLockManager) -> None:
    files = ["a.py", "b.py", "c.py"]
    mgr.acquire(files, agent_id="a1", task_id="t1")
    assert {lock.file_path for lock in mgr.all_locks()} == set(files)


# ---------------------------------------------------------------------------
# release
# ---------------------------------------------------------------------------


def test_release_removes_agent_locks(mgr: FileLockManager) -> None:
    mgr.acquire(["src/foo.py", "src/bar.py"], agent_id="a1", task_id="t1")
    released = mgr.release("a1")
    assert set(released) == {"src/foo.py", "src/bar.py"}
    assert mgr.all_locks() == []


def test_release_only_removes_given_agents_locks(mgr: FileLockManager) -> None:
    mgr.acquire(["src/foo.py"], agent_id="a1", task_id="t1")
    mgr.acquire(["src/bar.py"], agent_id="a2", task_id="t2")
    mgr.release("a1")
    locks = mgr.all_locks()
    assert len(locks) == 1
    assert locks[0].agent_id == "a2"


def test_release_unknown_agent_returns_empty(mgr: FileLockManager) -> None:
    released = mgr.release("nonexistent")
    assert released == []


def test_release_enables_reacquire_by_other_agent(mgr: FileLockManager) -> None:
    mgr.acquire(["src/foo.py"], agent_id="a1", task_id="t1")
    mgr.release("a1")
    conflicts = mgr.acquire(["src/foo.py"], agent_id="a2", task_id="t2")
    assert conflicts == []


# ---------------------------------------------------------------------------
# release_all_except
# ---------------------------------------------------------------------------


def test_release_all_except_drops_locks_of_agents_outside_the_live_set(mgr: FileLockManager) -> None:
    mgr.acquire(["src/live.py"], agent_id="live", task_id="t1")
    mgr.acquire(["src/dead.py", "src/dead2.py"], agent_id="dead", task_id="t2")
    released = mgr.release_all_except({"live"})
    assert sorted(released) == ["src/dead.py", "src/dead2.py"]
    assert [lock.agent_id for lock in mgr.all_locks()] == ["live"]


def test_release_all_except_keeps_locks_when_every_holder_is_live(mgr: FileLockManager) -> None:
    mgr.acquire(["src/a.py"], agent_id="a1", task_id="t1")
    assert mgr.release_all_except({"a1", "a2"}) == []
    assert [lock.file_path for lock in mgr.all_locks()] == ["src/a.py"]


def test_release_all_except_persists_for_the_next_manager(workdir: Path, mgr: FileLockManager) -> None:
    mgr.acquire(["src/dead.py"], agent_id="dead", task_id="t1")
    mgr.release_all_except(set())
    assert FileLockManager(workdir).all_locks() == []


# ---------------------------------------------------------------------------
# Tool concurrency safety classification (T438)
# ---------------------------------------------------------------------------


class TestToolDefinition:
    def test_known_safe_tool(self) -> None:
        defn = get_tool_definition("read_file")
        assert defn.concurrency_safe is True
        assert defn.name == "read_file"

    def test_known_unsafe_tool(self) -> None:
        defn = get_tool_definition("bash")
        assert defn.concurrency_safe is False
        assert defn.name == "bash"

    def test_unknown_tool_defaults_unsafe(self) -> None:
        defn = get_tool_definition("some_weird_tool")
        assert defn.concurrency_safe is False
        assert defn.name == "some_weird_tool"

    def test_case_insensitive(self) -> None:
        assert get_tool_definition("Bash").concurrency_safe is False
        assert get_tool_definition("Read_File").concurrency_safe is True


class TestConcurrencyToolListing:
    def test_safe_tools_non_empty(self) -> None:
        safe = get_concurrency_safe_tools()
        assert len(safe) > 0
        assert "read_file" in safe

    def test_unsafe_tools_non_empty(self) -> None:
        unsafe = get_concurrency_unsafe_tools()
        assert len(unsafe) > 0
        assert "bash" in unsafe

    def test_safe_and_unsafe_disjoint(self) -> None:
        safe = set(get_concurrency_safe_tools())
        unsafe = set(get_concurrency_unsafe_tools())
        assert not (safe & unsafe)


class TestPartitionToolsByConcurrency:
    def test_empty_list(self) -> None:
        safe, unsafe = partition_tools_by_concurrency([])
        assert safe == []
        assert unsafe == []

    def test_mixed_tools(self) -> None:
        safe, unsafe = partition_tools_by_concurrency(["read_file", "bash", "grep", "write_file"])
        assert set(safe) == {"read_file", "grep"}
        assert set(unsafe) == {"bash", "write_file"}

    def test_all_safe(self) -> None:
        safe, unsafe = partition_tools_by_concurrency(["read_file", "list_directory"])
        assert unsafe == []
        assert set(safe) == {"read_file", "list_directory"}

    def test_all_unsafe(self) -> None:
        safe, unsafe = partition_tools_by_concurrency(["bash", "write_file"])
        assert safe == []
        assert set(unsafe) == {"bash", "write_file"}

    def test_unknown_tool_treated_as_unsafe(self) -> None:
        safe, unsafe = partition_tools_by_concurrency(["unknown_tool_xyz"])
        assert safe == []
        assert unsafe == ["unknown_tool_xyz"]


# ---------------------------------------------------------------------------
# check_conflicts
# ---------------------------------------------------------------------------


def test_check_conflicts_empty_when_no_locks(mgr: FileLockManager) -> None:
    assert mgr.check_conflicts(["src/foo.py"]) == []


def test_check_conflicts_returns_pairs(mgr: FileLockManager) -> None:
    mgr.acquire(["src/foo.py"], agent_id="a1", task_id="t1")
    result = mgr.check_conflicts(["src/foo.py"])
    assert len(result) == 1
    path, lock = result[0]
    assert path == "src/foo.py"
    assert isinstance(lock, FileLock)
    assert lock.agent_id == "a1"


def test_check_conflicts_does_not_modify_locks(mgr: FileLockManager) -> None:
    mgr.acquire(["src/foo.py"], agent_id="a1", task_id="t1")
    mgr.check_conflicts(["src/foo.py"])
    assert mgr.is_locked("src/foo.py")


# ---------------------------------------------------------------------------
# is_locked / locks_for_agent
# ---------------------------------------------------------------------------


def test_is_locked_false_initially(mgr: FileLockManager) -> None:
    assert not mgr.is_locked("src/foo.py")


def test_is_locked_true_after_acquire(mgr: FileLockManager) -> None:
    mgr.acquire(["src/foo.py"], agent_id="a1", task_id="t1")
    assert mgr.is_locked("src/foo.py")


def test_locks_for_agent(mgr: FileLockManager) -> None:
    mgr.acquire(["a.py", "b.py"], agent_id="a1", task_id="t1")
    mgr.acquire(["c.py"], agent_id="a2", task_id="t2")
    a1_locks = mgr.locks_for_agent("a1")
    assert len(a1_locks) == 2
    assert all(lock.agent_id == "a1" for lock in a1_locks)


# ---------------------------------------------------------------------------
# Persistence
# ---------------------------------------------------------------------------


def test_locks_persisted_to_disk(workdir: Path) -> None:
    mgr = FileLockManager(workdir)
    mgr.acquire(["src/foo.py"], agent_id="a1", task_id="t1", task_title="T1")
    lock_path = workdir / ".sdd" / "runtime" / "file_locks.json"
    assert lock_path.exists()
    data = json.loads(lock_path.read_text())
    assert len(data) == 1
    assert data[0]["file_path"] == "src/foo.py"
    assert data[0]["agent_id"] == "a1"


def test_locks_reloaded_on_new_instance(workdir: Path) -> None:
    mgr1 = FileLockManager(workdir)
    mgr1.acquire(["src/foo.py"], agent_id="a1", task_id="t1")

    mgr2 = FileLockManager(workdir)
    assert mgr2.is_locked("src/foo.py")
    locks = mgr2.all_locks()
    assert locks[0].agent_id == "a1"


def test_release_removes_from_disk(workdir: Path) -> None:
    mgr1 = FileLockManager(workdir)
    mgr1.acquire(["src/foo.py"], agent_id="a1", task_id="t1")
    mgr1.release("a1")

    mgr2 = FileLockManager(workdir)
    assert not mgr2.is_locked("src/foo.py")


def test_corrupt_lock_file_is_tolerated(workdir: Path) -> None:
    lock_path = workdir / ".sdd" / "runtime" / "file_locks.json"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text("not valid json")
    mgr = FileLockManager(workdir)  # should not raise
    assert mgr.all_locks() == []


# ---------------------------------------------------------------------------
# TTL / expiry
# ---------------------------------------------------------------------------


def _backdate_persisted_lock(workdir: Path, file_path: str, age_seconds: float) -> None:
    """Rewrite the persisted lock JSON so *file_path* looks *age_seconds* old.

    Used by TTL tests: since every public method reloads state from disk under
    the cross-process guard, merely mutating ``mgr._locks`` in memory is no
    longer enough - the next call would re-read the fresh timestamp from disk.
    """
    lock_path = workdir / ".sdd" / "runtime" / "file_locks.json"
    data = json.loads(lock_path.read_text())
    for entry in data:
        if entry["file_path"] == file_path:
            entry["locked_at"] = time.time() - age_seconds
    lock_path.write_text(json.dumps(data))


def test_expired_lock_is_evicted(workdir: Path) -> None:
    mgr = FileLockManager(workdir)
    mgr.LOCK_TTL_SECONDS = 1  # type: ignore[assignment]
    mgr.acquire(["src/foo.py"], agent_id="a1", task_id="t1")
    _backdate_persisted_lock(workdir, "src/foo.py", age_seconds=10)
    assert not mgr.is_locked("src/foo.py")


def test_expired_lock_allows_reacquire(workdir: Path) -> None:
    mgr = FileLockManager(workdir)
    mgr.LOCK_TTL_SECONDS = 1  # type: ignore[assignment]
    mgr.acquire(["src/foo.py"], agent_id="a1", task_id="t1")
    _backdate_persisted_lock(workdir, "src/foo.py", age_seconds=10)
    conflicts = mgr.acquire(["src/foo.py"], agent_id="a2", task_id="t2")
    assert conflicts == []


# ---------------------------------------------------------------------------
# Cross-process file locking (audit-077)
# ---------------------------------------------------------------------------


_WORKER_SRC = """
import json
import os
import sys
import time
from pathlib import Path

from bernstein.core.persistence.file_locks import _cross_process_lock

lock_path = Path(sys.argv[1])
trace_path = Path(sys.argv[2])
worker_id = sys.argv[3]
hold_seconds = float(sys.argv[4])

with _cross_process_lock(lock_path):
    enter = time.time()
    time.sleep(hold_seconds)
    leave = time.time()
    with trace_path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps({"worker": worker_id, "pid": os.getpid(), "enter": enter, "leave": leave}) + "\\n")
"""


def test_cross_process_flock_serializes_workers(tmp_path: Path) -> None:
    """Two subprocesses racing on the same OS lock must NOT overlap.

    Regression guard for audit-077: the old implementation used only a
    ``threading.Lock`` so two Python processes happily entered the critical
    section simultaneously. With ``fcntl.flock`` / ``msvcrt.locking`` around
    every load-modify-save cycle, their [enter, leave] intervals are
    guaranteed to be disjoint.
    """
    import subprocess
    import sys as _sys

    lock_path = tmp_path / "cross.lock"
    trace_path = tmp_path / "trace.jsonl"
    worker_script = tmp_path / "worker.py"
    worker_script.write_text(_WORKER_SRC, encoding="utf-8")

    hold = 0.3
    procs = [
        subprocess.Popen(
            [_sys.executable, str(worker_script), str(lock_path), str(trace_path), f"w{i}", str(hold)],
            cwd=str(tmp_path),
        )
        for i in range(2)
    ]
    for p in procs:
        p.wait(timeout=30)
        assert p.returncode == 0, f"worker exited with {p.returncode}"

    entries = [json.loads(line) for line in trace_path.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(entries) == 2, f"expected 2 trace entries, got {entries}"
    assert entries[0]["pid"] != entries[1]["pid"]

    entries.sort(key=lambda e: e["enter"])
    epsilon = 0.01
    assert entries[1]["enter"] + epsilon >= entries[0]["leave"], (
        f"workers overlapped: first={entries[0]}, second={entries[1]}"
    )
    total = entries[1]["leave"] - entries[0]["enter"]
    assert total >= 2 * hold - epsilon, f"workers were not serialized: total={total:.3f}s"


def test_cross_process_file_lock_manager_no_lost_writes(tmp_path: Path) -> None:
    """Two FileLockManager processes writing concurrently must not lose locks.

    Without cross-process locking, the last-writer-wins ``json.dump`` in
    ``_save`` silently drops acquisitions made by the peer process between its
    load and our save. Running many acquires from two subprocesses and
    asserting the final on-disk state contains them all is the tightest
    available proof.
    """
    import subprocess
    import sys as _sys

    script_src = """
import sys
from pathlib import Path
from bernstein.core.persistence.file_locks import FileLockManager

workdir = Path(sys.argv[1])
worker_id = sys.argv[2]
count = int(sys.argv[3])

mgr = FileLockManager(workdir)
for i in range(count):
    mgr.acquire([f"{worker_id}/file_{i}.py"], agent_id=f"{worker_id}-agent-{i}", task_id=f"{worker_id}-t{i}")
"""
    script_path = tmp_path / "mgr_worker.py"
    script_path.write_text(script_src, encoding="utf-8")

    n_per_worker = 20
    procs = [
        subprocess.Popen(
            [_sys.executable, str(script_path), str(tmp_path), f"w{i}", str(n_per_worker)],
            cwd=str(tmp_path),
        )
        for i in range(2)
    ]
    for p in procs:
        p.wait(timeout=60)
        assert p.returncode == 0

    final = FileLockManager(tmp_path)
    paths = {lock.file_path for lock in final.all_locks()}
    expected = {f"w{w}/file_{i}.py" for w in range(2) for i in range(n_per_worker)}
    missing = expected - paths
    assert not missing, f"cross-process write lost {len(missing)} locks: {sorted(missing)[:5]}..."
