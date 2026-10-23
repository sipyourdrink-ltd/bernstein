"""Tests for `LineageStore` - append-only log + tip projections + reindex.

These cover the file-layout invariants described in ADR-009 §4 plus the
crash-safety / concurrency guarantees the recorder relies on:

  * ``log.jsonl`` is the source of truth; ``by-artefact/`` and ``tips/`` are
    rebuildable projections.
  * Every ``append`` fsyncs the log before returning.
  * ``flock(LOCK_EX)`` over the log serialises concurrent writers.
  * Tip JSON files are written via write-then-rename for atomicity.
"""

from __future__ import annotations

import hashlib
import json
import multiprocessing as mp
import os
import threading
from pathlib import Path

import pytest

from bernstein.core.lineage.entry import LineageEntry, canonicalise, entry_hash
from bernstein.core.lineage.store import LineageStore


def _make_entry(
    *,
    artefact_path: str = "src/foo.py",
    content_hash: str | None = None,
    parent_hashes: list[str] | None = None,
    ts_ns: int = 1_715_600_000_000_000_000,
    operator_hmac: str = "deadbeef" * 8,
) -> LineageEntry:
    return LineageEntry(
        v=1,
        artefact_path=artefact_path,
        artefact_kind="file",
        content_hash=content_hash or ("sha256:" + "a" * 64),
        parent_hashes=parent_hashes or [],
        agent_id="agent:claude-worker-1",
        agent_card_kid="key-001",
        tool_call_id="tc-7f3a",
        span_id="00f067aa0ba902b7",
        ts_ns=ts_ns,
        operator_hmac=operator_hmac,
    )


def _path_shard(artefact_path: str) -> tuple[str, str]:
    """Return (shard-dir, full-hash) using sha256(artefact_path) as in ADR-009 §4."""
    h = hashlib.sha256(artefact_path.encode("utf-8")).hexdigest()
    return h[:2], h


# ---------------------------------------------------------------------------
# Append + read roundtrip
# ---------------------------------------------------------------------------


def test_append_returns_entry_hash(tmp_path: Path) -> None:
    store = LineageStore(tmp_path / "lineage")
    entry = _make_entry()
    h = store.append(entry, jws="dummyJWS")
    assert h == entry_hash(entry)


def test_append_writes_canonical_line_to_log(tmp_path: Path) -> None:
    root = tmp_path / "lineage"
    store = LineageStore(root)
    entry = _make_entry()
    store.append(entry, jws="dummyJWS")

    log_path = root / "log.jsonl"
    assert log_path.exists()
    raw = log_path.read_bytes()
    assert raw.endswith(b"\n")
    # Single JSONL record, byte-equal to canonical form (with trailing \n).
    assert raw[:-1] == canonicalise(entry)


def test_read_log_yields_entries_and_jws(tmp_path: Path) -> None:
    store = LineageStore(tmp_path / "lineage")
    e1 = _make_entry(ts_ns=1)
    e2 = _make_entry(ts_ns=2, parent_hashes=[entry_hash(e1)])
    store.append(e1, jws="jws-1")
    store.append(e2, jws="jws-2")

    records = list(store.read_log())
    assert len(records) == 2
    assert records[0][0] == e1
    assert records[1][0] == e2
    assert records[0][1] == "jws-1"
    assert records[1][1] == "jws-2"


# ---------------------------------------------------------------------------
# Tip projection + fork detection
# ---------------------------------------------------------------------------


def test_genesis_tip_is_first_entry(tmp_path: Path) -> None:
    store = LineageStore(tmp_path / "lineage")
    entry = _make_entry()
    h = store.append(entry, jws="jws")
    tips = store.tip_set("src/foo.py")
    assert tips == {"open": [h], "merged": []}


def test_linear_chain_collapses_to_single_open_tip(tmp_path: Path) -> None:
    store = LineageStore(tmp_path / "lineage")
    e1 = _make_entry(content_hash="sha256:" + "1" * 64, ts_ns=1)
    h1 = store.append(e1, jws="jws-1")
    e2 = _make_entry(content_hash="sha256:" + "2" * 64, parent_hashes=[h1], ts_ns=2)
    h2 = store.append(e2, jws="jws-2")
    tips = store.tip_set("src/foo.py")
    # Only the most recent entry is the open tip; its parent is consumed.
    assert tips["open"] == [h2]
    assert tips["merged"] == []
    # The intermediate entry is not reported as open.
    assert h1 not in tips["open"]


def test_fork_surfaces_two_open_tips(tmp_path: Path) -> None:
    store = LineageStore(tmp_path / "lineage")
    e0 = _make_entry(content_hash="sha256:" + "0" * 64, ts_ns=1)
    h0 = store.append(e0, jws="jws-0")
    # Two siblings sharing the same parent - classic fork.
    e_a = _make_entry(content_hash="sha256:" + "a" * 64, parent_hashes=[h0], ts_ns=2)
    e_b = _make_entry(content_hash="sha256:" + "b" * 64, parent_hashes=[h0], ts_ns=3)
    h_a = store.append(e_a, jws="jws-a")
    h_b = store.append(e_b, jws="jws-b")
    tips = store.tip_set("src/foo.py")
    assert set(tips["open"]) == {h_a, h_b}
    assert tips["merged"] == []


def test_merge_entry_resolves_fork(tmp_path: Path) -> None:
    store = LineageStore(tmp_path / "lineage")
    e0 = _make_entry(content_hash="sha256:" + "0" * 64, ts_ns=1)
    h0 = store.append(e0, jws="jws-0")
    e_a = _make_entry(content_hash="sha256:" + "a" * 64, parent_hashes=[h0], ts_ns=2)
    e_b = _make_entry(content_hash="sha256:" + "b" * 64, parent_hashes=[h0], ts_ns=3)
    h_a = store.append(e_a, jws="jws-a")
    h_b = store.append(e_b, jws="jws-b")
    merge = _make_entry(content_hash="sha256:" + "c" * 64, parent_hashes=[h_a, h_b], ts_ns=4)
    h_m = store.append(merge, jws="jws-merge")
    tips = store.tip_set("src/foo.py")
    assert tips["open"] == [h_m]
    assert set(tips["merged"]) == {h_a, h_b}


def test_tip_set_isolated_per_artefact(tmp_path: Path) -> None:
    store = LineageStore(tmp_path / "lineage")
    e_foo = _make_entry(artefact_path="src/foo.py", ts_ns=1)
    e_bar = _make_entry(artefact_path="src/bar.py", ts_ns=2)
    h_foo = store.append(e_foo, jws="jws-foo")
    h_bar = store.append(e_bar, jws="jws-bar")
    assert store.tip_set("src/foo.py") == {"open": [h_foo], "merged": []}
    assert store.tip_set("src/bar.py") == {"open": [h_bar], "merged": []}


def test_tip_set_empty_for_unknown_artefact(tmp_path: Path) -> None:
    store = LineageStore(tmp_path / "lineage")
    assert store.tip_set("src/never-touched.py") == {"open": [], "merged": []}


# ---------------------------------------------------------------------------
# File layout (ADR-009 §4)
# ---------------------------------------------------------------------------


def test_by_artefact_projection_uses_path_hash_shard(tmp_path: Path) -> None:
    root = tmp_path / "lineage"
    store = LineageStore(root)
    entry = _make_entry(artefact_path="src/foo.py")
    store.append(entry, jws="jws")
    shard, full = _path_shard("src/foo.py")
    proj = root / "by-artefact" / shard / f"{full}.jsonl"
    assert proj.exists()
    # Projection holds the same canonical JSONL line as the main log.
    assert proj.read_bytes().rstrip(b"\n") == canonicalise(entry)


def test_tips_file_uses_path_hash(tmp_path: Path) -> None:
    root = tmp_path / "lineage"
    store = LineageStore(root)
    entry = _make_entry(artefact_path="src/foo.py")
    store.append(entry, jws="jws")
    _, full = _path_shard("src/foo.py")
    tips_path = root / "tips" / f"{full}.json"
    assert tips_path.exists()
    payload = json.loads(tips_path.read_text(encoding="utf-8"))
    assert payload == {"open": [entry_hash(entry)], "merged": []}


def test_signature_sidecar_written(tmp_path: Path) -> None:
    root = tmp_path / "lineage"
    store = LineageStore(root)
    entry = _make_entry(artefact_path="src/foo.py")
    h = store.append(entry, jws="abc.def.ghi")
    shard, full = _path_shard("src/foo.py")
    # Sidecar filename strips the ``sha256:`` prefix so the path is portable
    # (Windows rejects ``:`` in filenames) and matches the gate's lookup.
    stem = h.split(":", 1)[1] if ":" in h else h
    sig_path = root / "signatures" / shard / full / f"{stem}.jws"
    assert sig_path.exists()
    assert sig_path.read_text(encoding="utf-8") == "abc.def.ghi"


# ---------------------------------------------------------------------------
# Reindex
# ---------------------------------------------------------------------------


def test_reindex_rebuilds_projections_after_deletion(tmp_path: Path) -> None:
    root = tmp_path / "lineage"
    store = LineageStore(root)
    e1 = _make_entry(content_hash="sha256:" + "1" * 64, ts_ns=1)
    h1 = store.append(e1, jws="jws-1")
    e2 = _make_entry(content_hash="sha256:" + "2" * 64, parent_hashes=[h1], ts_ns=2)
    h2 = store.append(e2, jws="jws-2")

    # Nuke projections - the log alone is the source of truth.
    import shutil

    shutil.rmtree(root / "by-artefact")
    shutil.rmtree(root / "tips")

    store.reindex()

    shard, full = _path_shard("src/foo.py")
    proj = root / "by-artefact" / shard / f"{full}.jsonl"
    assert proj.exists()
    lines = proj.read_bytes().rstrip(b"\n").split(b"\n")
    assert len(lines) == 2

    tips_path = root / "tips" / f"{full}.json"
    payload = json.loads(tips_path.read_text(encoding="utf-8"))
    assert payload == {"open": [h2], "merged": []}


def test_reindex_is_idempotent(tmp_path: Path) -> None:
    root = tmp_path / "lineage"
    store = LineageStore(root)
    e1 = _make_entry(ts_ns=1)
    store.append(e1, jws="jws-1")
    e2 = _make_entry(content_hash="sha256:" + "2" * 64, ts_ns=2, parent_hashes=[entry_hash(e1)])
    store.append(e2, jws="jws-2")

    _, full = _path_shard("src/foo.py")
    proj = root / "by-artefact" / full[:2] / f"{full}.jsonl"
    before = proj.read_bytes()
    store.reindex()
    after = proj.read_bytes()
    assert before == after


# ---------------------------------------------------------------------------
# Concurrency: flock(LOCK_EX) serialises appends
# ---------------------------------------------------------------------------


def _appender_worker(root_str: str, n: int, agent_marker: str) -> None:  # pragma: no cover - subprocess
    from bernstein.core.lineage.entry import LineageEntry
    from bernstein.core.lineage.store import LineageStore

    store = LineageStore(Path(root_str))
    for i in range(n):
        entry = LineageEntry(
            v=1,
            artefact_path=f"src/{agent_marker}-{i}.py",
            artefact_kind="file",
            content_hash="sha256:" + (agent_marker + str(i)).rjust(64, "0"),
            parent_hashes=[],
            agent_id=f"agent:{agent_marker}",
            agent_card_kid="key-001",
            tool_call_id=f"tc-{agent_marker}-{i}",
            span_id="00f067aa0ba902b7",
            ts_ns=i,
            operator_hmac="deadbeef" * 8,
        )
        store.append(entry, jws=f"jws-{agent_marker}-{i}")


def test_concurrent_appends_serialised_no_torn_lines(tmp_path: Path) -> None:
    """Two processes hammering the same log must not interleave bytes within a line."""
    root = tmp_path / "lineage"
    root.mkdir()
    n_each = 30
    p1 = mp.get_context("spawn").Process(target=_appender_worker, args=(str(root), n_each, "A"))
    p2 = mp.get_context("spawn").Process(target=_appender_worker, args=(str(root), n_each, "B"))
    p1.start()
    p2.start()
    p1.join(timeout=30)
    p2.join(timeout=30)
    assert p1.exitcode == 0, p1.exitcode
    assert p2.exitcode == 0, p2.exitcode

    log_path = root / "log.jsonl"
    raw = log_path.read_bytes()
    # No torn lines: every line parses as JSON, and the total line count is exact.
    lines = raw.rstrip(b"\n").split(b"\n")
    assert len(lines) == 2 * n_each
    for line in lines:
        # Parses cleanly → no interleaved bytes from the other writer.
        json.loads(line)


def test_threaded_appends_dont_clobber(tmp_path: Path) -> None:
    """Threads share the same LineageStore instance; the lock must still serialise."""
    root = tmp_path / "lineage"
    store = LineageStore(root)
    n_each = 20

    def worker(marker: str) -> None:
        for i in range(n_each):
            entry = _make_entry(
                artefact_path=f"src/{marker}-{i}.py",
                content_hash="sha256:" + (marker + str(i)).rjust(64, "0"),
                ts_ns=i,
            )
            store.append(entry, jws=f"jws-{marker}-{i}")

    threads = [threading.Thread(target=worker, args=(m,)) for m in ("X", "Y", "Z")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
        assert not t.is_alive()

    lines = (root / "log.jsonl").read_bytes().rstrip(b"\n").split(b"\n")
    assert len(lines) == 3 * n_each
    for line in lines:
        json.loads(line)


# ---------------------------------------------------------------------------
# Crash safety
# ---------------------------------------------------------------------------


def test_append_fsyncs_before_return(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``append`` must call ``os.fsync`` on the log file descriptor before returning."""
    root = tmp_path / "lineage"
    store = LineageStore(root)

    fsync_calls: list[int] = []
    real_fsync = os.fsync

    def spy_fsync(fd: int) -> None:
        fsync_calls.append(fd)
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", spy_fsync)
    store.append(_make_entry(), jws="jws")
    assert fsync_calls, "expected at least one fsync on log.jsonl"


def test_recovery_from_partial_tip_write(tmp_path: Path) -> None:
    """If a torn tip file is left on disk, ``reindex`` rebuilds clean state from the log."""
    root = tmp_path / "lineage"
    store = LineageStore(root)
    e1 = _make_entry(ts_ns=1)
    h1 = store.append(e1, jws="jws-1")

    # Simulate a torn write - clobber the tip JSON with garbage.
    _, full = _path_shard("src/foo.py")
    tip_path = root / "tips" / f"{full}.json"
    tip_path.write_text("{not-json", encoding="utf-8")

    store.reindex()
    payload = json.loads(tip_path.read_text(encoding="utf-8"))
    assert payload == {"open": [h1], "merged": []}


# ---------------------------------------------------------------------------
# Optional fields survive a write -> read round trip
# ---------------------------------------------------------------------------


def test_read_log_round_trips_model_ref_and_activity_source(tmp_path: Path) -> None:
    """``model_ref`` and ``activity_source`` are in the log line, so reading
    must return them -- otherwise the rebuilt entry hashes differently from the
    one that was written and nothing can query by model."""
    from dataclasses import replace

    from bernstein.core.lineage.entry import ACTIVITY_SOURCES, ModelRef

    store = LineageStore(tmp_path / "lineage")
    ref = ModelRef(
        provider="anthropic",
        model_requested="opus",
        model_reported="opus-4-1",
        version="2026-01",
        routing_decision_hash="sha256:" + "b" * 64,
    )
    entry = replace(_make_entry(), model_ref=ref, activity_source=sorted(ACTIVITY_SOURCES)[0])
    written = store.append(entry, jws="jws")

    ((read_entry, _jws),) = list(store.read_log())

    assert read_entry.model_ref == ref
    assert read_entry.activity_source == entry.activity_source
    assert entry_hash(read_entry) == written
