"""Replay-fidelity tests for :class:`DeterministicStore` (issue #1846).

A recorded run appends one ``llm_calls.jsonl`` line per LLM call, in call
order. Replay must return the recorded responses for a repeated
``(prompt, model, ...)`` key *in that same order*, not collapse them to a
single last-write-wins value. These tests pin the per-key sequence contract
and the over-consumption guard.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.orchestration.deterministic import (
    DeterministicStore,
    ReplayMissError,
    _prompt_key,
)
from bernstein.core.replay.journal import EventJournal


def _write_calls(path: Path, rows: list[dict[str, object]]) -> None:
    """Write ``rows`` as ``llm_calls.jsonl`` lines under ``path``'s run dir."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row) + "\n")


def _record_run(run_dir: Path, calls: list[tuple[str, str, str]]) -> DeterministicStore:
    """Record ``(prompt, model, response)`` calls and return the store."""
    store = DeterministicStore(run_dir, replay=False)
    for prompt, model, response in calls:
        store.record(prompt, model, response)
    return store


def test_repeated_key_replays_in_recorded_order(tmp_path: Path) -> None:
    """Two identical (prompt, model) calls replay as A then B, not B then B.

    This is the core of issue #1846: a flat ``dict[key -> response]`` keeps
    only the last recording, so the first call wrongly replays the second
    response.
    """
    run_dir = tmp_path / "runs" / "rep-1"
    _record_run(
        run_dir,
        [
            ("same-prompt", "sonnet", "A"),
            ("same-prompt", "sonnet", "B"),
        ],
    )

    replay = DeterministicStore(run_dir, replay=True)
    first = replay.get_replay("same-prompt", "sonnet")
    second = replay.get_replay("same-prompt", "sonnet")

    assert first == "A", "first replay must return the first recorded response"
    assert second == "B", "second replay must return the second recorded response"


def test_over_consumption_raises_replay_miss(tmp_path: Path) -> None:
    """Requesting a key more times than recorded raises ReplayMissError.

    Recording K responses for a key and replaying K+1 times is a
    replay-fidelity failure: the run diverged from the recording. Strict
    replay must surface it rather than silently re-returning a stale value.
    """
    run_dir = tmp_path / "runs" / "rep-2"
    _record_run(
        run_dir,
        [
            ("p", "sonnet", "A"),
            ("p", "sonnet", "B"),
        ],
    )

    replay = DeterministicStore(run_dir, replay=True)
    assert replay.get_replay("p", "sonnet") == "A"
    assert replay.get_replay("p", "sonnet") == "B"
    with pytest.raises(ReplayMissError):
        replay.get_replay("p", "sonnet")


def test_single_occurrence_keys_unchanged(tmp_path: Path) -> None:
    """Common case: distinct keys each replay their single recorded response."""
    run_dir = tmp_path / "runs" / "rep-3"
    _record_run(
        run_dir,
        [
            ("prompt-a", "sonnet", "RA"),
            ("prompt-b", "opus", "RB"),
        ],
    )

    replay = DeterministicStore(run_dir, replay=True)
    assert replay.get_replay("prompt-a", "sonnet") == "RA"
    assert replay.get_replay("prompt-b", "opus") == "RB"


def test_interleaved_distinct_keys_each_keep_their_sequence(tmp_path: Path) -> None:
    """Per-key cursors are independent: interleaving keys does not cross streams."""
    run_dir = tmp_path / "runs" / "rep-4"
    _record_run(
        run_dir,
        [
            ("p1", "sonnet", "1A"),
            ("p2", "sonnet", "2A"),
            ("p1", "sonnet", "1B"),
            ("p2", "sonnet", "2B"),
        ],
    )

    replay = DeterministicStore(run_dir, replay=True)
    # Consume p1 fully, then p2 fully - each must follow its own order.
    assert replay.get_replay("p1", "sonnet") == "1A"
    assert replay.get_replay("p1", "sonnet") == "1B"
    assert replay.get_replay("p2", "sonnet") == "2A"
    assert replay.get_replay("p2", "sonnet") == "2B"


def test_non_strict_over_consumption_returns_none(tmp_path: Path) -> None:
    """Non-strict replay returns None on over-consumption instead of raising."""
    run_dir = tmp_path / "runs" / "rep-5"
    _record_run(run_dir, [("p", "sonnet", "A")])

    replay = DeterministicStore(run_dir, replay=True, strict=False)
    assert replay.get_replay("p", "sonnet") == "A"
    assert replay.get_replay("p", "sonnet") is None


def _replay_into_replay_log(run_dir: Path, replay_log: Path, n_calls: int) -> str:
    """Drive a replay that folds each consumed response into the event journal.

    Models the real coupling: an agent's decision (recorded as a journal event)
    is a function of the LLM response it consumed. Returns the Merkle head over
    the resulting decision stream.
    """
    store = DeterministicStore(run_dir, replay=True)
    rec = EventJournal(run_id=replay_log.parent.name, sdd_dir=replay_log.parent.parent.parent)
    for i in range(n_calls):
        response = store.get_replay("loop-prompt", "sonnet")
        # The decision the agent records depends on the consumed response.
        rec.record("agent_decision", step=i, decision=response)
    return rec.fingerprint()


def test_faithful_replay_and_swapped_replay_differ_in_fingerprint(tmp_path: Path) -> None:
    """Tie the per-key sequence to the determinism fingerprint (issue #1846).

    A faithful replay of the recording reproduces the decision stream, so its
    fingerprint matches a second faithful replay. Swapping two same-key
    responses in the fixture perturbs the consumed sequence and therefore the
    fingerprint - proving the fix couples per-key order to the determinism
    proof rather than masking the divergence.
    """
    sdd = tmp_path / ".sdd"
    run_dir = sdd / "runs" / "fp-rec"
    _record_run(
        run_dir,
        [
            ("loop-prompt", "sonnet", "A"),
            ("loop-prompt", "sonnet", "B"),
        ],
    )

    # Two faithful replays of the same recording -> identical fingerprint.
    fp_1 = _replay_into_replay_log(run_dir, sdd / "runs" / "faithful-1" / "journal.jsonl", 2)
    fp_2 = _replay_into_replay_log(run_dir, sdd / "runs" / "faithful-2" / "journal.jsonl", 2)
    assert fp_1 == fp_2
    assert fp_1 != ""

    # Swap the two same-key responses in the fixture (B then A) and replay.
    swapped_dir = sdd / "runs" / "fp-swapped"
    _record_run(
        swapped_dir,
        [
            ("loop-prompt", "sonnet", "B"),
            ("loop-prompt", "sonnet", "A"),
        ],
    )
    fp_swapped = _replay_into_replay_log(swapped_dir, sdd / "runs" / "swapped-replay" / "journal.jsonl", 2)
    assert fp_swapped != fp_1, "swapping the same-key response order must change the fingerprint"


def test_recorded_lines_preserve_call_order_on_disk(tmp_path: Path) -> None:
    """The journal itself stays append-only and order-preserving (sanity)."""
    run_dir = tmp_path / "runs" / "rep-6"
    store = _record_run(
        run_dir,
        [
            ("p", "sonnet", "first"),
            ("p", "sonnet", "second"),
        ],
    )
    lines = store.calls_path.read_text(encoding="utf-8").splitlines()
    responses = [json.loads(line)["response"] for line in lines]
    assert responses == ["first", "second"]
    # Both lines share the same key (repeated prompt+model).
    keys = {json.loads(line)["key"] for line in lines}
    assert keys == {_prompt_key("p", "sonnet")}
