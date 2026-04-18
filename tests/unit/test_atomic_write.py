"""Unit tests for :mod:`bernstein.core.persistence.atomic_write` (audit-076).

These tests cover the crash-safe persistence contract:

* Repeated writes to the same path never produce a corrupt or empty
  reader view — the file is either old-content or new-content at any
  observation point.
* A simulated mid-write crash (exception between temp creation and
  ``os.replace``) cleans up the stray ``.tmp.*`` file and leaves the
  pre-existing target intact.
* Concurrent writers never expose a partial payload to readers —
  every successful read returns a fully-parseable JSON document.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

from bernstein.core.persistence.atomic_write import (
    write_atomic_bytes,
    write_atomic_json,
    write_atomic_text,
)


def test_atomic_write_bytes_creates_file(tmp_path: Path) -> None:
    target = tmp_path / "runtime" / "payload.bin"
    write_atomic_bytes(target, b"hello-world")
    assert target.read_bytes() == b"hello-world"


def test_atomic_write_text_encodes_utf8(tmp_path: Path) -> None:
    target = tmp_path / "runtime" / "payload.txt"
    write_atomic_text(target, "héllo")
    assert target.read_text(encoding="utf-8") == "héllo"


def test_atomic_write_json_round_trip(tmp_path: Path) -> None:
    target = tmp_path / "runtime" / "state.json"
    payload = {"a": 1, "b": [1, 2, 3], "c": None}
    write_atomic_json(target, payload)
    assert json.loads(target.read_text(encoding="utf-8")) == payload


def test_atomic_write_json_overwrites_existing(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    write_atomic_json(target, {"v": 1})
    write_atomic_json(target, {"v": 2})
    assert json.loads(target.read_text(encoding="utf-8")) == {"v": 2}


def test_atomic_write_leaves_no_tmp_files_on_success(tmp_path: Path) -> None:
    """After successful write there are no stray ``.tmp.*`` siblings."""
    target = tmp_path / "state.json"
    write_atomic_json(target, {"v": 1})
    write_atomic_json(target, {"v": 2})
    leftovers = [p for p in tmp_path.iterdir() if ".tmp." in p.name]
    assert leftovers == []


def test_atomic_write_repeated_writes_never_corrupt(tmp_path: Path) -> None:
    """After many repeated writes the file always parses cleanly."""
    target = tmp_path / "state.json"
    for i in range(100):
        write_atomic_json(target, {"i": i, "s": "x" * i})
    data = json.loads(target.read_text(encoding="utf-8"))
    assert data["i"] == 99
    assert data["s"] == "x" * 99


def test_atomic_write_crash_before_replace_preserves_old(tmp_path: Path) -> None:
    """A mid-write crash must leave the pre-existing target intact."""
    target = tmp_path / "state.json"
    write_atomic_json(target, {"v": "old"})

    real_replace = os.replace

    def boom(*_args: object, **_kwargs: object) -> None:
        raise OSError("simulated crash before rename")

    with patch("bernstein.core.persistence.atomic_write.os.replace", side_effect=boom):
        with pytest.raises(OSError, match="simulated crash"):
            write_atomic_json(target, {"v": "new"})

    # Old content survives the failed write.
    assert json.loads(target.read_text(encoding="utf-8")) == {"v": "old"}
    # Temp file was cleaned up.
    leftovers = [p for p in tmp_path.iterdir() if ".tmp." in p.name]
    assert leftovers == []
    # sanity: real os.replace is unchanged
    assert os.replace is real_replace


def test_atomic_write_crash_during_write_cleans_tmp(tmp_path: Path) -> None:
    """If writing to the temp file itself fails, no target is created and no tmp survives."""
    target = tmp_path / "state.json"

    original_fsync = os.fsync
    fsync_calls: list[int] = []

    def failing_fsync(fd: int) -> None:
        fsync_calls.append(fd)
        # Fail on the file fsync (first call), before the directory fsync.
        raise OSError("simulated fsync failure")

    with patch("bernstein.core.persistence.atomic_write.os.fsync", side_effect=failing_fsync):
        with pytest.raises(OSError, match="simulated fsync failure"):
            write_atomic_json(target, {"v": "new"})

    assert not target.exists()
    leftovers = [p for p in tmp_path.iterdir() if ".tmp." in p.name]
    assert leftovers == []
    assert fsync_calls, "fsync should have been attempted"
    # sanity: real os.fsync is unchanged
    assert os.fsync is original_fsync


def test_atomic_write_tmp_path_unique_per_call(tmp_path: Path) -> None:
    """Two concurrent writers must use distinct temp names to avoid collision."""
    target = tmp_path / "state.json"

    seen: list[str] = []
    real_fsync = os.fsync

    def observe_fsync(fd: int) -> None:
        # Snapshot sibling .tmp.* entries each time a file fd is fsynced.
        siblings = [p.name for p in tmp_path.iterdir() if ".tmp." in p.name]
        seen.extend(siblings)
        real_fsync(fd)

    # Perform two writes serially, captured through fsync.
    with patch("bernstein.core.persistence.atomic_write.os.fsync", side_effect=observe_fsync):
        write_atomic_json(target, {"v": 1})
        write_atomic_json(target, {"v": 2})

    # Each call creates a uniquely-named temp sibling.
    unique = {name for name in seen if name != "state.json"}
    assert len(unique) >= 2


def test_atomic_write_reads_during_concurrent_writes_see_old_or_new(tmp_path: Path) -> None:
    """Readers running while another thread repeatedly overwrites the target
    must always see a fully-parseable JSON document — never a partial/torn
    write. ``os.replace`` guarantees this atomicity; this test pins the
    behaviour against regressions that reintroduce plain ``write_text``.
    """
    target = tmp_path / "state.json"
    write_atomic_json(target, {"v": 0})

    stop = threading.Event()
    errors: list[str] = []
    observed_versions: set[int] = set()

    def writer() -> None:
        i = 1
        while not stop.is_set():
            try:
                write_atomic_json(target, {"v": i, "pad": "x" * 4096})
            except OSError as exc:  # pragma: no cover - should not happen
                errors.append(f"write failed: {exc}")
                return
            i += 1

    def reader() -> None:
        while not stop.is_set():
            try:
                raw = target.read_text(encoding="utf-8")
            except FileNotFoundError:
                continue
            try:
                data = json.loads(raw)
            except json.JSONDecodeError as exc:
                errors.append(f"torn read: {exc!r} on {raw[:80]!r}")
                return
            v = data.get("v")
            if isinstance(v, int):
                observed_versions.add(v)

    w = threading.Thread(target=writer)
    readers = [threading.Thread(target=reader) for _ in range(4)]
    w.start()
    for r in readers:
        r.start()

    # Let them race for a short while.
    threading.Event().wait(0.5)
    stop.set()

    w.join(timeout=5)
    for r in readers:
        r.join(timeout=5)

    assert errors == [], f"concurrent access produced torn reads: {errors}"
    # We should have observed many distinct versions — proves the reader/writer
    # actually interleaved.
    assert len(observed_versions) >= 5


def test_persistence_write_session_json_atomic(tmp_path: Path) -> None:
    """End-to-end check that save_session routes through the atomic helper."""
    from bernstein.core.persistence.session import SessionState, save_session

    state = SessionState(saved_at=123.0, goal="test", cost_spent=1.5)
    save_session(tmp_path, state)

    session_path = tmp_path / ".sdd" / "runtime" / "session.json"
    assert session_path.exists()
    data = json.loads(session_path.read_text(encoding="utf-8"))
    assert data["saved_at"] == pytest.approx(123.0)
    assert data["goal"] == "test"
    # No stray temp files.
    runtime = session_path.parent
    assert [p.name for p in runtime.iterdir() if ".tmp." in p.name] == []


def test_runtime_write_supervisor_state_atomic(tmp_path: Path) -> None:
    """write_supervisor_state uses atomic semantics and cleans tmp files."""
    from bernstein.core.persistence.runtime_state import (
        SupervisorStateSnapshot,
        write_supervisor_state,
    )

    snap = SupervisorStateSnapshot(started_at=1.0, restart_count=0, current_pid=os.getpid())
    path = write_supervisor_state(tmp_path, snap)
    assert path.exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["current_pid"] == os.getpid()
    assert [p.name for p in path.parent.iterdir() if ".tmp." in p.name] == []


def test_runtime_write_file_locks_atomic(tmp_path: Path) -> None:
    """FileLockManager._save routes through the atomic helper."""
    from bernstein.core.persistence.file_locks import FileLockManager

    manager = FileLockManager(tmp_path)
    conflicts = manager.acquire(["src/foo.py"], agent_id="agent-1", task_id="t1")
    assert conflicts == []

    lock_path = tmp_path / ".sdd" / "runtime" / "file_locks.json"
    assert lock_path.exists()
    data = json.loads(lock_path.read_text(encoding="utf-8"))
    assert any(entry["file_path"] == "src/foo.py" for entry in data)
    assert [p.name for p in lock_path.parent.iterdir() if ".tmp." in p.name] == []
