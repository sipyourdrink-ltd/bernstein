"""Unit tests for the per-invocation model-call ledger.

Cost accounting prices tokens (``bernstein.core.cost.model_prices``) but
carries no call identity: which capability called which adapter, with what
resolved parameters, what went in and what came back. These tests pin the
record that holds all of it, its content hash, the guarded status graph,
the opt-in identical-call short circuit, and the replay contract that
leaves the original record untouched.

Everything here runs against a real ``.sdd`` directory and a real JSONL
file: the ledger's whole job is durability, so an in-memory double would
prove nothing.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.cost.model_call_ledger import (
    InvalidStatusTransitionError,
    ModelCallLedger,
    ModelCallRecord,
)
from bernstein.core.cost.model_prices import price_model_usage


def _ledger(tmp_path: Path) -> ModelCallLedger:
    return ModelCallLedger(sdd_dir=tmp_path / ".sdd")


class TestRecordIdentity:
    def test_one_record_per_invocation_carries_full_call_identity(self, tmp_path: Path) -> None:
        """Load-bearing: one record holds every field of a model call.

        On the pre-change tree no type combines capability id, adapter id,
        model identifier, resolved parameters, input and output, so an
        operator reading the cost ledger cannot reconstruct what a call
        actually sent or received.
        """
        ledger = _ledger(tmp_path)

        record = ledger.invoke(
            capability_id="review.summarise",
            adapter_id="claude",
            model="claude-opus-4",
            model_version="20260501",
            parameters={"temperature": 0.0, "max_tokens": 512},
            parameter_schema_version="claude/2",
            input_text="summarise this diff",
            journal_entry_id="entry-7",
            call=lambda: "the diff renames two helpers",
        )

        assert record.capability_id == "review.summarise"
        assert record.adapter_id == "claude"
        assert record.model == "claude-opus-4"
        assert record.model_version == "20260501"
        assert record.model_identifier == "claude-opus-4@20260501"
        assert record.parameters == {"temperature": 0.0, "max_tokens": 512}
        assert record.parameter_schema_version == "claude/2"
        assert record.input_text == "summarise this diff"
        assert record.output_text == "the diff renames two helpers"
        assert record.status == "succeeded"
        assert record.journal_entry_id == "entry-7"
        assert record.created_at > 0.0
        assert record.started_at >= record.created_at
        assert record.finished_at >= record.started_at

        # Exactly one record on disk, and it round-trips through JSONL.
        stored = ledger.list_records()
        assert len(stored) == 1
        assert stored[0] == record

        rows = [
            json.loads(line)
            for line in (tmp_path / ".sdd" / "runtime" / "model_calls.jsonl").read_text().splitlines()
            if line.strip()
        ]
        assert len(rows) == 1
        assert rows[0]["kind"] == "model_call"

    def test_content_hash_is_stable_for_identical_inputs(self) -> None:
        """The hash covers call identity only, independent of key order."""
        first = ModelCallRecord.new(
            capability_id="x",
            adapter_id="claude",
            model="claude-opus-4",
            parameters={"temperature": 0.0, "top_p": 1.0},
            input_text="hello",
        )
        second = ModelCallRecord.new(
            capability_id="x",
            adapter_id="claude",
            model="claude-opus-4",
            parameters={"top_p": 1.0, "temperature": 0.0},
            input_text="hello",
        )

        assert first.id != second.id
        assert first.content_hash() == second.content_hash()

        # Any of the four identity fields changing changes the hash.
        assert (
            ModelCallRecord.new(
                capability_id="y",
                adapter_id="claude",
                model="claude-opus-4",
                parameters={"temperature": 0.0, "top_p": 1.0},
                input_text="hello",
            ).content_hash()
            != first.content_hash()
        )
        assert (
            ModelCallRecord.new(
                capability_id="x",
                adapter_id="claude",
                model="claude-opus-4",
                parameters={"temperature": 0.0, "top_p": 1.0},
                input_text="goodbye",
            ).content_hash()
            != first.content_hash()
        )


class TestReuseIdentical:
    def test_reuse_identical_short_circuits_and_marks_reused(self, tmp_path: Path) -> None:
        ledger = _ledger(tmp_path)
        calls: list[str] = []

        def _call() -> str:
            calls.append("called")
            return "answer"

        first = ledger.invoke(
            capability_id="x",
            adapter_id="claude",
            model="claude-opus-4",
            parameters={"temperature": 0.0},
            input_text="question",
            call=_call,
            reuse_identical=True,
        )
        second = ledger.invoke(
            capability_id="x",
            adapter_id="claude",
            model="claude-opus-4",
            parameters={"temperature": 0.0},
            input_text="question",
            call=_call,
            reuse_identical=True,
        )

        assert calls == ["called"]
        assert first.reused is False
        assert second.reused is True
        assert second.reused_from == first.id
        assert second.output_text == first.output_text
        assert second.content_hash() == first.content_hash()
        assert len(ledger.list_records()) == 2

    def test_reuse_identical_default_off_always_calls_adapter(self, tmp_path: Path) -> None:
        ledger = _ledger(tmp_path)
        calls: list[str] = []

        def _call() -> str:
            calls.append("called")
            return "answer"

        for _ in range(2):
            record = ledger.invoke(
                capability_id="x",
                adapter_id="claude",
                model="claude-opus-4",
                parameters={"temperature": 0.0},
                input_text="question",
                call=_call,
            )
            assert record.reused is False
            assert record.reused_from == ""

        assert calls == ["called", "called"]


class TestStatusTransitions:
    def test_invalid_status_transition_raises_and_is_journaled(self, tmp_path: Path) -> None:
        ledger = _ledger(tmp_path)
        record = ModelCallRecord.new(
            capability_id="x",
            adapter_id="claude",
            model="claude-opus-4",
            parameters={},
            input_text="question",
        )

        # pending -> succeeded skips running and is refused.
        with pytest.raises(InvalidStatusTransitionError):
            ledger.transition(record, "succeeded")

        running = ledger.transition(record, "running")
        succeeded = ledger.transition(running, "succeeded")

        # A terminal status has no outgoing edge.
        with pytest.raises(InvalidStatusTransitionError):
            ledger.transition(succeeded, "running")

        refusals = ledger.refusals()
        assert [(r.from_status, r.to_status) for r in refusals] == [
            ("pending", "succeeded"),
            ("succeeded", "running"),
        ]
        assert all(r.record_id == record.id for r in refusals)

    def test_refusal_is_journaled_to_the_same_durable_file(self, tmp_path: Path) -> None:
        ledger = _ledger(tmp_path)
        record = ModelCallRecord.new(
            capability_id="x",
            adapter_id="claude",
            model="claude-opus-4",
            parameters={},
            input_text="question",
        )
        with pytest.raises(InvalidStatusTransitionError):
            ledger.transition(record, "failed")

        reopened = _ledger(tmp_path)
        assert len(reopened.refusals()) == 1
        assert reopened.list_records() == []


class TestReplay:
    def test_replay_writes_new_record_linked_to_original_which_stays_immutable(self, tmp_path: Path) -> None:
        ledger = _ledger(tmp_path)
        outputs = iter(["first answer", "second answer"])

        original = ledger.invoke(
            capability_id="x",
            adapter_id="claude",
            model="claude-opus-4",
            parameters={"temperature": 0.0},
            input_text="question",
            call=lambda: next(outputs),
        )

        replayed = ledger.replay(original.id, call=lambda: next(outputs))
        assert replayed is not None
        assert replayed.id != original.id
        assert replayed.replay_of == original.id
        # The stored parameters are re-executed verbatim.
        assert replayed.capability_id == original.capability_id
        assert replayed.parameters == original.parameters
        assert replayed.input_text == original.input_text
        assert replayed.content_hash() == original.content_hash()
        assert replayed.output_text == "second answer"

        # The original is untouched on disk, not rebuilt in place.
        reopened = _ledger(tmp_path)
        stored_original = reopened.get_record(original.id)
        assert stored_original == original
        assert stored_original is not None
        assert stored_original.output_text == "first answer"
        assert stored_original.replay_of == ""
        assert len(reopened.list_records()) == 2
        assert reopened.replays_of(original.id) == [replayed.id]

    def test_replay_of_unknown_record_returns_none(self, tmp_path: Path) -> None:
        ledger = _ledger(tmp_path)
        assert ledger.replay("nope", call=lambda: "x") is None


class TestCostJoin:
    def test_cost_row_carries_model_call_id(self, tmp_path: Path) -> None:
        ledger = _ledger(tmp_path)
        record = ledger.invoke(
            capability_id="x",
            adapter_id="claude",
            model="gpt-5-mini",
            parameters={},
            input_text="question",
            call=lambda: "answer",
        )

        priced = price_model_usage(
            "gpt-5-mini",
            input_tokens=1_000,
            output_tokens=100,
            model_call_id=record.id,
        )
        assert priced.model_call_id == record.id
        assert ledger.get_record(priced.model_call_id) == record

    def test_cost_row_model_call_id_defaults_to_empty(self) -> None:
        priced = price_model_usage("gpt-5-mini", input_tokens=1_000, output_tokens=100)
        assert priced.model_call_id == ""
