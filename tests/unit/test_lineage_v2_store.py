"""Unit tests for LineageV2Store - two-layer storage with detached children.

Covers append, replay, verify, HMAC chain tampering, partial-write
recovery, oversize bodies, concurrent appends, missing child files,
parent-without-child, and the public dataclass invariants.
"""

from __future__ import annotations

import dataclasses
import json
import multiprocessing as mp
import threading
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest

from bernstein.core.lineage.v2_store import (
    LINEAGE_V2_ENTRY_VERSION,
    ChildBody,
    LineageV2Store,
    ParentRef,
    compute_child_sha,
    is_v2_enabled,
)

_TEST_KEY = b"test-hmac-key-for-v2-lineage-store-unit-tests"


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _make_parent(
    *,
    task_id: str = "task-001",
    child_run_id: str = "child-run-001",
    parent_call_id: str = "call-a",
    summary: str = "spawn child",
    child_sha: str = "sha256:" + "0" * 64,
    ts_ns: int = 1_000_000_000,
) -> ParentRef:
    return ParentRef(
        v=LINEAGE_V2_ENTRY_VERSION,
        task_id=task_id,
        child_run_id=child_run_id,
        parent_call_id=parent_call_id,
        summary=summary,
        child_sha=child_sha,
        ts_ns=ts_ns,
        prev_hmac="",
        hmac="",
    )


def _make_child(
    *,
    task_id: str = "task-001",
    child_run_id: str = "child-run-001",
    seq: int = 0,
    kind: str = "subagent.started",
    payload: dict[str, Any] | None = None,
    ts_ns: int = 1_000_000_000,
) -> ChildBody:
    return ChildBody(
        v=LINEAGE_V2_ENTRY_VERSION,
        task_id=task_id,
        child_run_id=child_run_id,
        seq=seq,
        kind=kind,
        payload=dict(payload or {}),
        ts_ns=ts_ns,
        prev_hmac="",
        hmac="",
    )


# ---------------------------------------------------------------------------
# Dataclass invariants
# ---------------------------------------------------------------------------


def test_parent_ref_rejects_wrong_version() -> None:
    with pytest.raises(ValueError, match="unsupported parent_ref version"):
        ParentRef(
            v=99,
            task_id="t",
            child_run_id="r",
            parent_call_id="c",
            summary="",
            child_sha="sha256:" + "0" * 64,
            ts_ns=0,
            prev_hmac="",
            hmac="",
        )


def test_parent_ref_rejects_empty_task_id() -> None:
    with pytest.raises(ValueError, match="task_id"):
        ParentRef(
            v=2,
            task_id="",
            child_run_id="r",
            parent_call_id="c",
            summary="",
            child_sha="sha256:" + "0" * 64,
            ts_ns=0,
            prev_hmac="",
            hmac="",
        )


def test_parent_ref_rejects_empty_child_run_id() -> None:
    with pytest.raises(ValueError, match="child_run_id"):
        ParentRef(
            v=2,
            task_id="t",
            child_run_id="",
            parent_call_id="c",
            summary="",
            child_sha="sha256:" + "0" * 64,
            ts_ns=0,
            prev_hmac="",
            hmac="",
        )


def test_parent_ref_rejects_bad_child_sha_prefix() -> None:
    with pytest.raises(ValueError, match="child_sha"):
        ParentRef(
            v=2,
            task_id="t",
            child_run_id="r",
            parent_call_id="c",
            summary="",
            child_sha="md5:abc",
            ts_ns=0,
            prev_hmac="",
            hmac="",
        )


def test_child_body_rejects_wrong_version() -> None:
    with pytest.raises(ValueError, match="unsupported child_body version"):
        ChildBody(
            v=1,
            task_id="t",
            child_run_id="r",
            seq=0,
            kind="x",
        )


def test_child_body_rejects_negative_seq() -> None:
    with pytest.raises(ValueError, match="seq"):
        ChildBody(
            v=2,
            task_id="t",
            child_run_id="r",
            seq=-1,
            kind="x",
        )


def test_child_body_rejects_empty_kind() -> None:
    with pytest.raises(ValueError, match="kind"):
        ChildBody(
            v=2,
            task_id="t",
            child_run_id="r",
            seq=0,
            kind="",
        )


def test_child_body_rejects_empty_task_id() -> None:
    with pytest.raises(ValueError, match="task_id"):
        ChildBody(
            v=2,
            task_id="",
            child_run_id="r",
            seq=0,
            kind="x",
        )


def test_child_body_rejects_empty_child_run_id() -> None:
    with pytest.raises(ValueError, match="child_run_id"):
        ChildBody(
            v=2,
            task_id="t",
            child_run_id="",
            seq=0,
            kind="x",
        )


def test_parent_ref_is_frozen() -> None:
    ref = _make_parent()
    with pytest.raises((AttributeError, TypeError, dataclasses.FrozenInstanceError)):
        ref.task_id = "other"  # type: ignore[misc]


def test_child_body_is_frozen() -> None:
    body = _make_child()
    with pytest.raises((AttributeError, TypeError, dataclasses.FrozenInstanceError)):
        body.kind = "other"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# compute_child_sha
# ---------------------------------------------------------------------------


def test_compute_child_sha_is_deterministic() -> None:
    body = _make_child(payload={"k": "v", "n": 1})
    a = compute_child_sha(body)
    b = compute_child_sha(body)
    assert a == b
    assert a.startswith("sha256:")


def test_compute_child_sha_changes_with_payload() -> None:
    a = compute_child_sha(_make_child(payload={"k": "v"}))
    b = compute_child_sha(_make_child(payload={"k": "w"}))
    assert a != b


def test_compute_child_sha_ignores_hmac_fields() -> None:
    """The content-address must NOT depend on chain state."""
    body_a = _make_child()
    body_b = ChildBody(
        v=body_a.v,
        task_id=body_a.task_id,
        child_run_id=body_a.child_run_id,
        seq=body_a.seq,
        kind=body_a.kind,
        payload=dict(body_a.payload),
        ts_ns=body_a.ts_ns,
        prev_hmac="some-prev",
        hmac="some-hmac",
    )
    assert compute_child_sha(body_a) == compute_child_sha(body_b)


def test_compute_child_sha_differs_for_seq() -> None:
    a = compute_child_sha(_make_child(seq=0))
    b = compute_child_sha(_make_child(seq=1))
    assert a != b


# ---------------------------------------------------------------------------
# Store construction
# ---------------------------------------------------------------------------


def test_store_creates_root_and_children_dir(tmp_path: Path) -> None:
    root = tmp_path / "v2"
    LineageV2Store(root, hmac_key=_TEST_KEY)
    assert root.exists()
    assert (root / "children").exists()


def test_store_default_hmac_key_works(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2")
    store.append(_make_parent(), _make_child())
    assert store.verify().ok


def test_store_parent_log_path_property(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    assert store.parent_log == tmp_path / "v2" / "parent.jsonl"


def test_store_child_log_path_strips_prefix(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    sha = "sha256:" + "a" * 64
    p = store.child_log(sha)
    assert p.name == "a" * 64 + ".jsonl"


def test_store_child_log_path_without_prefix(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    p = store.child_log("b" * 64)
    assert p.name == "b" * 64 + ".jsonl"


# ---------------------------------------------------------------------------
# Append basics
# ---------------------------------------------------------------------------


def test_append_returns_child_sha_and_parent_hmac(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    child_sha, parent_hmac = store.append(_make_parent(), _make_child())
    assert child_sha.startswith("sha256:")
    assert len(parent_hmac) == 64  # sha256 hex digest


def test_append_writes_parent_line(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    store.append(_make_parent(), _make_child())
    assert store.parent_log.exists()
    text = store.parent_log.read_text()
    assert text.endswith("\n")
    assert text.count("\n") == 1


def test_append_writes_child_file(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    child_sha, _ = store.append(_make_parent(), _make_child())
    child_path = store.child_log(child_sha)
    assert child_path.exists()
    assert child_path.read_text().endswith("\n")


def test_append_recorded_child_sha_matches_first_body(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    child = _make_child(payload={"hello": "world"})
    child_sha, _ = store.append(_make_parent(), child)
    # The first body line, re-read, must have content-address == child_sha.
    bodies = list(store.iter_child_bodies(child_sha))
    assert bodies
    first = bodies[0]
    seed = ChildBody(
        v=first.v,
        task_id=first.task_id,
        child_run_id=first.child_run_id,
        seq=first.seq,
        kind=first.kind,
        payload=dict(first.payload),
        ts_ns=first.ts_ns,
        prev_hmac="",
        hmac="",
    )
    assert compute_child_sha(seed) == child_sha


def test_append_parent_carries_child_sha(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    child_sha, _ = store.append(_make_parent(), _make_child(payload={"a": 1}))
    refs = list(store.iter_parent_refs())
    assert len(refs) == 1
    assert refs[0].child_sha == child_sha


def test_append_first_parent_has_empty_prev_hmac(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    store.append(_make_parent(), _make_child())
    refs = list(store.iter_parent_refs())
    assert refs[0].prev_hmac == ""


def test_append_chains_parent_prev_hmac(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    _, h1 = store.append(_make_parent(child_run_id="r1"), _make_child(child_run_id="r1"))
    _, h2 = store.append(_make_parent(child_run_id="r2"), _make_child(child_run_id="r2"))
    refs = list(store.iter_parent_refs())
    assert refs[0].hmac == h1
    assert refs[1].prev_hmac == h1
    assert refs[1].hmac == h2


def test_append_persists_summary_verbatim(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    store.append(_make_parent(summary="ran 3 steps"), _make_child())
    refs = list(store.iter_parent_refs())
    assert refs[0].summary == "ran 3 steps"


def test_append_persists_parent_call_id(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    store.append(_make_parent(parent_call_id="call-xyz"), _make_child())
    refs = list(store.iter_parent_refs())
    assert refs[0].parent_call_id == "call-xyz"


def test_append_persists_ts_ns(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    store.append(_make_parent(ts_ns=42_000_000_000), _make_child(ts_ns=42_000_000_000))
    refs = list(store.iter_parent_refs())
    assert refs[0].ts_ns == 42_000_000_000


def test_append_two_distinct_children_have_distinct_files(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    sha_a, _ = store.append(_make_parent(child_run_id="r1"), _make_child(child_run_id="r1", payload={"x": 1}))
    sha_b, _ = store.append(_make_parent(child_run_id="r2"), _make_child(child_run_id="r2", payload={"x": 2}))
    assert sha_a != sha_b
    assert store.child_log(sha_a).exists()
    assert store.child_log(sha_b).exists()


def test_append_same_child_body_same_sha(tmp_path: Path) -> None:
    """Idempotent content-address - same body bytes yield same sha."""
    store_a = LineageV2Store(tmp_path / "a", hmac_key=_TEST_KEY)
    store_b = LineageV2Store(tmp_path / "b", hmac_key=_TEST_KEY)
    body = _make_child(payload={"const": True})
    sha_a, _ = store_a.append(_make_parent(), body)
    sha_b, _ = store_b.append(_make_parent(), body)
    assert sha_a == sha_b


# ---------------------------------------------------------------------------
# append_child_body
# ---------------------------------------------------------------------------


def test_append_child_body_extends_chain(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    child_sha, _ = store.append(_make_parent(), _make_child(seq=0))
    store.append_child_body(child_sha, _make_child(seq=1, kind="subagent.progress"))
    bodies = list(store.iter_child_bodies(child_sha))
    assert len(bodies) == 2
    assert bodies[1].prev_hmac == bodies[0].hmac


def test_append_child_body_missing_file_raises(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    with pytest.raises(FileNotFoundError):
        store.append_child_body("sha256:" + "0" * 64, _make_child(seq=1))


def test_append_child_body_does_not_touch_parent_log(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    child_sha, _ = store.append(_make_parent(), _make_child(seq=0))
    parent_before = store.parent_log.read_bytes()
    store.append_child_body(child_sha, _make_child(seq=1, kind="subagent.completed"))
    assert store.parent_log.read_bytes() == parent_before


def test_append_child_body_multiple_extensions(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    child_sha, _ = store.append(_make_parent(), _make_child(seq=0))
    for i in range(1, 6):
        store.append_child_body(child_sha, _make_child(seq=i, kind="subagent.progress"))
    bodies = list(store.iter_child_bodies(child_sha))
    assert [b.seq for b in bodies] == [0, 1, 2, 3, 4, 5]


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------


def test_replay_empty_for_unknown_task(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    assert store.replay("missing") == []


def test_replay_single_pair(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    store.append(_make_parent(task_id="T"), _make_child(task_id="T"))
    timeline = store.replay("T")
    assert len(timeline) == 1
    ref, bodies = timeline[0]
    assert ref.task_id == "T"
    assert len(bodies) == 1


def test_replay_filters_by_task(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    store.append(_make_parent(task_id="A", child_run_id="rA"), _make_child(task_id="A", child_run_id="rA"))
    store.append(_make_parent(task_id="B", child_run_id="rB"), _make_child(task_id="B", child_run_id="rB"))
    assert len(store.replay("A")) == 1
    assert len(store.replay("B")) == 1


def test_replay_returns_bodies_in_seq_order(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    child_sha, _ = store.append(_make_parent(task_id="T"), _make_child(task_id="T", seq=0))
    store.append_child_body(child_sha, _make_child(task_id="T", seq=1, kind="subagent.progress"))
    store.append_child_body(child_sha, _make_child(task_id="T", seq=2, kind="subagent.completed"))
    timeline = store.replay("T")
    assert len(timeline) == 1
    _, bodies = timeline[0]
    assert [b.seq for b in bodies] == [0, 1, 2]


def test_replay_is_deterministic(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    for i in range(5):
        store.append(
            _make_parent(task_id="T", child_run_id=f"r{i}", ts_ns=i),
            _make_child(task_id="T", child_run_id=f"r{i}", ts_ns=i, payload={"n": i}),
        )
    a = store.replay("T")
    b = store.replay("T")
    assert [r.hmac for r, _ in a] == [r.hmac for r, _ in b]


def test_replay_pair_order_matches_append_order(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    order = []
    for i in range(4):
        store.append(
            _make_parent(task_id="T", child_run_id=f"r{i}", ts_ns=i),
            _make_child(task_id="T", child_run_id=f"r{i}", ts_ns=i, payload={"i": i}),
        )
        order.append(f"r{i}")
    timeline = store.replay("T")
    assert [r.child_run_id for r, _ in timeline] == order


def test_replay_surfaces_missing_child_with_empty_body_list(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    child_sha, _ = store.append(_make_parent(task_id="T"), _make_child(task_id="T"))
    store.child_log(child_sha).unlink()
    timeline = store.replay("T")
    assert len(timeline) == 1
    _, bodies = timeline[0]
    assert bodies == []


# ---------------------------------------------------------------------------
# verify
# ---------------------------------------------------------------------------


def test_verify_clean_log_passes(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    store.append(_make_parent(), _make_child())
    assert store.verify().ok


def test_verify_empty_store_passes(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    r = store.verify()
    assert r.ok
    assert r.parent_count == 0
    assert r.child_count == 0


def test_verify_counts_entries(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    child_sha, _ = store.append(_make_parent(), _make_child(seq=0))
    store.append_child_body(child_sha, _make_child(seq=1, kind="subagent.completed"))
    r = store.verify()
    assert r.ok
    assert r.parent_count == 1
    assert r.child_count == 2


def test_verify_detects_parent_hmac_tampering(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    store.append(_make_parent(), _make_child())
    raw = store.parent_log.read_text()
    obj = json.loads(raw.strip())
    obj["summary"] = "TAMPERED"
    store.parent_log.write_text(json.dumps(obj, sort_keys=True, separators=(",", ":")) + "\n")
    r = store.verify()
    assert not r.ok
    assert any("hmac mismatch" in f for f in r.failures)


def test_verify_detects_parent_prev_hmac_break(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    store.append(_make_parent(child_run_id="r1"), _make_child(child_run_id="r1"))
    store.append(_make_parent(child_run_id="r2"), _make_child(child_run_id="r2"))
    # Replace prev_hmac on the second line with a wrong-but-syntactic value.
    lines = store.parent_log.read_text().splitlines()
    obj = json.loads(lines[1])
    obj["prev_hmac"] = "f" * 64
    lines[1] = json.dumps(obj, sort_keys=True, separators=(",", ":"))
    store.parent_log.write_text("\n".join(lines) + "\n")
    r = store.verify()
    assert not r.ok
    assert any("prev_hmac break" in f or "hmac mismatch" in f for f in r.failures)


def test_verify_detects_child_hmac_tampering(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    child_sha, _ = store.append(_make_parent(), _make_child())
    cpath = store.child_log(child_sha)
    obj = json.loads(cpath.read_text().strip())
    obj["payload"] = {"tampered": True}
    cpath.write_text(json.dumps(obj, sort_keys=True, separators=(",", ":")) + "\n")
    r = store.verify()
    assert not r.ok


def test_verify_detects_missing_child_file(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    child_sha, _ = store.append(_make_parent(), _make_child())
    store.child_log(child_sha).unlink()
    r = store.verify()
    assert not r.ok
    assert any("missing child file" in f for f in r.failures)


def test_verify_detects_orphan_child_file(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    store.append(_make_parent(), _make_child())
    # Plant an unreferenced child file.
    orphan_path = store.child_log("sha256:" + "f" * 64)
    orphan_path.parent.mkdir(parents=True, exist_ok=True)
    orphan_path.write_text("{}\n")
    r = store.verify()
    assert not r.ok
    assert any("orphan child file" in f for f in r.failures)


def test_verify_detects_content_address_mismatch(tmp_path: Path) -> None:
    """Replace the first child-body line with bytes whose body-hash != child_sha."""
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    child_sha, _ = store.append(_make_parent(), _make_child(payload={"a": 1}))
    cpath = store.child_log(child_sha)
    # Swap in a body whose payload is different but re-chain its hmac correctly.
    different = _make_child(payload={"a": 2})
    stamped = store._stamp_child(  # type: ignore[attr-defined]
        ChildBody(
            v=different.v,
            task_id=different.task_id,
            child_run_id=different.child_run_id,
            seq=different.seq,
            kind=different.kind,
            payload=dict(different.payload),
            ts_ns=different.ts_ns,
            prev_hmac="",
            hmac="",
        )
    )
    cpath.write_text(json.dumps(asdict(stamped), sort_keys=True, separators=(",", ":")) + "\n")
    r = store.verify()
    assert not r.ok
    assert any("child_sha mismatch" in f for f in r.failures)


def test_verify_wrong_key_fails_chain(tmp_path: Path) -> None:
    a = LineageV2Store(tmp_path / "v2", hmac_key=b"key-A")
    a.append(_make_parent(), _make_child())
    b = LineageV2Store(tmp_path / "v2", hmac_key=b"key-B")
    r = b.verify()
    assert not r.ok


def test_verify_after_truncation(tmp_path: Path) -> None:
    """Truncating bytes from the parent log produces an unverifiable chain.

    ``verify`` reports the torn line rather than raising: an auditor
    needs a verdict on the store, not a traceback from the JSON parser.
    """
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    store.append(_make_parent(), _make_child())
    raw = store.parent_log.read_bytes()
    # Lop off the last 5 bytes - the line is now torn JSON.
    store.parent_log.write_bytes(raw[:-5])
    r = store.verify()
    assert not r.ok
    assert any("undecodable" in f for f in r.failures)


# ---------------------------------------------------------------------------
# verify - byte coverage
# ---------------------------------------------------------------------------


def _flip_every_byte_and_verify(store: LineageV2Store, path: Path) -> list[tuple[int, str]]:
    """Flip each non-newline byte of ``path`` in turn; return silent passes.

    Returns ``(offset, character)`` for every byte whose mutation left
    ``verify`` reporting a clean store. A correct verifier returns [].
    """
    original = path.read_bytes()
    undetected: list[tuple[int, str]] = []
    try:
        for idx in range(len(original)):
            if original[idx : idx + 1] == b"\n":
                continue
            mutated = bytearray(original)
            mutated[idx] ^= 0x01
            path.write_bytes(bytes(mutated))
            # Must not raise - a tampered store still gets a verdict.
            result = store.verify()
            if result.ok or not result.failures:
                undetected.append((idx, chr(original[idx])))
    finally:
        path.write_bytes(original)
    return undetected


def test_verify_detects_every_single_byte_flip_in_child_log(tmp_path: Path) -> None:
    """Exhaustive: no byte of a child log can be flipped undetected.

    The empty payload is the case that matters. The reader defaults a
    missing ``payload`` key to ``{}``, so renaming that key - one bit in
    any of its seven letters - used to rebuild a record identical to the
    one that was signed. HMAC, chain and content address all matched and
    ``verify`` reported ok.
    """
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    child_sha, _ = store.append(_make_parent(), _make_child(payload={}))
    assert store.verify().ok

    undetected = _flip_every_byte_and_verify(store, store.child_log(child_sha))
    assert undetected == []


def test_verify_detects_every_single_byte_flip_in_parent_log(tmp_path: Path) -> None:
    """Exhaustive: no byte of the parent log can be flipped undetected."""
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    store.append(_make_parent(), _make_child(payload={"k": "v"}))
    assert store.verify().ok

    undetected = _flip_every_byte_and_verify(store, store.parent_log)
    assert undetected == []


def test_verify_detects_renamed_payload_key(tmp_path: Path) -> None:
    """Renaming ``payload`` on an empty-payload record must not verify.

    Pins the exact regression: the mutated line still decodes, and every
    hash and HMAC over the decoded record still matches, because the
    dropped key reads back as the ``{}`` that was signed. Only the bytes
    differ - so only a byte-level check catches it.
    """
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    child_sha, _ = store.append(_make_parent(), _make_child(payload={}))
    path = store.child_log(child_sha)

    tampered = path.read_bytes().replace(b'"payload":', b'"payloae":')
    assert tampered != path.read_bytes(), "fixture did not mutate the payload key"
    path.write_bytes(tampered)

    r = store.verify()
    assert not r.ok
    assert any("non-canonical bytes" in f for f in r.failures)


def test_verify_rejects_unknown_key_appended_to_child_record(tmp_path: Path) -> None:
    """A key the reader ignores still has to break verification."""
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    child_sha, _ = store.append(_make_parent(), _make_child())
    path = store.child_log(child_sha)

    obj = json.loads(path.read_bytes())
    obj["injected"] = "ignored-by-the-reader"
    path.write_bytes(json.dumps(obj, sort_keys=True, separators=(",", ":")).encode() + b"\n")

    r = store.verify()
    assert not r.ok
    assert any("non-canonical bytes" in f for f in r.failures)


# ---------------------------------------------------------------------------
# is_v2_enabled
# ---------------------------------------------------------------------------


def test_is_v2_enabled_default_false() -> None:
    assert is_v2_enabled(env={}) is False


@pytest.mark.parametrize("val", ["1", "true", "yes", "on", "TRUE", "Yes"])
def test_is_v2_enabled_env_truthy(val: str) -> None:
    assert is_v2_enabled(env={"BERNSTEIN_LINEAGE_V2": val}) is True


@pytest.mark.parametrize("val", ["0", "false", "no", ""])
def test_is_v2_enabled_env_falsy(val: str) -> None:
    assert is_v2_enabled(env={"BERNSTEIN_LINEAGE_V2": val}) is False


def test_is_v2_enabled_cfg_version_2() -> None:
    assert is_v2_enabled(env={}, cfg={"lineage": {"version": 2}}) is True


def test_is_v2_enabled_cfg_version_1() -> None:
    assert is_v2_enabled(env={}, cfg={"lineage": {"version": 1}}) is False


def test_is_v2_enabled_cfg_missing_lineage() -> None:
    assert is_v2_enabled(env={}, cfg={"other": {}}) is False


def test_is_v2_enabled_env_overrides_cfg() -> None:
    assert is_v2_enabled(env={"BERNSTEIN_LINEAGE_V2": "1"}, cfg={"lineage": {"version": 1}}) is True


# ---------------------------------------------------------------------------
# Concurrent appends
# ---------------------------------------------------------------------------


def _process_worker(root_str: str, marker: str, n: int, key: bytes) -> None:  # pragma: no cover - subproc
    from bernstein.core.lineage.v2_store import LineageV2Store

    store = LineageV2Store(Path(root_str), hmac_key=key)
    for i in range(n):
        store.append(
            _make_parent(task_id=f"T-{marker}", child_run_id=f"{marker}-{i}", ts_ns=i),
            _make_child(task_id=f"T-{marker}", child_run_id=f"{marker}-{i}", payload={"i": i}, ts_ns=i),
        )


def test_concurrent_process_appends_no_torn_lines(tmp_path: Path) -> None:
    root = tmp_path / "v2"
    root.mkdir()
    n_each = 12
    p1 = mp.get_context("spawn").Process(target=_process_worker, args=(str(root), "A", n_each, _TEST_KEY))
    p2 = mp.get_context("spawn").Process(target=_process_worker, args=(str(root), "B", n_each, _TEST_KEY))
    p1.start()
    p2.start()
    p1.join(timeout=60)
    p2.join(timeout=60)
    assert p1.exitcode == 0
    assert p2.exitcode == 0
    raw = (root / "parent.jsonl").read_bytes()
    lines = raw.rstrip(b"\n").split(b"\n")
    assert len(lines) == 2 * n_each
    for line in lines:
        json.loads(line)
    # Chain is consistent regardless of interleave.
    store = LineageV2Store(root, hmac_key=_TEST_KEY)
    assert store.verify().ok


def test_concurrent_thread_appends_serialised(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    n_each = 15

    def worker(marker: str) -> None:
        for i in range(n_each):
            store.append(
                _make_parent(task_id=f"T-{marker}", child_run_id=f"{marker}-{i}", ts_ns=i),
                _make_child(task_id=f"T-{marker}", child_run_id=f"{marker}-{i}", payload={"i": i}, ts_ns=i),
            )

    threads = [threading.Thread(target=worker, args=(m,)) for m in ("X", "Y", "Z")]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=60)
        assert not t.is_alive()
    assert store.verify().ok
    refs = list(store.iter_parent_refs())
    assert len(refs) == 3 * n_each


# ---------------------------------------------------------------------------
# Oversize bodies
# ---------------------------------------------------------------------------


def test_append_oversize_body_roundtrips(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    big = "x" * (256 * 1024)
    sha, _ = store.append(_make_parent(), _make_child(payload={"blob": big}))
    bodies = list(store.iter_child_bodies(sha))
    assert bodies[0].payload["blob"] == big
    assert store.verify().ok


def test_append_unicode_payload_roundtrips(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    sha, _ = store.append(_make_parent(), _make_child(payload={"u": "плагин 漢字 🔥"}))
    bodies = list(store.iter_child_bodies(sha))
    assert bodies[0].payload["u"] == "плагин 漢字 🔥"
    assert store.verify().ok


def test_append_nested_payload_roundtrips(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    nested = {"a": {"b": {"c": [1, 2, 3]}}}
    sha, _ = store.append(_make_parent(), _make_child(payload=nested))
    bodies = list(store.iter_child_bodies(sha))
    assert bodies[0].payload == nested
    assert store.verify().ok


# ---------------------------------------------------------------------------
# Crash-safety / partial-write recovery
# ---------------------------------------------------------------------------


def test_partial_write_recovery_orphan_child_detected(tmp_path: Path) -> None:
    """Simulate a crash AFTER child write but BEFORE parent line lands."""
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    store.append(_make_parent(), _make_child(payload={"a": 1}))
    # Now drop the parent line entirely to simulate the missing parent.
    store.parent_log.write_bytes(b"")
    r = store.verify()
    assert not r.ok
    assert any("orphan" in f for f in r.failures)


def test_append_fsyncs_parent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import os as _os

    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    calls: list[int] = []
    real = _os.fsync

    def spy(fd: int) -> None:
        calls.append(fd)
        real(fd)

    monkeypatch.setattr(_os, "fsync", spy)
    store.append(_make_parent(), _make_child())
    assert calls  # at least one fsync over the lifecycle


# ---------------------------------------------------------------------------
# iter_parent_refs / iter_child_bodies edge cases
# ---------------------------------------------------------------------------


def test_iter_parent_refs_empty_log_yields_nothing(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    assert list(store.iter_parent_refs()) == []


def test_iter_child_bodies_missing_file_yields_nothing(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    assert list(store.iter_child_bodies("sha256:" + "0" * 64)) == []


def test_iter_child_bodies_after_unlink(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    sha, _ = store.append(_make_parent(), _make_child())
    store.child_log(sha).unlink()
    assert list(store.iter_child_bodies(sha)) == []


# ---------------------------------------------------------------------------
# Multi-task interleaved
# ---------------------------------------------------------------------------


def test_multi_task_interleaved_replay_isolates(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    store.append(
        _make_parent(task_id="T1", child_run_id="r1a", ts_ns=1),
        _make_child(task_id="T1", child_run_id="r1a", ts_ns=1, payload={"i": 1}),
    )
    store.append(
        _make_parent(task_id="T2", child_run_id="r2a", ts_ns=2),
        _make_child(task_id="T2", child_run_id="r2a", ts_ns=2, payload={"i": 2}),
    )
    store.append(
        _make_parent(task_id="T1", child_run_id="r1b", ts_ns=3),
        _make_child(task_id="T1", child_run_id="r1b", ts_ns=3, payload={"i": 3}),
    )
    t1 = store.replay("T1")
    t2 = store.replay("T2")
    assert [r.child_run_id for r, _ in t1] == ["r1a", "r1b"]
    assert [r.child_run_id for r, _ in t2] == ["r2a"]


def test_verify_passes_with_interleaved_multi_task(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    for i in range(6):
        task = f"T{i % 3}"
        store.append(
            _make_parent(task_id=task, child_run_id=f"r{i}", ts_ns=i),
            _make_child(task_id=task, child_run_id=f"r{i}", ts_ns=i, payload={"i": i}),
        )
    assert store.verify().ok


# ---------------------------------------------------------------------------
# Export
# ---------------------------------------------------------------------------


def test_export_jsonl_empty_for_unknown_task(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    assert store.export_jsonl("missing") == ""


def test_export_jsonl_includes_parent_and_child(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    store.append(_make_parent(task_id="T"), _make_child(task_id="T"))
    txt = store.export_jsonl("T")
    lines = txt.strip().split("\n")
    assert len(lines) == 2
    a = json.loads(lines[0])
    b = json.loads(lines[1])
    assert a["_kind"] == "parent"
    assert b["_kind"] == "child"


def test_export_jsonl_preserves_seq_order(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    sha, _ = store.append(_make_parent(task_id="T"), _make_child(task_id="T", seq=0))
    store.append_child_body(sha, _make_child(task_id="T", seq=1, kind="subagent.completed"))
    txt = store.export_jsonl("T")
    lines = [json.loads(l) for l in txt.strip().split("\n")]
    assert [obj.get("seq") for obj in lines if obj["_kind"] == "child"] == [0, 1]


def test_export_sigstore_has_one_attestation_per_parent(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    for i in range(3):
        store.append(
            _make_parent(task_id="T", child_run_id=f"r{i}", ts_ns=i),
            _make_child(task_id="T", child_run_id=f"r{i}", ts_ns=i, payload={"i": i}),
        )
    atts = store.export_sigstore("T")
    assert len(atts) == 3
    for a in atts:
        assert a["_type"] == "https://in-toto.io/Statement/v1"
        assert a["predicateType"] == "https://slsa.dev/provenance/v0.3"
        assert isinstance(a["subject"], list)
        assert a["subject"][0]["digest"]["sha256"]


def test_export_sigstore_subject_sha_matches_parent_ref(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    child_sha, _ = store.append(_make_parent(task_id="T"), _make_child(task_id="T"))
    atts = store.export_sigstore("T")
    assert atts[0]["subject"][0]["digest"]["sha256"] == child_sha.removeprefix("sha256:")


def test_export_sigstore_external_params_carry_summary(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    store.append(_make_parent(task_id="T", summary="ran cleanup"), _make_child(task_id="T"))
    atts = store.export_sigstore("T")
    ext = atts[0]["predicate"]["buildDefinition"]["externalParameters"]
    assert ext["summary"] == "ran cleanup"
    assert ext["task_id"] == "T"


def test_export_sigstore_byproducts_one_per_body(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    sha, _ = store.append(_make_parent(task_id="T"), _make_child(task_id="T", seq=0))
    store.append_child_body(sha, _make_child(task_id="T", seq=1, kind="subagent.completed"))
    atts = store.export_sigstore("T")
    byproducts = atts[0]["predicate"]["runDetails"]["byproducts"]
    assert len(byproducts) == 2


def test_export_sigstore_empty_task_returns_empty_list(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    assert store.export_sigstore("nope") == []


def test_export_sigstore_is_json_serialisable(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    store.append(_make_parent(task_id="T"), _make_child(task_id="T"))
    atts = store.export_sigstore("T")
    s = json.dumps(atts)
    assert json.loads(s) == atts


# ---------------------------------------------------------------------------
# Many entries / scale
# ---------------------------------------------------------------------------


def test_many_entries_verify(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    for i in range(100):
        store.append(
            _make_parent(task_id="T", child_run_id=f"r{i}", ts_ns=i),
            _make_child(task_id="T", child_run_id=f"r{i}", ts_ns=i, payload={"i": i}),
        )
    r = store.verify()
    assert r.ok
    assert r.parent_count == 100
    assert r.child_count == 100


def test_many_entries_replay_full(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    for i in range(50):
        store.append(
            _make_parent(task_id="T", child_run_id=f"r{i}", ts_ns=i),
            _make_child(task_id="T", child_run_id=f"r{i}", ts_ns=i, payload={"i": i}),
        )
    timeline = store.replay("T")
    assert len(timeline) == 50


def test_verify_failures_message_format(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    store.append(_make_parent(), _make_child())
    raw = store.parent_log.read_text()
    obj = json.loads(raw.strip())
    obj["summary"] = "TAMPERED"
    store.parent_log.write_text(json.dumps(obj, sort_keys=True, separators=(",", ":")) + "\n")
    r = store.verify()
    # All failures are strings.
    for f in r.failures:
        assert isinstance(f, str)


def test_verify_result_dataclass_invariants(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    r = store.verify()
    assert isinstance(r.ok, bool)
    assert isinstance(r.failures, list)
    assert isinstance(r.parent_count, int)
    assert isinstance(r.child_count, int)


# ---------------------------------------------------------------------------
# child_sha derivation invariants
# ---------------------------------------------------------------------------


def test_same_seed_body_with_different_ts_gives_different_sha() -> None:
    a = compute_child_sha(_make_child(ts_ns=1))
    b = compute_child_sha(_make_child(ts_ns=2))
    assert a != b


def test_child_sha_has_64_hex_chars() -> None:
    sha = compute_child_sha(_make_child())
    hex_part = sha.removeprefix("sha256:")
    assert len(hex_part) == 64
    int(hex_part, 16)


def test_append_does_not_clobber_existing_child_file(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    body = _make_child(payload={"const": True})
    sha1, _ = store.append(_make_parent(child_run_id="r1"), body)
    # Re-append the same body shape: same child_sha, append to existing file.
    sha2, _ = store.append(_make_parent(child_run_id="r2"), body)
    assert sha1 == sha2
    bodies = list(store.iter_child_bodies(sha1))
    assert len(bodies) == 2


# ---------------------------------------------------------------------------
# Parent ref / child body roundtrip
# ---------------------------------------------------------------------------


def test_parent_ref_roundtrip_via_log(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    store.append(_make_parent(task_id="T", child_run_id="r", summary="hi", ts_ns=42), _make_child(task_id="T"))
    ref = next(iter(store.iter_parent_refs()))
    assert ref.task_id == "T"
    assert ref.child_run_id == "r"
    assert ref.summary == "hi"
    assert ref.ts_ns == 42


def test_child_body_roundtrip_payload_preserved(tmp_path: Path) -> None:
    store = LineageV2Store(tmp_path / "v2", hmac_key=_TEST_KEY)
    p = {"int": 1, "str": "x", "bool": True, "list": [1, 2], "obj": {"nested": True}}
    sha, _ = store.append(_make_parent(), _make_child(payload=p))
    body = next(iter(store.iter_child_bodies(sha)))
    assert body.payload == p
