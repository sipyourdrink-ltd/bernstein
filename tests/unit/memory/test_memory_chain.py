"""Tests for :mod:`bernstein.core.memory.chain` -- the tamper-evident,
content-addressed, actor-attributed memory write chain (issue #2298).

Each memory write becomes an append-only chained record::

    entry_hash = H(prev_hash, source_hash, actor, claim, model, timestamp, ...)

HMAC-tagged with the audit-chain key and anchored to a lineage-spine
entry via ``source_hash``. These tests pin the five acceptance criteria:

* AC1 - every write produces a chained, actor-attributed, HMAC-tagged
  entry.
* AC2 - ``verify`` detects any post-hoc edit of a stored fact.
* AC3 - ``why`` returns the exact originating run id and step for a fact.
* AC4 - ``forget`` appends a signed tombstone; the original entry and
  the chain stay intact and verifiable.
* AC5 - each record's ``source_hash`` resolves to a real lineage-spine
  entry.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.lineage.spine import LineageSpine
from bernstein.core.memory.chain import (
    MemoryChain,
    MemoryChainStatus,
    MemoryScope,
)

_KEY = b"k" * 32


def _make_spine(tmp_path: Path, run_id: str = "run-1") -> LineageSpine:
    return LineageSpine(tmp_path / ".sdd" / "lineage", run_id=run_id, hmac_key=_KEY)


def _make_chain(tmp_path: Path) -> MemoryChain:
    return MemoryChain(tmp_path / ".sdd" / "memory" / "chain", hmac_key=_KEY)


def _anchor(tmp_path: Path, run_id: str = "run-1", step_id: str = "s1") -> str:
    """Write a real spine entry and return its hash to anchor a memory write."""
    spine = _make_spine(tmp_path, run_id=run_id)
    return spine.record(
        artifact_path="src/pref.py",
        content=b"prefers dark mode",
        actor="agent:worker",
        step_id=step_id,
        model="claude",
        timestamp=1,
    )


# ---------------------------------------------------------------------------
# AC1 - chained, actor-attributed, HMAC-tagged writes
# ---------------------------------------------------------------------------


def test_write_produces_actor_attributed_hmac_tagged_entry(tmp_path: Path) -> None:
    source = _anchor(tmp_path)
    chain = _make_chain(tmp_path)
    entry = chain.write(
        scope=MemoryScope.USER,
        namespace="alex",
        claim="prefers dark mode",
        actor="agent:worker",
        source_hash=source,
        run_id="run-1",
        step_id="s1",
        model="claude",
        timestamp=100,
    )
    assert entry.actor == "agent:worker"
    assert entry.claim == "prefers dark mode"
    assert entry.hmac  # HMAC tag present
    assert entry.entry_hash.startswith("sha256:")
    # First write chains from genesis.
    assert entry.prev_hash == ""


def test_writes_chain_prev_hash_within_a_scope(tmp_path: Path) -> None:
    source = _anchor(tmp_path)
    chain = _make_chain(tmp_path)
    first = chain.write(
        scope=MemoryScope.USER,
        namespace="alex",
        claim="fact one",
        actor="agent:a",
        source_hash=source,
        run_id="run-1",
        step_id="s1",
        model="claude",
        timestamp=1,
    )
    second = chain.write(
        scope=MemoryScope.USER,
        namespace="alex",
        claim="fact two",
        actor="agent:a",
        source_hash=source,
        run_id="run-1",
        step_id="s1",
        model="claude",
        timestamp=2,
    )
    assert second.prev_hash == first.entry_hash


def test_scopes_are_disjoint_namespaces(tmp_path: Path) -> None:
    source = _anchor(tmp_path)
    chain = _make_chain(tmp_path)
    user_entry = chain.write(
        scope=MemoryScope.USER,
        namespace="alex",
        claim="user fact",
        actor="agent:a",
        source_hash=source,
        run_id="run-1",
        step_id="s1",
        model="claude",
        timestamp=1,
    )
    app_entry = chain.write(
        scope=MemoryScope.APP,
        namespace="alex",
        claim="app fact",
        actor="agent:a",
        source_hash=source,
        run_id="run-1",
        step_id="s1",
        model="claude",
        timestamp=2,
    )
    # Different scope chains: each starts from genesis independently.
    assert user_entry.prev_hash == ""
    assert app_entry.prev_hash == ""
    assert user_entry.entry_hash != app_entry.entry_hash


def test_four_identity_scopes_supported(tmp_path: Path) -> None:
    assert {s.value for s in MemoryScope} == {"user", "agent", "run", "app"}


# ---------------------------------------------------------------------------
# AC2 - verify detects post-hoc edits
# ---------------------------------------------------------------------------


def test_verify_ok_for_intact_chain(tmp_path: Path) -> None:
    source = _anchor(tmp_path)
    chain = _make_chain(tmp_path)
    chain.write(
        scope=MemoryScope.USER,
        namespace="alex",
        claim="prefers dark mode",
        actor="agent:worker",
        source_hash=source,
        run_id="run-1",
        step_id="s1",
        model="claude",
        timestamp=1,
    )
    spine_root = tmp_path / ".sdd" / "lineage"
    result = chain.verify(MemoryScope.USER, "alex", spine_root=spine_root)
    assert result.status is MemoryChainStatus.OK
    assert result.count == 1


def test_verify_empty_chain_is_no_entries(tmp_path: Path) -> None:
    chain = _make_chain(tmp_path)
    spine_root = tmp_path / ".sdd" / "lineage"
    result = chain.verify(MemoryScope.USER, "nobody", spine_root=spine_root)
    assert result.status is MemoryChainStatus.NO_ENTRIES


def test_verify_detects_edited_claim(tmp_path: Path) -> None:
    source = _anchor(tmp_path)
    chain = _make_chain(tmp_path)
    chain.write(
        scope=MemoryScope.USER,
        namespace="alex",
        claim="prefers dark mode",
        actor="agent:worker",
        source_hash=source,
        run_id="run-1",
        step_id="s1",
        model="claude",
        timestamp=1,
    )
    path = chain.chain_path(MemoryScope.USER, "alex")
    raw = path.read_text(encoding="utf-8")
    tampered = raw.replace("prefers dark mode", "prefers light mode")
    assert tampered != raw
    path.write_text(tampered, encoding="utf-8")

    spine_root = tmp_path / ".sdd" / "lineage"
    result = chain.verify(MemoryScope.USER, "alex", spine_root=spine_root)
    assert result.status is MemoryChainStatus.TAMPERED
    assert result.errors


def test_verify_detects_flipped_hmac_byte(tmp_path: Path) -> None:
    source = _anchor(tmp_path)
    chain = _make_chain(tmp_path)
    entry = chain.write(
        scope=MemoryScope.USER,
        namespace="alex",
        claim="prefers dark mode",
        actor="agent:worker",
        source_hash=source,
        run_id="run-1",
        step_id="s1",
        model="claude",
        timestamp=1,
    )
    path = chain.chain_path(MemoryScope.USER, "alex")
    raw = path.read_text(encoding="utf-8")
    good = entry.hmac
    bad = ("0" if good[0] != "0" else "1") + good[1:]
    path.write_text(raw.replace(good, bad), encoding="utf-8")

    spine_root = tmp_path / ".sdd" / "lineage"
    result = chain.verify(MemoryScope.USER, "alex", spine_root=spine_root)
    assert result.status is MemoryChainStatus.TAMPERED


# ---------------------------------------------------------------------------
# AC3 - why returns originating run id and step
# ---------------------------------------------------------------------------


def test_why_returns_originating_run_and_step(tmp_path: Path) -> None:
    source = _anchor(tmp_path, run_id="run-42", step_id="step-7")
    chain = _make_chain(tmp_path)
    chain.write(
        scope=MemoryScope.USER,
        namespace="alex",
        claim="prefers dark mode",
        actor="agent:worker",
        source_hash=source,
        run_id="run-42",
        step_id="step-7",
        model="claude",
        timestamp=1,
    )
    spine_root = tmp_path / ".sdd" / "lineage"
    origin = chain.why(
        "prefers dark mode",
        scope=MemoryScope.USER,
        namespace="alex",
        spine_root=spine_root,
    )
    assert origin is not None
    assert origin.run_id == "run-42"
    assert origin.step_id == "step-7"
    # The origin is corroborated by the spine entry the source_hash anchors.
    assert origin.source_hash == source


def test_why_returns_none_for_unknown_fact(tmp_path: Path) -> None:
    source = _anchor(tmp_path)
    chain = _make_chain(tmp_path)
    chain.write(
        scope=MemoryScope.USER,
        namespace="alex",
        claim="prefers dark mode",
        actor="agent:worker",
        source_hash=source,
        run_id="run-1",
        step_id="s1",
        model="claude",
        timestamp=1,
    )
    spine_root = tmp_path / ".sdd" / "lineage"
    origin = chain.why(
        "never stored",
        scope=MemoryScope.USER,
        namespace="alex",
        spine_root=spine_root,
    )
    assert origin is None


# ---------------------------------------------------------------------------
# AC4 - forget appends a tombstone; original + chain stay verifiable
# ---------------------------------------------------------------------------


def test_forget_appends_tombstone_without_deleting_original(tmp_path: Path) -> None:
    source = _anchor(tmp_path)
    chain = _make_chain(tmp_path)
    original = chain.write(
        scope=MemoryScope.USER,
        namespace="alex",
        claim="prefers dark mode",
        actor="agent:worker",
        source_hash=source,
        run_id="run-1",
        step_id="s1",
        model="claude",
        timestamp=1,
    )
    tombstone = chain.forget(
        original.entry_hash,
        scope=MemoryScope.USER,
        namespace="alex",
        actor="agent:worker",
        source_hash=source,
        run_id="run-1",
        step_id="s2",
        model="claude",
        timestamp=2,
    )
    assert tombstone.tombstone_of == original.entry_hash
    assert tombstone.hmac
    # Original entry bytes remain present and unedited.
    entries = list(chain.iter_entries(MemoryScope.USER, "alex"))
    assert len(entries) == 2
    assert entries[0].entry_hash == original.entry_hash
    assert entries[0].claim == "prefers dark mode"
    # Chain still verifies after the tombstone append.
    spine_root = tmp_path / ".sdd" / "lineage"
    result = chain.verify(MemoryScope.USER, "alex", spine_root=spine_root)
    assert result.status is MemoryChainStatus.OK
    assert result.count == 2


def test_forgotten_facts_are_marked_but_provable(tmp_path: Path) -> None:
    source = _anchor(tmp_path)
    chain = _make_chain(tmp_path)
    original = chain.write(
        scope=MemoryScope.USER,
        namespace="alex",
        claim="prefers dark mode",
        actor="agent:worker",
        source_hash=source,
        run_id="run-1",
        step_id="s1",
        model="claude",
        timestamp=1,
    )
    chain.forget(
        original.entry_hash,
        scope=MemoryScope.USER,
        namespace="alex",
        actor="agent:worker",
        source_hash=source,
        run_id="run-1",
        step_id="s2",
        model="claude",
        timestamp=2,
    )
    forgotten = chain.forgotten_hashes(MemoryScope.USER, "alex")
    assert original.entry_hash in forgotten


# ---------------------------------------------------------------------------
# AC5 - source_hash resolves to a real spine entry
# ---------------------------------------------------------------------------


def test_verify_flags_dangling_source_hash(tmp_path: Path) -> None:
    # Anchor to a spine hash that was never written to the spine.
    chain = _make_chain(tmp_path)
    chain.write(
        scope=MemoryScope.USER,
        namespace="alex",
        claim="unanchored fact",
        actor="agent:worker",
        source_hash="sha256:" + "de" * 32,
        run_id="run-1",
        step_id="s1",
        model="claude",
        timestamp=1,
    )
    spine_root = tmp_path / ".sdd" / "lineage"
    result = chain.verify(MemoryScope.USER, "alex", spine_root=spine_root)
    assert result.status is MemoryChainStatus.TAMPERED
    assert any("source_hash" in e for e in result.errors)


def test_verify_accepts_resolvable_source_hash(tmp_path: Path) -> None:
    source = _anchor(tmp_path)
    chain = _make_chain(tmp_path)
    chain.write(
        scope=MemoryScope.USER,
        namespace="alex",
        claim="anchored fact",
        actor="agent:worker",
        source_hash=source,
        run_id="run-1",
        step_id="s1",
        model="claude",
        timestamp=1,
    )
    spine_root = tmp_path / ".sdd" / "lineage"
    result = chain.verify(MemoryScope.USER, "alex", spine_root=spine_root)
    assert result.status is MemoryChainStatus.OK


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_identical_writes_produce_identical_rows(tmp_path: Path) -> None:
    def _run(root: Path) -> bytes:
        spine = LineageSpine(root / ".sdd" / "lineage", run_id="run-1", hmac_key=_KEY)
        src = spine.record(
            artifact_path="src/pref.py",
            content=b"c",
            actor="agent:worker",
            step_id="s1",
            model="claude",
            timestamp=1,
        )
        chain = MemoryChain(root / ".sdd" / "memory" / "chain", hmac_key=_KEY)
        chain.write(
            scope=MemoryScope.USER,
            namespace="alex",
            claim="prefers dark mode",
            actor="agent:worker",
            source_hash=src,
            run_id="run-1",
            step_id="s1",
            model="claude",
            timestamp=100,
        )
        return chain.chain_path(MemoryScope.USER, "alex").read_bytes()

    a = _run(tmp_path / "a")
    b = _run(tmp_path / "b")
    assert a == b


def test_namespace_rejects_path_traversal(tmp_path: Path) -> None:
    chain = _make_chain(tmp_path)
    with pytest.raises(ValueError, match="namespace"):
        chain.write(
            scope=MemoryScope.USER,
            namespace="../escape",
            claim="x",
            actor="a",
            source_hash="sha256:" + "0" * 64,
            run_id="run-1",
            step_id="s1",
            model="claude",
            timestamp=1,
        )


# ---------------------------------------------------------------------------
# Deterministic fold -- the current state of a namespace (issue #2914)
# ---------------------------------------------------------------------------


def test_memory_fold_is_byte_identical_across_readers(tmp_path: Path) -> None:
    """Two independent readers must project the same chain into identical bytes.

    The fold is the answer to "what does this namespace currently say".
    If it is not byte-identical across readers it cannot be diffed,
    hashed, or attested, and an operator comparing two reads of the same
    chain would see spurious drift.
    """
    source = _anchor(tmp_path)
    writer = _make_chain(tmp_path)
    for i, claim in enumerate(("prefers dark mode", "ships on fridays", "uses tabs")):
        writer.write(
            scope=MemoryScope.USER,
            namespace="alex",
            claim=claim,
            actor="agent:worker",
            source_hash=source,
            run_id="run-1",
            step_id=f"s{i}",
            model="claude",
            timestamp=i,
        )

    reader_a = _make_chain(tmp_path)
    reader_b = _make_chain(tmp_path)
    bytes_a = reader_a.fold_bytes(MemoryScope.USER, "alex")
    bytes_b = reader_b.fold_bytes(MemoryScope.USER, "alex")

    assert bytes_a == bytes_b
    assert bytes_a  # a non-empty chain folds to non-empty bytes
    # The fold carries the live claims in append order.
    folded = reader_a.fold(MemoryScope.USER, "alex")
    assert [e.claim for e in folded] == ["prefers dark mode", "ships on fridays", "uses tabs"]


def test_tombstoned_claim_is_absent_from_fold_but_still_in_chain(tmp_path: Path) -> None:
    """Retention-as-append: forgetting removes a claim from the current
    state without removing it from the record.

    The tombstoned write must disappear from ``fold`` (it no longer
    describes what the namespace says) while ``iter_entries`` still
    yields it and ``verify`` still returns OK, so the audit trail of
    what was known-and-when survives the forget.
    """
    source = _anchor(tmp_path)
    chain = _make_chain(tmp_path)
    kept = chain.write(
        scope=MemoryScope.USER,
        namespace="alex",
        claim="ships on fridays",
        actor="agent:worker",
        source_hash=source,
        run_id="run-1",
        step_id="s1",
        model="claude",
        timestamp=1,
    )
    dropped = chain.write(
        scope=MemoryScope.USER,
        namespace="alex",
        claim="prefers dark mode",
        actor="agent:worker",
        source_hash=source,
        run_id="run-1",
        step_id="s2",
        model="claude",
        timestamp=2,
    )
    chain.forget(
        dropped.entry_hash,
        scope=MemoryScope.USER,
        namespace="alex",
        actor="agent:worker",
        source_hash=source,
        run_id="run-1",
        step_id="s3",
        model="claude",
        timestamp=3,
    )

    folded = chain.fold(MemoryScope.USER, "alex")
    assert [e.entry_hash for e in folded] == [kept.entry_hash]
    assert dropped.claim not in [e.claim for e in folded]

    # The record itself is untouched: both writes and the tombstone remain.
    entries = list(chain.iter_entries(MemoryScope.USER, "alex"))
    assert len(entries) == 3
    assert dropped.entry_hash in {e.entry_hash for e in entries}
    result = chain.verify(MemoryScope.USER, "alex", spine_root=tmp_path / ".sdd" / "lineage")
    assert result.status is MemoryChainStatus.OK
    assert result.count == 3


def test_fold_of_an_empty_namespace_is_empty_and_still_canonical(tmp_path: Path) -> None:
    """A namespace that stored nothing folds to an empty, still-parseable
    projection rather than raising -- the no-memory regression path."""
    chain = _make_chain(tmp_path)
    assert chain.fold(MemoryScope.USER, "nobody") == ()
    raw = chain.fold_bytes(MemoryScope.USER, "nobody")
    assert json.loads(raw)["entries"] == []
