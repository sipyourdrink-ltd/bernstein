"""FIFO-ordering tests for the replay gateway (issue #1855).

When two distinct keys recorded the same response value, the by-kind FIFO
fallback must still pop responses in recorded order. The old by-value removal
on a by-key hit deleted the wrong by-kind slot, silently desyncing the two
structures. These tests pin the recorded-order contract under duplicate
response values and mixed by-key / by-kind lookups.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.replay import (
    EVENTS_FILENAME,
    GatewayMode,
    ReplayGateway,
    ReplayMissError,
    derive_replay_key,
)


def _sk(caller_key: str) -> str:
    """Scheme-prefixed storage key for a hand-written fixture row."""
    return derive_replay_key(caller_key)


def _write_events(run_dir: Path, rows: list[dict[str, object]]) -> None:
    """Write recorded ``events.jsonl`` rows for a run directory."""
    path = run_dir / EVENTS_FILENAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def _explode() -> object:
    raise AssertionError("replay must not call invoke()")


def test_bykey_hit_does_not_corrupt_bykind_fifo_with_dup_values(tmp_path: Path) -> None:
    """The worked example from issue #1855.

    Record kind=tool: A->R, B->R, C->S. A by-key hit on (tool, B) must not
    remove A's slot from the by-kind FIFO. A later by-kind fallback must then
    still see R (A's recorded slot) then S (C's), in recorded order.
    """
    run_dir = tmp_path / "runs" / "dup-1"
    _write_events(
        run_dir,
        [
            {"seq": 1, "kind": "tool", "key": _sk("A"), "response": "R"},
            {"seq": 2, "kind": "tool", "key": _sk("B"), "response": "R"},
            {"seq": 3, "kind": "tool", "key": _sk("C"), "response": "S"},
        ],
    )

    gw = ReplayGateway("dup-1", tmp_path, mode=GatewayMode.REPLAY)

    # By-key hit on B consumes B's recorded slot (seq 2).
    assert gw.dispatch(kind="tool", key="B", invoke=_explode) == "R"
    # Two by-kind fallbacks must return the remaining recorded slots in
    # seq order: A's R (seq 1), then C's S (seq 3).
    assert gw.dispatch(kind="tool", key="miss-1", invoke=_explode) == "R"
    assert gw.dispatch(kind="tool", key="miss-2", invoke=_explode) == "S"


def test_bykey_hit_steals_wrong_bykind_slot_corrupts_fallback(tmp_path: Path) -> None:
    """Minimal corruption case: by-value removal serves a consumed slot.

    Record kind=tool: A->R (s1), B->S (s2), C->R (s3); by-kind FIFO is
    [R, S, R]. A by-key hit on C (recorded value R at seq 3) must consume
    *C's* slot. With value-equality removal it instead deletes the first R
    (A's seq-1 slot), so the by-kind queue becomes [S, R] = B's S and C's
    *already-consumed* R. Two by-kind fallbacks then return S then R, but
    the only unconsumed recorded slots are A (R) and B (S): the correct
    fallback order is R then S. The second R is a phantom replay of C, which
    was already served - a silent wrong-response substitution.
    """
    run_dir = tmp_path / "runs" / "dup-corrupt"
    _write_events(
        run_dir,
        [
            {"seq": 1, "kind": "tool", "key": _sk("A"), "response": "R"},
            {"seq": 2, "kind": "tool", "key": _sk("B"), "response": "S"},
            {"seq": 3, "kind": "tool", "key": _sk("C"), "response": "R"},
        ],
    )

    gw = ReplayGateway("dup-corrupt", tmp_path, mode=GatewayMode.REPLAY)
    # Consume C by key (its recorded response is R at seq 3).
    assert gw.dispatch(kind="tool", key="C", invoke=_explode) == "R"
    # Remaining unconsumed slots are A (R, seq 1) and B (S, seq 2). A by-kind
    # fallback must return them in recorded seq order: R then S.
    assert gw.dispatch(kind="tool", key="miss-1", invoke=_explode) == "R"
    assert gw.dispatch(kind="tool", key="miss-2", invoke=_explode) == "S"


def test_pure_bykey_replay_with_repeated_values_in_order(tmp_path: Path) -> None:
    """Every call matches its recorded key; duplicate values stay in order."""
    run_dir = tmp_path / "runs" / "dup-2"
    _write_events(
        run_dir,
        [
            {"seq": 1, "kind": "llm", "key": _sk("k1"), "response": "same"},
            {"seq": 2, "kind": "llm", "key": _sk("k2"), "response": "same"},
            {"seq": 3, "kind": "llm", "key": _sk("k1"), "response": "diff"},
        ],
    )

    gw = ReplayGateway("dup-2", tmp_path, mode=GatewayMode.REPLAY)
    # k1 recorded "same" (seq 1) then "diff" (seq 3); k2 recorded "same".
    assert gw.dispatch(kind="llm", key="k1", invoke=_explode) == "same"
    assert gw.dispatch(kind="llm", key="k2", invoke=_explode) == "same"
    assert gw.dispatch(kind="llm", key="k1", invoke=_explode) == "diff"


def test_bykind_fallback_strict_seq_order_with_dups(tmp_path: Path) -> None:
    """A pure by-kind fallback returns responses in recorded seq order."""
    run_dir = tmp_path / "runs" / "dup-3"
    _write_events(
        run_dir,
        [
            {"seq": 1, "kind": "tool", "key": _sk("a"), "response": {"ok": True}},
            {"seq": 2, "kind": "tool", "key": _sk("b"), "response": {"ok": True}},
            {"seq": 3, "kind": "tool", "key": _sk("c"), "response": {"ok": False}},
        ],
    )

    gw = ReplayGateway("dup-3", tmp_path, mode=GatewayMode.REPLAY)
    # All keys miss -> FIFO by seq: ok, ok, not-ok.
    out = [gw.dispatch(kind="tool", key=f"x{i}", invoke=_explode) for i in range(3)]
    assert out == [{"ok": True}, {"ok": True}, {"ok": False}]


def test_mixed_interleave_returns_recorded_position(tmp_path: Path) -> None:
    """Mixing by-key hits and by-kind fallbacks never substitutes a wrong
    recorded response, even with all-identical values."""
    run_dir = tmp_path / "runs" / "dup-4"
    _write_events(
        run_dir,
        [
            {"seq": 1, "kind": "tool", "key": _sk("A"), "response": "R"},
            {"seq": 2, "kind": "tool", "key": _sk("B"), "response": "R"},
            {"seq": 3, "kind": "tool", "key": _sk("C"), "response": "R"},
            {"seq": 4, "kind": "tool", "key": _sk("D"), "response": "R"},
        ],
    )

    gw = ReplayGateway("dup-4", tmp_path, mode=GatewayMode.REPLAY)
    # by-key C (seq 3), then by-kind (seq 1 = A), by-key A would now miss but
    # we instead consume by-kind twice more (seq 2, seq 4). All return "R" and
    # crucially the bucket never under/over-drains.
    assert gw.dispatch(kind="tool", key="C", invoke=_explode) == "R"
    assert gw.dispatch(kind="tool", key="zzz", invoke=_explode) == "R"
    assert gw.dispatch(kind="tool", key="A", invoke=_explode) == "R"
    assert gw.dispatch(kind="tool", key="yyy", invoke=_explode) == "R"
    # Exactly four fixtures recorded; the fifth consumption must miss.
    with pytest.raises(ReplayMissError):
        gw.dispatch(kind="tool", key="extra", invoke=_explode)


def test_replay_is_byte_identical_across_two_runs_with_dups(tmp_path: Path) -> None:
    """Replaying a duplicate-value recording twice yields identical output."""
    run_dir = tmp_path / "runs" / "dup-5"
    rows = [
        {"seq": 1, "kind": "tool", "key": _sk("A"), "response": "R"},
        {"seq": 2, "kind": "tool", "key": _sk("B"), "response": "R"},
        {"seq": 3, "kind": "tool", "key": _sk("C"), "response": "S"},
    ]
    _write_events(run_dir, rows)

    def _drain() -> list[object]:
        gw = ReplayGateway("dup-5", tmp_path, mode=GatewayMode.REPLAY)
        return [
            gw.dispatch(kind="tool", key="B", invoke=_explode),
            gw.dispatch(kind="tool", key="miss-1", invoke=_explode),
            gw.dispatch(kind="tool", key="miss-2", invoke=_explode),
        ]

    assert _drain() == _drain()
