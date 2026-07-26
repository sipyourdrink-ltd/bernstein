"""Property-based tests for lineage v2 (Hypothesis).

Properties:

1. HMAC chain unforgeability       - flipping any byte of any record on disk
                                     produces at least one verify() failure.
                                     ``verify`` is total: it reports, it never
                                     raises and never passes silently.
2. Replay determinism              - replay() is a pure function of disk state.
3. Append commutativity per task   - sequences of appends targeting distinct
                                     tasks can be interleaved without changing
                                     the per-task replay output.
4. Verify-after-truncate invariants - truncating bytes never produces a
                                     "silent ok" result; verify returns
                                     ok=False.
5. Roundtrip                       - any well-formed (parent, child) pair
                                     written and read back equals the input
                                     (modulo the prev_hmac/hmac stamps).
6. child_sha collision-free        - distinct child-body seeds have distinct
                                     child_sha values.

Heavy fuzz sweeps run in the nightly ``deep`` profile via the shared
``tests/property/conftest.py``; PR-time runs ``smoke`` (50 examples).
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from bernstein.core.lineage.v2_store import (
    LINEAGE_V2_ENTRY_VERSION,
    ChildBody,
    LineageV2Store,
    ParentRef,
    compute_child_sha,
)

_TEST_KEY = b"hypothesis-lineage-v2-key"


# ---------------------------------------------------------------------------
# Strategies
# ---------------------------------------------------------------------------

_SAFE_CHARS = st.characters(blacklist_categories=("Cs",), min_codepoint=0x20, max_codepoint=0x7E)
_IDENT = st.text(_SAFE_CHARS, min_size=1, max_size=16)


@st.composite
def _payload(draw: st.DrawFn) -> dict[str, str | int | bool]:
    return draw(
        st.dictionaries(
            keys=st.text(_SAFE_CHARS, min_size=1, max_size=8),
            values=st.one_of(st.integers(-1000, 1000), st.booleans(), st.text(_SAFE_CHARS, max_size=16)),
            max_size=4,
        )
    )


def _make_pair(task_id: str, child_run_id: str, ts_ns: int, payload: dict[str, object]) -> tuple[ParentRef, ChildBody]:
    pref = ParentRef(
        v=LINEAGE_V2_ENTRY_VERSION,
        task_id=task_id,
        child_run_id=child_run_id,
        parent_call_id="call",
        summary="s",
        child_sha="sha256:" + "0" * 64,
        ts_ns=ts_ns,
        prev_hmac="",
        hmac="",
    )
    cbody = ChildBody(
        v=LINEAGE_V2_ENTRY_VERSION,
        task_id=task_id,
        child_run_id=child_run_id,
        seq=0,
        kind="subagent.started",
        payload=payload.copy(),
        ts_ns=ts_ns,
        prev_hmac="",
        hmac="",
    )
    return pref, cbody


# ---------------------------------------------------------------------------
# Property 1 - HMAC chain unforgeability (single byte flip)
# ---------------------------------------------------------------------------


@given(
    task_id=_IDENT,
    payload=_payload(),
    flip_offset=st.integers(min_value=0, max_value=2048),
)
@settings(
    max_examples=50,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
    deadline=None,
)
def test_property_parent_byte_flip_breaks_verify(
    task_id: str,
    payload: dict[str, object],
    flip_offset: int,
) -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "v2"
        store = LineageV2Store(root, hmac_key=_TEST_KEY)
        pref, cbody = _make_pair(task_id, "r", 1, payload)
        store.append(pref, cbody)
        # Pre-condition: clean log verifies.
        assert store.verify().ok

        raw = store.parent_log.read_bytes()
        if not raw:
            return
        idx = flip_offset % len(raw)
        # Skip flipping the trailing newline - that just truncates the line.
        if raw[idx : idx + 1] == b"\n":
            return
        mutated = bytearray(raw)
        # XOR with 0x01 keeps it valid UTF-8 in our ASCII payloads.
        mutated[idx] ^= 0x01
        store.parent_log.write_bytes(bytes(mutated))
        # verify() is total: every mutation is reported, none raises and
        # none passes silently.
        r = store.verify()
        assert not r.ok
        assert r.failures


@given(payload=_payload(), flip_offset=st.integers(min_value=0, max_value=2048))
@settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
def test_property_child_byte_flip_breaks_verify(payload: dict[str, object], flip_offset: int) -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "v2"
        store = LineageV2Store(root, hmac_key=_TEST_KEY)
        pref, cbody = _make_pair("t", "r", 1, payload)
        child_sha, _ = store.append(pref, cbody)
        assert store.verify().ok

        cpath = store.child_log(child_sha)
        raw = cpath.read_bytes()
        if not raw:
            return
        idx = flip_offset % len(raw)
        if raw[idx : idx + 1] == b"\n":
            return
        mutated = bytearray(raw)
        mutated[idx] ^= 0x01
        cpath.write_bytes(bytes(mutated))
        r = store.verify()
        assert not r.ok
        assert r.failures


# ---------------------------------------------------------------------------
# Property 2 - replay determinism
# ---------------------------------------------------------------------------


@given(
    pairs=st.lists(
        st.tuples(_IDENT, _IDENT, st.integers(0, 10**9), _payload()),
        min_size=1,
        max_size=8,
        unique_by=lambda t: t[1],
    ),
)
@settings(max_examples=40, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
def test_property_replay_deterministic(pairs: list[tuple[str, str, int, dict[str, object]]]) -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "v2"
        store = LineageV2Store(root, hmac_key=_TEST_KEY)
        for task_id, run_id, ts, p in pairs:
            pref, cb = _make_pair(task_id, run_id, ts, p)
            store.append(pref, cb)

        task_ids = {t for t, _, _, _ in pairs}
        for tid in task_ids:
            a = store.replay(tid)
            b = store.replay(tid)
            assert [r.hmac for r, _ in a] == [r.hmac for r, _ in b]
            assert [r.child_run_id for r, _ in a] == [r.child_run_id for r, _ in b]


# ---------------------------------------------------------------------------
# Property 3 - append commutativity per task (distinct tasks are independent)
# ---------------------------------------------------------------------------


@given(
    a_runs=st.lists(_IDENT, min_size=1, max_size=4, unique=True),
    b_runs=st.lists(_IDENT, min_size=1, max_size=4, unique=True),
)
@settings(max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
def test_property_per_task_replay_independent_of_interleave(a_runs: list[str], b_runs: list[str]) -> None:
    """Two tasks' replay outputs depend only on each task's own append order.

    Verified by appending {A, B} in two orderings: (A-then-B) and
    (interleaved-zip). The per-task replay must be the same in both.
    """
    with tempfile.TemporaryDirectory() as td:
        # Run 1: A then B.
        s1 = LineageV2Store(Path(td) / "s1", hmac_key=_TEST_KEY)
        for i, r in enumerate(a_runs):
            pref, cb = _make_pair("A", r, i, {"i": i})
            s1.append(pref, cb)
        for i, r in enumerate(b_runs):
            pref, cb = _make_pair("B", r, i, {"i": i})
            s1.append(pref, cb)

        # Run 2: zip-interleaved.
        s2 = LineageV2Store(Path(td) / "s2", hmac_key=_TEST_KEY)
        ai = bi = 0
        ts = 0
        while ai < len(a_runs) or bi < len(b_runs):
            if ai < len(a_runs):
                pref, cb = _make_pair("A", a_runs[ai], ai, {"i": ai})
                s2.append(pref, cb)
                ai += 1
                ts += 1
            if bi < len(b_runs):
                pref, cb = _make_pair("B", b_runs[bi], bi, {"i": bi})
                s2.append(pref, cb)
                bi += 1
                ts += 1

        for tid in ("A", "B"):
            r1 = [r.child_run_id for r, _ in s1.replay(tid)]
            r2 = [r.child_run_id for r, _ in s2.replay(tid)]
            assert r1 == r2


# ---------------------------------------------------------------------------
# Property 4 - verify-after-truncate
# ---------------------------------------------------------------------------


@given(
    n=st.integers(min_value=2, max_value=6),
    drop_last_k=st.integers(min_value=1, max_value=5),
)
@settings(max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
def test_property_truncate_parent_never_silent_ok(n: int, drop_last_k: int) -> None:
    """Dropping the last K parent lines must leave verify failing.

    The parent timeline shrinks but the detached child files for the
    dropped lines still exist, so ``verify`` must surface them as
    orphan child files.
    """
    if drop_last_k >= n:
        return
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "v2"
        store = LineageV2Store(root, hmac_key=_TEST_KEY)
        for i in range(n):
            pref, cb = _make_pair("T", f"r{i}", i, {"i": i})
            store.append(pref, cb)
        raw = store.parent_log.read_bytes()
        lines = raw.rstrip(b"\n").split(b"\n")
        kept = lines[: len(lines) - drop_last_k]
        store.parent_log.write_bytes(b"\n".join(kept) + b"\n")
        r = store.verify()
        assert not r.ok


# ---------------------------------------------------------------------------
# Property 5 - roundtrip (semantic fields preserved)
# ---------------------------------------------------------------------------


@given(
    task_id=_IDENT,
    run_id=_IDENT,
    summary=st.text(_SAFE_CHARS, max_size=40),
    payload=_payload(),
    ts_ns=st.integers(min_value=0, max_value=10**12),
)
@settings(max_examples=40, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
def test_property_roundtrip(
    task_id: str,
    run_id: str,
    summary: str,
    payload: dict[str, object],
    ts_ns: int,
) -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "v2"
        store = LineageV2Store(root, hmac_key=_TEST_KEY)
        pref = ParentRef(
            v=2,
            task_id=task_id,
            child_run_id=run_id,
            parent_call_id="call",
            summary=summary,
            child_sha="sha256:" + "0" * 64,
            ts_ns=ts_ns,
            prev_hmac="",
            hmac="",
        )
        cb = ChildBody(
            v=2,
            task_id=task_id,
            child_run_id=run_id,
            seq=0,
            kind="x",
            payload=payload.copy(),
            ts_ns=ts_ns,
            prev_hmac="",
            hmac="",
        )
        child_sha, _ = store.append(pref, cb)
        ref = next(iter(store.iter_parent_refs()))
        body = next(iter(store.iter_child_bodies(child_sha)))
        assert ref.task_id == task_id
        assert ref.child_run_id == run_id
        assert ref.summary == summary
        assert ref.ts_ns == ts_ns
        assert body.task_id == task_id
        assert body.payload == payload
        assert store.verify().ok


# ---------------------------------------------------------------------------
# Property 6 - child_sha collision-free
# ---------------------------------------------------------------------------


@given(
    a=_payload(),
    b=_payload(),
    seq_a=st.integers(min_value=0, max_value=100),
    seq_b=st.integers(min_value=0, max_value=100),
)
@settings(max_examples=50, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
def test_property_child_sha_distinguishes_distinct_seeds(
    a: dict[str, object],
    b: dict[str, object],
    seq_a: int,
    seq_b: int,
) -> None:
    sa = compute_child_sha(ChildBody(v=2, task_id="t", child_run_id="r", seq=seq_a, kind="x", payload=a.copy()))
    sb = compute_child_sha(ChildBody(v=2, task_id="t", child_run_id="r", seq=seq_b, kind="x", payload=b.copy()))
    if (a, seq_a) == (b, seq_b):
        assert sa == sb
    else:
        # Distinct content -> distinct sha (sha256 collision is astronomical).
        assert sa != sb


# ---------------------------------------------------------------------------
# Property 7 - parent-line count == replay count over the union of tasks
# ---------------------------------------------------------------------------


@given(
    pairs=st.lists(
        st.tuples(_IDENT, _IDENT, st.integers(0, 1000), _payload()),
        min_size=1,
        max_size=10,
        unique_by=lambda t: t[1],
    )
)
@settings(max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
def test_property_count_matches_appended(pairs: list[tuple[str, str, int, dict[str, object]]]) -> None:
    with tempfile.TemporaryDirectory() as td:
        store = LineageV2Store(Path(td) / "v2", hmac_key=_TEST_KEY)
        for task_id, run_id, ts, p in pairs:
            pref, cb = _make_pair(task_id, run_id, ts, p)
            store.append(pref, cb)
        per_task: dict[str, int] = {}
        for t, _, _, _ in pairs:
            per_task[t] = per_task.get(t, 0) + 1
        total_replay = sum(len(store.replay(t)) for t in per_task)
        assert total_replay == len(pairs)
        assert store.verify().ok


# ---------------------------------------------------------------------------
# Property 8 - empty store always verifies
# ---------------------------------------------------------------------------


@given(seed=st.integers(min_value=0, max_value=1000))
@settings(max_examples=15, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
def test_property_empty_store_verifies(seed: int) -> None:
    _ = seed
    with tempfile.TemporaryDirectory() as td:
        store = LineageV2Store(Path(td) / "v2", hmac_key=_TEST_KEY)
        r = store.verify()
        assert r.ok
        assert r.parent_count == 0
        assert r.child_count == 0


# ---------------------------------------------------------------------------
# Property 9 - sigstore export is JSON-serialisable for any well-formed task
# ---------------------------------------------------------------------------


@given(
    n=st.integers(min_value=1, max_value=5),
    payloads=st.lists(_payload(), min_size=1, max_size=5),
)
@settings(max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
def test_property_sigstore_json_serialisable(n: int, payloads: list[dict[str, object]]) -> None:
    with tempfile.TemporaryDirectory() as td:
        store = LineageV2Store(Path(td) / "v2", hmac_key=_TEST_KEY)
        for i in range(min(n, len(payloads))):
            pref, cb = _make_pair("T", f"r{i}", i, payloads[i])
            store.append(pref, cb)
        atts = store.export_sigstore("T")
        s = json.dumps(atts)
        assert json.loads(s) == atts


# ---------------------------------------------------------------------------
# Property 10 - wrong key always fails verify on non-empty log
# ---------------------------------------------------------------------------


@given(payload=_payload(), wrong_key=st.binary(min_size=8, max_size=64))
@settings(max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
def test_property_wrong_key_fails(payload: dict[str, object], wrong_key: bytes) -> None:
    if wrong_key == _TEST_KEY:
        return
    with tempfile.TemporaryDirectory() as td:
        good = LineageV2Store(Path(td) / "v2", hmac_key=_TEST_KEY)
        pref, cb = _make_pair("T", "r", 1, payload)
        good.append(pref, cb)
        bad = LineageV2Store(Path(td) / "v2", hmac_key=wrong_key)
        r = bad.verify()
        assert not r.ok


# ---------------------------------------------------------------------------
# Property 11 - hmac is hex-only / fixed length
# ---------------------------------------------------------------------------


@given(payload=_payload())
@settings(max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
def test_property_parent_hmac_is_sha256_hex(payload: dict[str, object]) -> None:
    with tempfile.TemporaryDirectory() as td:
        store = LineageV2Store(Path(td) / "v2", hmac_key=_TEST_KEY)
        pref, cb = _make_pair("T", "r", 1, payload)
        _, hmac = store.append(pref, cb)
        assert len(hmac) == 64
        int(hmac, 16)


# ---------------------------------------------------------------------------
# Property 12 - parent prev_hmac equals previous line's hmac
# ---------------------------------------------------------------------------


@given(
    runs=st.lists(_IDENT, min_size=2, max_size=8, unique=True),
)
@settings(max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
def test_property_parent_chain_linkage(runs: list[str]) -> None:
    with tempfile.TemporaryDirectory() as td:
        store = LineageV2Store(Path(td) / "v2", hmac_key=_TEST_KEY)
        for i, r in enumerate(runs):
            pref, cb = _make_pair("T", r, i, {"i": i})
            store.append(pref, cb)
        refs = list(store.iter_parent_refs())
        for i in range(1, len(refs)):
            assert refs[i].prev_hmac == refs[i - 1].hmac


# ---------------------------------------------------------------------------
# Stress sanity - many appends still verify
# ---------------------------------------------------------------------------


@given(n=st.integers(min_value=5, max_value=25))
@settings(max_examples=10, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
def test_property_many_appends_verify(n: int) -> None:
    with tempfile.TemporaryDirectory() as td:
        store = LineageV2Store(Path(td) / "v2", hmac_key=_TEST_KEY)
        for i in range(n):
            pref, cb = _make_pair("T", f"r{i}", i, {"i": i})
            store.append(pref, cb)
        r = store.verify()
        assert r.ok
        assert r.parent_count == n


# ---------------------------------------------------------------------------
# Property 13 - replay output equals iter_parent_refs filtered by task_id
# ---------------------------------------------------------------------------


@given(
    pairs=st.lists(
        st.tuples(_IDENT, _IDENT, st.integers(0, 1000), _payload()),
        min_size=1,
        max_size=8,
        unique_by=lambda t: t[1],
    )
)
@settings(max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
def test_property_replay_equals_filtered_iter(
    pairs: list[tuple[str, str, int, dict[str, object]]],
) -> None:
    with tempfile.TemporaryDirectory() as td:
        store = LineageV2Store(Path(td) / "v2", hmac_key=_TEST_KEY)
        for task_id, run_id, ts, p in pairs:
            pref, cb = _make_pair(task_id, run_id, ts, p)
            store.append(pref, cb)
        task_ids = {t for t, _, _, _ in pairs}
        for tid in task_ids:
            replayed = [r.child_run_id for r, _ in store.replay(tid)]
            via_iter = [ref.child_run_id for ref in store.iter_parent_refs() if ref.task_id == tid]
            assert replayed == via_iter


# ---------------------------------------------------------------------------
# Property 14 - parent_count matches len(iter_parent_refs)
# ---------------------------------------------------------------------------


@given(n=st.integers(min_value=0, max_value=12))
@settings(max_examples=15, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
def test_property_parent_count_matches_iter(n: int) -> None:
    with tempfile.TemporaryDirectory() as td:
        store = LineageV2Store(Path(td) / "v2", hmac_key=_TEST_KEY)
        for i in range(n):
            pref, cb = _make_pair("T", f"r{i}", i, {"i": i})
            store.append(pref, cb)
        r = store.verify()
        assert r.parent_count == len(list(store.iter_parent_refs()))


# ---------------------------------------------------------------------------
# Property 15 - child file count == distinct child_sha values
# ---------------------------------------------------------------------------


@given(n=st.integers(min_value=1, max_value=10))
@settings(max_examples=15, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
def test_property_child_file_count_matches_unique_shas(n: int) -> None:
    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "v2"
        store = LineageV2Store(root, hmac_key=_TEST_KEY)
        unique_shas: set[str] = set()
        for i in range(n):
            pref, cb = _make_pair("T", f"r{i}", i, {"i": i})
            sha, _ = store.append(pref, cb)
            unique_shas.add(sha)
        files = list((root / "children").iterdir())
        assert len(files) == len(unique_shas)


# ---------------------------------------------------------------------------
# Property 16 - export_jsonl line count matches replay structure
# ---------------------------------------------------------------------------


@given(
    pairs=st.lists(
        st.tuples(_IDENT, st.integers(0, 1000), _payload()),
        min_size=1,
        max_size=8,
        unique_by=lambda t: t[0],
    )
)
@settings(max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
def test_property_export_jsonl_line_count(pairs: list[tuple[str, int, dict[str, object]]]) -> None:
    with tempfile.TemporaryDirectory() as td:
        store = LineageV2Store(Path(td) / "v2", hmac_key=_TEST_KEY)
        for run_id, ts, p in pairs:
            pref, cb = _make_pair("T", run_id, ts, p)
            store.append(pref, cb)
        text = store.export_jsonl("T")
        if not text:
            return
        lines = text.strip().split("\n")
        timeline = store.replay("T")
        expected = sum(1 + len(bodies) for _, bodies in timeline)
        assert len(lines) == expected


# ---------------------------------------------------------------------------
# Property 17 - sigstore attestation count == replay parent count
# ---------------------------------------------------------------------------


@given(n=st.integers(min_value=1, max_value=8))
@settings(max_examples=15, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
def test_property_sigstore_attestation_count(n: int) -> None:
    with tempfile.TemporaryDirectory() as td:
        store = LineageV2Store(Path(td) / "v2", hmac_key=_TEST_KEY)
        for i in range(n):
            pref, cb = _make_pair("T", f"r{i}", i, {"i": i})
            store.append(pref, cb)
        atts = store.export_sigstore("T")
        assert len(atts) == len(store.replay("T"))


# ---------------------------------------------------------------------------
# Property 18 - re-stamping is idempotent (same body -> same hmac)
# ---------------------------------------------------------------------------


@given(payload=_payload())
@settings(max_examples=30, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
def test_property_stamp_idempotent(payload: dict[str, object]) -> None:
    from bernstein.core.lineage.v2_store import _child_body_for_hmac, _compute_hmac

    body = ChildBody(
        v=LINEAGE_V2_ENTRY_VERSION,
        task_id="t",
        child_run_id="r",
        seq=0,
        kind="x",
        payload=payload.copy(),
        ts_ns=0,
        prev_hmac="",
        hmac="",
    )
    h1 = _compute_hmac(_TEST_KEY, _child_body_for_hmac(body))
    h2 = _compute_hmac(_TEST_KEY, _child_body_for_hmac(body))
    assert h1 == h2


# ---------------------------------------------------------------------------
# Property 19 - replay of empty task is []
# ---------------------------------------------------------------------------


@given(task_id=_IDENT, other_task_id=_IDENT)
@settings(max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
def test_property_replay_empty_task_returns_empty(task_id: str, other_task_id: str) -> None:
    if task_id == other_task_id:
        return
    with tempfile.TemporaryDirectory() as td:
        store = LineageV2Store(Path(td) / "v2", hmac_key=_TEST_KEY)
        pref, cb = _make_pair(other_task_id, "r", 1, {})
        store.append(pref, cb)
        assert store.replay(task_id) == []


# ---------------------------------------------------------------------------
# Property 20 - orphan child file detection
# ---------------------------------------------------------------------------


@given(orphan_marker=st.text(_SAFE_CHARS, min_size=1, max_size=16))
@settings(max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture], deadline=None)
def test_property_orphan_child_file_detected(orphan_marker: str) -> None:
    import hashlib

    with tempfile.TemporaryDirectory() as td:
        root = Path(td) / "v2"
        store = LineageV2Store(root, hmac_key=_TEST_KEY)
        # Plant a fake child file with no parent referencing it.
        sha_hex = hashlib.sha256(orphan_marker.encode("utf-8")).hexdigest()
        orphan_path = root / "children" / f"{sha_hex}.jsonl"
        orphan_path.parent.mkdir(parents=True, exist_ok=True)
        orphan_path.write_text("{}\n")
        r = store.verify()
        assert not r.ok
        assert any("orphan" in f for f in r.failures)
