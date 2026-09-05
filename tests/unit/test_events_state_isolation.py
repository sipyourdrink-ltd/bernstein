"""Per-project trigger state isolation under concurrency (#2548).

Covers the acceptance criterion: counter and expectation state lives per project
under ``.sdd/runtime/triggers/``, and two concurrent workers evaluating rules do
not interleave state (tested with concurrent workers).
"""

from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from bernstein.core.events import state as state_mod
from bernstein.core.events.state import TriggerStateCorruptError, TriggerStateStore


def test_state_lives_under_runtime_triggers(tmp_path: Path) -> None:
    root = tmp_path / ".sdd" / "runtime" / "triggers"
    store = TriggerStateStore(root)
    store.increment("k")
    assert store.counters_path == root / "counters.json"
    assert store.counters_path.exists()


def test_concurrent_increments_do_not_lose_updates(tmp_path: Path) -> None:
    root = tmp_path / ".sdd" / "runtime" / "triggers"
    # Two independent store handles standing in for two workers sharing a root.
    worker_a = TriggerStateStore(root)
    worker_b = TriggerStateStore(root)
    per_worker = 200

    def hammer(store: TriggerStateStore) -> None:
        for _ in range(per_worker):
            store.increment("gate.result")

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(hammer, worker_a), pool.submit(hammer, worker_b)]
        for future in futures:
            future.result()

    # No lost updates: exactly 2 * per_worker increments landed.
    assert TriggerStateStore(root).get_counter("gate.result") == 2 * per_worker


def test_expectations_roundtrip(tmp_path: Path) -> None:
    root = tmp_path / ".sdd" / "runtime" / "triggers"
    store = TriggerStateStore(root)
    store.open_expectation("run_1", {"expect": "run.completed", "after_hmac": "abc"})
    assert store.open_expectations() == {"run_1": {"expect": "run.completed", "after_hmac": "abc"}}
    closed = store.close_expectation("run_1")
    assert closed == {"expect": "run.completed", "after_hmac": "abc"}
    assert store.open_expectations() == {}


def test_corrupt_state_file_is_preserved_not_overwritten(tmp_path: Path) -> None:
    root = tmp_path / ".sdd" / "runtime" / "triggers"
    root.mkdir(parents=True)
    counters = root / "counters.json"
    counters.write_text("{not valid json", encoding="utf-8")
    store = TriggerStateStore(root)

    with pytest.raises(TriggerStateCorruptError):
        store.increment("gate.result")

    # Original bytes preserved for inspection; not silently emptied and rewritten.
    corrupt = root / "counters.json.corrupt"
    assert corrupt.exists()
    assert corrupt.read_text(encoding="utf-8") == "{not valid json"
    assert not counters.exists()


def test_non_object_state_file_raises(tmp_path: Path) -> None:
    root = tmp_path / ".sdd" / "runtime" / "triggers"
    root.mkdir(parents=True)
    (root / "expectations.json").write_text("[1, 2, 3]", encoding="utf-8")
    store = TriggerStateStore(root)

    with pytest.raises(TriggerStateCorruptError):
        store.open_expectations()
    assert (root / "expectations.json.corrupt").exists()


def test_missing_lock_primitive_refuses_silent_no_op(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # With neither flock nor msvcrt available the store must raise rather than
    # yield an unsynchronised no-op (the old Windows behaviour).
    monkeypatch.setattr(state_mod, "fcntl", None, raising=False)
    monkeypatch.setattr(state_mod, "msvcrt", None, raising=False)
    store = TriggerStateStore(tmp_path / "triggers")

    with pytest.raises(RuntimeError):
        store.increment("gate.result")


def test_windows_lock_path_uses_msvcrt(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Simulate the Windows branch: fcntl absent, msvcrt present. The lock must be
    # taken (LK_LOCK) and released (LK_UNLCK), not skipped.
    modes: list[int] = []

    class _FakeMsvcrt:
        LK_LOCK = 1
        LK_UNLCK = 2

        def locking(self, _fileno: int, mode: int, _nbytes: int) -> None:
            modes.append(mode)

    monkeypatch.setattr(state_mod, "fcntl", None, raising=False)
    monkeypatch.setattr(state_mod, "msvcrt", _FakeMsvcrt(), raising=False)
    store = TriggerStateStore(tmp_path / "triggers")

    assert store.increment("gate.result") == 1
    assert modes == [_FakeMsvcrt.LK_LOCK, _FakeMsvcrt.LK_UNLCK]


def _fsync_spy(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    """Record every ``os.fsync`` the write path performs."""
    seen: list[int] = []
    real = os.fsync

    def spy(fd: int) -> None:
        seen.append(fd)
        real(fd)

    monkeypatch.setattr(os, "fsync", spy)
    return seen


def test_a_counter_write_is_fsynced_before_it_is_published(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A rename that outlives its own bytes is what _load quarantines.

    The write path used to rename the temporary into place without ever
    calling fsync, so a power loss could leave a durable directory entry
    pointing at a zero-length file. The next boot reads that, raises
    TriggerStateCorruptError and quarantines the state.
    """
    store = TriggerStateStore(tmp_path / ".sdd" / "runtime" / "triggers")
    seen = _fsync_spy(monkeypatch)
    store.increment("k")
    assert seen, "counters were published without being fsynced"


def test_an_expectation_write_is_fsynced_before_it_is_published(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = TriggerStateStore(tmp_path / ".sdd" / "runtime" / "triggers")
    seen = _fsync_spy(monkeypatch)
    store.open_expectation("k", {"payload": 1})
    assert seen, "expectations were published without being fsynced"


def test_a_write_leaves_no_temporary_behind(tmp_path: Path) -> None:
    root = tmp_path / ".sdd" / "runtime" / "triggers"
    store = TriggerStateStore(root)
    store.increment("k")
    store.open_expectation("e", {"payload": 1})
    assert [p.name for p in root.iterdir() if ".tmp" in p.name] == []


def test_a_failed_write_leaves_the_previous_counters_readable(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The point of temp-and-rename: a failure loses the update, not the file."""
    root = tmp_path / ".sdd" / "runtime" / "triggers"
    store = TriggerStateStore(root)
    store.increment("k", amount=7)

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr("bernstein.core.persistence.atomic_write.os.replace", boom)
    with pytest.raises(OSError):
        store.increment("k")

    monkeypatch.undo()
    assert TriggerStateStore(root).get_counter("k") == 7
    assert [p.name for p in root.iterdir() if ".tmp" in p.name] == []
