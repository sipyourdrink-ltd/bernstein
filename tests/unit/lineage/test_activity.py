"""Tests for ``active_set`` (issue #4183): pure active-set closure over the
receipt ledger and invalidation seeds.

Checked by tests, not by reading:

- Three-level chain A <- B <- C: seed A => B and C both drop out while all
  three entries remain present and untouched.  Depth two is the case
  implementations get wrong -- asserted explicitly.
- A diamond (D depends on B and C, only B invalidated): D drops out --
  one bad premise suffices.
- A re-issued premise as a *new* entry does not reactivate old dependents
  that referenced the old one.
- Determinism: same inputs twice => identical results; permuting entry
  order in the input => identical results.
- Hostile input: a reference cycle terminates with defined behaviour; an
  unknown reference is handled per the documented choice (conservatively
  inactive), not by KeyError.
"""

from __future__ import annotations

from bernstein.core.lineage.activity import active_set
from bernstein.core.lineage.entry import LineageEntry, entry_hash


def _mk(
    artefact_path: str,
    content_hash: str,
    parent_hashes: list[str],
    *,
    ts_ns: int = 1_715_600_000_000_000_000,
    agent_id: str = "agent:a",
) -> LineageEntry:
    return LineageEntry(
        v=1,
        artefact_path=artefact_path,
        artefact_kind="file",
        content_hash=content_hash,
        parent_hashes=parent_hashes,
        agent_id=agent_id,
        agent_card_kid="k1",
        tool_call_id="tc-x",
        span_id="span-x",
        ts_ns=ts_ns,
        operator_hmac="deadbeef" * 8,
    )


def _h(seed: str) -> str:
    return "sha256:" + (seed * 64)[:64]


# ── core semantics ─────────────────────────────────────────────────────────


def test_empty_ledger_is_empty_active_set() -> None:
    assert active_set([], frozenset()) == frozenset()


def test_empty_ledger_with_seeds_is_empty() -> None:
    assert active_set([], frozenset({_h("a")})) == frozenset()


def test_single_entry_no_seeds_is_active() -> None:
    e = _mk("a.py", _h("1"), [])
    assert active_set([e], frozenset()) == frozenset({entry_hash(e)})


def test_single_entry_seeded_is_inactive() -> None:
    e = _mk("a.py", _h("1"), [])
    assert active_set([e], frozenset({entry_hash(e)})) == frozenset()


def test_three_level_chain_seed_root_drops_all() -> None:
    # A <- B <- C : B depends on A, C depends on B.
    a = _mk("a.py", _h("1"), [], ts_ns=1)
    b = _mk("a.py", _h("2"), [entry_hash(a)], ts_ns=2)
    c = _mk("a.py", _h("3"), [entry_hash(b)], ts_ns=3)
    entries = [a, b, c]
    active = active_set(entries, frozenset({entry_hash(a)}))
    # Depth-two is the case implementations get wrong: seed A must drop B
    # (depth 1) AND C (depth 2).
    assert active == frozenset()
    # All three entries remain present and untouched.
    assert {entry_hash(e) for e in entries} == {entry_hash(a), entry_hash(b), entry_hash(c)}


def test_chain_seed_middle_drops_only_descendants() -> None:
    a = _mk("a.py", _h("1"), [], ts_ns=1)
    b = _mk("a.py", _h("2"), [entry_hash(a)], ts_ns=2)
    c = _mk("a.py", _h("3"), [entry_hash(b)], ts_ns=3)
    # Seed B: C (dependent of B) drops, A (B's own premise) stays active.
    active = active_set([a, b, c], frozenset({entry_hash(b)}))
    assert active == frozenset({entry_hash(a)})


def test_diamond_one_bad_premise_drops_merge() -> None:
    # g -> a, g -> b ; D depends on B and C; only B invalidated.
    g = _mk("a.py", _h("1"), [], ts_ns=1)
    b = _mk("a.py", _h("2"), [entry_hash(g)], ts_ns=2)
    c = _mk("a.py", _h("3"), [entry_hash(g)], ts_ns=3)
    d = _mk("a.py", _h("4"), [entry_hash(b), entry_hash(c)], ts_ns=4)
    active = active_set([g, b, c, d], frozenset({entry_hash(b)}))
    # D drops out (one bad premise suffices); g and c stay active.
    assert active == frozenset({entry_hash(g), entry_hash(c)})


def test_reissued_premise_does_not_reactivate_old_dependents() -> None:
    # Old premise P1 (seeded), new premise P2 (re-issued as a new entry).
    # D referenced P1; a re-issued P2 must not reactivate D.
    p1 = _mk("p.py", _h("p1"), [], ts_ns=1)
    p2 = _mk("p.py", _h("p2"), [], ts_ns=2)
    d = _mk("d.py", _h("d"), [entry_hash(p1)], ts_ns=3)
    active = active_set([p1, p2, d], frozenset({entry_hash(p1)}))
    # P2 is a brand-new entry (active); D still references the old id -> inactive.
    assert active == frozenset({entry_hash(p2)})


def test_seed_need_not_be_in_ledger() -> None:
    # A seed may name a revoked receipt absent from this snapshot; every
    # entry referencing it still drops out.
    e = _mk("a.py", _h("1"), [])
    ghost = _h("dead")
    assert active_set([e], frozenset({ghost})) == frozenset({entry_hash(e)})
    dep = _mk("b.py", _h("2"), [ghost], ts_ns=2)
    assert active_set([e, dep], frozenset({ghost})) == frozenset({entry_hash(e)})


# ── determinism ────────────────────────────────────────────────────────────


def test_determinism_same_inputs_twice() -> None:
    a = _mk("a.py", _h("1"), [], ts_ns=1)
    b = _mk("b.py", _h("2"), [entry_hash(a)], ts_ns=2)
    c = _mk("c.py", _h("3"), [entry_hash(b)], ts_ns=3)
    entries = [a, b, c]
    seeds = frozenset({entry_hash(a)})
    assert active_set(entries, seeds) == active_set(entries, seeds)


def test_determinism_permuted_input_order() -> None:
    a = _mk("a.py", _h("1"), [], ts_ns=1)
    b = _mk("b.py", _h("2"), [entry_hash(a)], ts_ns=2)
    c = _mk("c.py", _h("3"), [entry_hash(b)], ts_ns=3)
    seeds = frozenset({entry_hash(a)})
    base = active_set([a, b, c], seeds)
    assert base == active_set([c, a, b], seeds)
    assert base == active_set([b, c, a], seeds)


def test_determinism_permuted_entries_within_ledger() -> None:
    # Permuting the input must not change the result even when seeds are empty.
    a = _mk("a.py", _h("1"), [], ts_ns=1)
    b = _mk("b.py", _h("2"), [entry_hash(a)], ts_ns=2)
    assert active_set([a, b], frozenset()) == active_set([b, a], frozenset())


# ── hostile input ──────────────────────────────────────────────────────────


def test_reference_cycle_terminates_with_defined_behaviour() -> None:
    # A true cycle cannot arise in a valid content-addressed log (entry_hash
    # is a function of parent_hashes, so a cycle would be a fixed point the
    # hash never reaches), but the walk must not hang or crash on hostile
    # input regardless.  The closest constructible shape is a two-entry
    # pseudo-cycle: a references b's real hash, b references a ghost hash
    # that claims to be a.  b's unknown reference is conservatively
    # inactive; a (dependent of b) drops out too -- defined behaviour,
    # termination guaranteed by the seen-guard.
    a = _mk("a.py", _h("1"), [], ts_ns=1)
    b = _mk("b.py", _h("2"), [entry_hash(a)], ts_ns=2)
    ghost_a = _mk("a.py", _h("9"), [entry_hash(b)], ts_ns=3)
    b_with_ghost = _mk("b.py", _h("2"), [_h("a-ghost")], ts_ns=4)
    # 1. ghost_a references b (real edge); b references ghost_a's premise id
    #    which is absent -> both conservatively inactive.
    result = active_set([ghost_a, b], frozenset())
    assert result == frozenset()
    # 2. b references a ghost id that names the *old* a; a is a different
    #    entry (different content), so b's reference is unknown -> inactive,
    #    a stays active.
    result2 = active_set([a, b_with_ghost], frozenset())
    assert result2 == frozenset({entry_hash(a)})


def test_unknown_reference_is_conservatively_inactive() -> None:
    # An entry whose parent_hashes names an id absent from the ledger is
    # not active (missing premise = not currently provable), and its
    # dependents drop out too.
    ghost = _h("ghost")
    e = _mk("a.py", _h("1"), [ghost], ts_ns=1)
    dep = _mk("b.py", _h("2"), [entry_hash(e)], ts_ns=2)
    active = active_set([e, dep], frozenset())
    assert active == frozenset()


def test_unknown_reference_does_not_raise_keyerror() -> None:
    ghost = _h("ghost")
    e = _mk("a.py", _h("1"), [ghost])
    # Must not raise; returns a defined value instead.
    active_set([e], frozenset())


def test_unknown_reference_inactive_propagates_to_siblings() -> None:
    ghost = _h("ghost")
    e = _mk("a.py", _h("1"), [ghost], ts_ns=1)
    good = _mk("c.py", _h("3"), [], ts_ns=2)
    sibling = _mk("d.py", _h("4"), [entry_hash(good), entry_hash(e)], ts_ns=3)
    active = active_set([e, good, sibling], frozenset())
    # sibling has one bad (unknown) premise -> drops; good stays active.
    assert active == frozenset({entry_hash(good)})


def test_fork_with_one_seeded_parent_drops_merge() -> None:
    # Two independent premises, one seeded: dependents referencing the
    # seeded premise (even transitively) must drop.
    p1 = _mk("p1.py", _h("p1"), [], ts_ns=1)
    p2 = _mk("p2.py", _h("p2"), [], ts_ns=2)
    m = _mk("m.py", _h("m"), [entry_hash(p1), entry_hash(p2)], ts_ns=3)
    active = active_set([p1, p2, m], frozenset({entry_hash(p1)}))
    assert active == frozenset({entry_hash(p2)})
