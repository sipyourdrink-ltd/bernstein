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


def test_expired_lock_is_evicted(workdir: Path) -> None:
    mgr = FileLockManager(workdir)
    mgr.LOCK_TTL_SECONDS = 1  # type: ignore[assignment]
    mgr.acquire(["src/foo.py"], agent_id="a1", task_id="t1")
    # Backdating the lock's timestamp is cleaner than sleeping
    lock = mgr._locks["src/foo.py"]
    mgr._locks["src/foo.py"] = FileLock(
        file_path=lock.file_path,
        agent_id=lock.agent_id,
        task_id=lock.task_id,
        task_title=lock.task_title,
        locked_at=time.time() - 10,  # 10 s ago, past TTL of 1 s
    )
    assert not mgr.is_locked("src/foo.py")


def test_expired_lock_allows_reacquire(workdir: Path) -> None:
    mgr = FileLockManager(workdir)
    mgr.LOCK_TTL_SECONDS = 1  # type: ignore[assignment]
    mgr.acquire(["src/foo.py"], agent_id="a1", task_id="t1")
    lock = mgr._locks["src/foo.py"]
    mgr._locks["src/foo.py"] = FileLock(
        file_path=lock.file_path,
        agent_id=lock.agent_id,
        task_id=lock.task_id,
        task_title=lock.task_title,
        locked_at=time.time() - 10,
    )
    conflicts = mgr.acquire(["src/foo.py"], agent_id="a2", task_id="t2")
    assert conflicts == []


# ---------------------------------------------------------------------------
