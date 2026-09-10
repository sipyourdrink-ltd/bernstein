"""Tests for `compute_tips` and `detect_forks` (ADR-009 §6.1)."""

from __future__ import annotations

from bernstein.core.lineage.entry import LineageEntry, entry_hash
from bernstein.core.lineage.tips import Fork, compute_tips, detect_forks


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


# ── compute_tips ────────────────────────────────────────────────────────────


def test_empty_log_returns_empty_dict() -> None:
    assert compute_tips([]) == {}


def test_single_entry_one_open_tip() -> None:
    e = _mk("a.py", _h("1"), [])
    tips = compute_tips([e])
    assert tips == {"a.py": {"open": [entry_hash(e)], "merged": []}}


def test_linear_chain_only_head_is_open() -> None:
    g = _mk("a.py", _h("1"), [], ts_ns=1)
    c1 = _mk("a.py", _h("2"), [entry_hash(g)], ts_ns=2)
    c2 = _mk("a.py", _h("3"), [entry_hash(c1)], ts_ns=3)
    tips = compute_tips([g, c1, c2])
    assert tips["a.py"]["open"] == [entry_hash(c2)]
    assert tips["a.py"]["merged"] == []


def test_simple_fork_two_open_tips() -> None:
    g = _mk("a.py", _h("1"), [], ts_ns=1)
    f1 = _mk("a.py", _h("2"), [entry_hash(g)], ts_ns=2, agent_id="agent:a")
    f2 = _mk("a.py", _h("3"), [entry_hash(g)], ts_ns=3, agent_id="agent:b")
    tips = compute_tips([g, f1, f2])
    assert set(tips["a.py"]["open"]) == {entry_hash(f1), entry_hash(f2)}
    assert tips["a.py"]["merged"] == []


def test_merge_resolves_fork() -> None:
    g = _mk("a.py", _h("1"), [], ts_ns=1)
    f1 = _mk("a.py", _h("2"), [entry_hash(g)], ts_ns=2)
    f2 = _mk("a.py", _h("3"), [entry_hash(g)], ts_ns=3)
    m = _mk("a.py", _h("4"), [entry_hash(f1), entry_hash(f2)], ts_ns=4)
    tips = compute_tips([g, f1, f2, m])
    assert tips["a.py"]["open"] == [entry_hash(m)]
    assert set(tips["a.py"]["merged"]) == {entry_hash(f1), entry_hash(f2)}


def test_per_artefact_isolation() -> None:
    a = _mk("a.py", _h("1"), [])
    b = _mk("b.py", _h("2"), [])
    tips = compute_tips([a, b])
    assert set(tips.keys()) == {"a.py", "b.py"}
    assert tips["a.py"]["open"] == [entry_hash(a)]
    assert tips["b.py"]["open"] == [entry_hash(b)]


def test_diamond_shape() -> None:
    # g → a, g → b ; m: parents [a, b] ; c: parent m
    g = _mk("a.py", _h("1"), [], ts_ns=1)
    a = _mk("a.py", _h("2"), [entry_hash(g)], ts_ns=2)
    b = _mk("a.py", _h("3"), [entry_hash(g)], ts_ns=3)
    m = _mk("a.py", _h("4"), [entry_hash(a), entry_hash(b)], ts_ns=4)
    c = _mk("a.py", _h("5"), [entry_hash(m)], ts_ns=5)
    tips = compute_tips([g, a, b, m, c])
    assert tips["a.py"]["open"] == [entry_hash(c)]


def test_n_way_fork_then_merge() -> None:
    g = _mk("a.py", _h("0"), [], ts_ns=1)
    children = [_mk("a.py", _h(str(i)), [entry_hash(g)], ts_ns=10 + i) for i in range(1, 6)]
    m = _mk("a.py", _h("9"), [entry_hash(c) for c in children], ts_ns=100)
    tips = compute_tips([g, *children, m])
    assert tips["a.py"]["open"] == [entry_hash(m)]
    assert set(tips["a.py"]["merged"]) == {entry_hash(c) for c in children}


# ── detect_forks ────────────────────────────────────────────────────────────


def test_detect_forks_empty() -> None:
    assert detect_forks([]) == []


def test_detect_forks_linear_no_fork() -> None:
    g = _mk("a.py", _h("1"), [])
    c = _mk("a.py", _h("2"), [entry_hash(g)])
    assert detect_forks([g, c]) == []


def test_detect_forks_simple() -> None:
    g = _mk("a.py", _h("1"), [], ts_ns=1)
    f1 = _mk("a.py", _h("2"), [entry_hash(g)], ts_ns=2)
    f2 = _mk("a.py", _h("3"), [entry_hash(g)], ts_ns=3)
    forks = detect_forks([g, f1, f2])
    assert len(forks) == 1
    fork = forks[0]
    assert isinstance(fork, Fork)
    assert fork.artefact_path == "a.py"
    assert fork.parent_hash == entry_hash(g)
    assert set(fork.child_hashes) == {entry_hash(f1), entry_hash(f2)}


def test_detect_forks_criss_cross() -> None:
    # Two parents, each have two children pointing to them (not really fork
    # in the §6.1 sense unless same parent + different content)
    # Here we model a real criss-cross: two independent forks on same artefact.
    g1 = _mk("a.py", _h("1"), [], ts_ns=1)
    f1 = _mk("a.py", _h("2"), [entry_hash(g1)], ts_ns=2)
    f2 = _mk("a.py", _h("3"), [entry_hash(g1)], ts_ns=3)
    m = _mk("a.py", _h("4"), [entry_hash(f1), entry_hash(f2)], ts_ns=4)
    f3 = _mk("a.py", _h("5"), [entry_hash(m)], ts_ns=5)
    f4 = _mk("a.py", _h("6"), [entry_hash(m)], ts_ns=6)
    forks = detect_forks([g1, f1, f2, m, f3, f4])
    parents = {f.parent_hash for f in forks}
    assert entry_hash(g1) in parents
    assert entry_hash(m) in parents
    assert len(forks) == 2


def test_detect_forks_n_way() -> None:
    g = _mk("a.py", _h("0"), [], ts_ns=1)
    children = [_mk("a.py", _h(str(i)), [entry_hash(g)], ts_ns=10 + i) for i in range(1, 6)]
    forks = detect_forks([g, *children])
    assert len(forks) == 1
    assert len(forks[0].child_hashes) == 5


def test_one_child_is_never_a_fork_whatever_its_content() -> None:
    """A single child cannot fork, and the content check alone is what says so.

    `detect_forks` used to guard `len(children) < 2` before comparing content hashes. That guard
    could never decide anything: one child contributes one content hash, so the distinct-content
    check below it already rejects the case. The mutation gate reported it as an unkillable
    survivor for exactly that reason -- there was no observable difference to write a test
    against. This pins the property the removal rests on (#5595 measurement).
    """
    g = _mk("a.py", _h("1"), [], ts_ns=1)
    only_child = _mk("a.py", _h("2"), [entry_hash(g)], ts_ns=2)
    assert detect_forks([g, only_child]) == []


def test_fork_requires_distinct_content() -> None:
    # Two entries with same parent AND same content_hash → NOT a fork (idempotent re-record)
    g = _mk("a.py", _h("1"), [], ts_ns=1)
    f1 = _mk("a.py", _h("2"), [entry_hash(g)], ts_ns=2, agent_id="agent:a")
    f2 = _mk("a.py", _h("2"), [entry_hash(g)], ts_ns=3, agent_id="agent:b")
    assert detect_forks([g, f1, f2]) == []
