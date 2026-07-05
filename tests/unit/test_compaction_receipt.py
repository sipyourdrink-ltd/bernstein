"""Tests for HMAC-chained compaction receipts (issue #2246).

Covers:
- Receipt build / serialise / deserialise round trip.
- record_compaction_receipt: prev_chain_digest embedding + chain verify.
- Step-journal registration: the compaction step carries the receipt's
  pre/post hashes inside the hashed payload, so tampering breaks replay.
- verify_compaction_receipts (AC #2): a compaction step without a
  chain-verifiable receipt fails the run's audit verification.
- Ledger reconciliation (AC #5): tokens_before/after in receipts
  reconcile with the spend-ledger deltas for the task.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.core.cost.spend_ledger import CallTags, SpendLedger
from bernstein.core.persistence.journal import Journal, JournalReader
from bernstein.core.security.audit_chain import (
    EVENT_COMPACTION_RECEIPT,
    AuditChainStore,
)
from bernstein.core.tokens.compaction_receipt import (
    COMPACTION_STEP_KIND,
    LEDGER_FEATURE_LABEL,
    CompactionReceipt,
    build_receipt,
    find_compaction_steps,
    load_receipts,
    receipt_from_details,
    reconcile_with_ledger,
    record_compaction_journal_step,
    record_compaction_receipt,
    record_ledger_delta,
    sha256_hex,
    verify_compaction_receipts,
)
from bernstein.core.tokens.compaction_validate import ValidatorVerdict

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _chain(tmp_path: Path) -> AuditChainStore:
    return AuditChainStore(tmp_path / "audit", key=b"k" * 32)


def _receipt(task_id: str = "T-1", trigger: str = "proactive") -> CompactionReceipt:
    return build_receipt(
        task_id=task_id,
        worker_id="sess-1",
        trigger=trigger,
        pre_text="original context " * 40,
        post_text="compacted context",
        tokens_before=400,
        tokens_after=25,
        verdicts=(
            ValidatorVerdict(name="code_blocks", passed=True),
            ValidatorVerdict(name="quoted_errors", passed=True),
        ),
        retry_count=0,
        gate_action="redacted",
        gate_rule_ids=("content.pem-private-key",),
        skills_reinjected=True,
    )


# ---------------------------------------------------------------------------
# Receipt shape
# ---------------------------------------------------------------------------


class TestReceiptShape:
    def test_hashes_are_sha256_of_texts(self) -> None:
        receipt = _receipt()
        assert receipt.pre_sha256 == sha256_hex("original context " * 40)
        assert receipt.post_sha256 == sha256_hex("compacted context")
        assert len(receipt.pre_sha256) == 64

    def test_details_round_trip(self) -> None:
        receipt = _receipt()
        details = receipt.to_details()
        assert details["trigger"] == "proactive"
        assert details["validators"] == [
            {"name": "code_blocks", "result": "pass"},
            {"name": "quoted_errors", "result": "pass"},
        ]
        assert details["skills_reinjected"] is True
        restored = receipt_from_details(details)
        assert restored == receipt

    def test_details_are_json_serialisable(self) -> None:
        assert json.loads(json.dumps(_receipt().to_details()))

    def test_invalid_trigger_rejected(self) -> None:
        with pytest.raises(ValueError, match="trigger"):
            _receipt(trigger="magic")

    def test_failed_validator_serialised_as_fail(self) -> None:
        receipt = build_receipt(
            task_id="T-1",
            worker_id="w",
            trigger="reactive",
            pre_text="a",
            post_text="b",
            tokens_before=2,
            tokens_after=1,
            verdicts=(ValidatorVerdict(name="code_blocks", passed=False, detail="x"),),
            retry_count=1,
        )
        assert receipt.to_details()["validators"] == [{"name": "code_blocks", "result": "fail"}]


# ---------------------------------------------------------------------------
# Chain recording
# ---------------------------------------------------------------------------


class TestChainRecording:
    def test_receipt_event_embeds_prev_digest_and_chain_verifies(self, tmp_path: Path) -> None:
        chain = _chain(tmp_path)
        event = record_compaction_receipt(chain=chain, receipt=_receipt())
        assert event.event_type == EVENT_COMPACTION_RECEIPT
        assert "prev_chain_digest" in event.details
        ok, errors = chain.verify()
        assert ok, errors

    def test_load_receipts_filters_by_task(self, tmp_path: Path) -> None:
        chain = _chain(tmp_path)
        record_compaction_receipt(chain=chain, receipt=_receipt(task_id="T-1"))
        record_compaction_receipt(chain=chain, receipt=_receipt(task_id="T-2"))
        record_compaction_receipt(chain=chain, receipt=_receipt(task_id="T-1", trigger="reactive"))

        receipts = load_receipts(chain, task_id="T-1")
        assert len(receipts) == 2
        assert {r.trigger for r in receipts} == {"proactive", "reactive"}
        assert all(r.task_id == "T-1" for r in receipts)

    def test_gate_outcomes_referenced_not_duplicated(self, tmp_path: Path) -> None:
        # The receipt records the gate action and rule ids; the gate events
        # themselves are chained separately by the sensitive-gate lane.
        chain = _chain(tmp_path)
        record_compaction_receipt(chain=chain, receipt=_receipt())
        [receipt] = load_receipts(chain)
        assert receipt.gate_action == "redacted"
        assert receipt.gate_rule_ids == ("content.pem-private-key",)
        details = receipt.to_details()
        assert "span_hash" not in details  # gate evidence lives in gate events


# ---------------------------------------------------------------------------
# Step-journal registration
# ---------------------------------------------------------------------------


class TestJournalRegistration:
    def test_compaction_step_registered_and_journal_verifies(self, tmp_path: Path) -> None:
        receipt = _receipt()
        journal = Journal.open(tmp_path / "journal" / "sess-1")
        entry = record_compaction_journal_step(journal, receipt)

        assert entry.input_hash == receipt.pre_sha256
        assert entry.tool_call["kind"] == COMPACTION_STEP_KIND
        assert entry.tool_call["pre_sha256"] == receipt.pre_sha256
        assert entry.tool_call["post_sha256"] == receipt.post_sha256
        assert entry.tool_call["correlation_id"] == receipt.correlation_id

        reader = JournalReader(tmp_path / "journal" / "sess-1")
        result = reader.verify()
        assert result.ok, result.errors
        assert find_compaction_steps(reader) == [entry]

    def test_tampered_post_hash_breaks_replay_verification(self, tmp_path: Path) -> None:
        receipt = _receipt()
        journal = Journal.open(tmp_path / "journal" / "sess-1")
        record_compaction_journal_step(journal, receipt)

        bucket = journal.bucket_path
        row = json.loads(bucket.read_text(encoding="utf-8").strip())
        row["tool_call"]["post_sha256"] = "f" * 64
        bucket.write_text(json.dumps(row) + "\n", encoding="utf-8")

        result = JournalReader(tmp_path / "journal" / "sess-1").verify()
        assert not result.ok
        assert any("step_hash mismatch" in err for err in result.errors)

    def test_ordinary_steps_are_not_compaction_steps(self, tmp_path: Path) -> None:
        journal = Journal.open(tmp_path / "journal" / "sess-1")
        journal.append(input_hash="a" * 64, tool_call={"tool": "bash"})
        reader = JournalReader(tmp_path / "journal" / "sess-1")
        assert find_compaction_steps(reader) == []


# ---------------------------------------------------------------------------
# AC #2: audit verification fails without a chain-verifiable receipt
# ---------------------------------------------------------------------------


class TestVerifyCompactionReceipts:
    def test_receipted_compaction_verifies(self, tmp_path: Path) -> None:
        receipt = _receipt()
        chain = _chain(tmp_path)
        record_compaction_receipt(chain=chain, receipt=receipt)
        journal = Journal.open(tmp_path / "journal" / "sess-1")
        record_compaction_journal_step(journal, receipt)

        ok, errors = verify_compaction_receipts(
            chain,
            journal_reader=JournalReader(tmp_path / "journal" / "sess-1"),
        )
        assert ok, errors

    def test_compaction_without_receipt_fails_verification(self, tmp_path: Path) -> None:
        receipt = _receipt()
        chain = _chain(tmp_path)  # receipt never recorded
        journal = Journal.open(tmp_path / "journal" / "sess-1")
        record_compaction_journal_step(journal, receipt)

        ok, errors = verify_compaction_receipts(
            chain,
            journal_reader=JournalReader(tmp_path / "journal" / "sess-1"),
        )
        assert not ok
        assert any("no chain receipt" in err for err in errors)

    def test_task_filter_skips_other_tasks_journal_steps(self, tmp_path: Path) -> None:
        # One worker journal carrying compactions for two tasks: scoping
        # verification to T-1 must not flag T-2's (receipt-less) step.
        receipt_t1 = _receipt(task_id="T-1")
        receipt_t2 = _receipt(task_id="T-2")
        chain = _chain(tmp_path)
        record_compaction_receipt(chain=chain, receipt=receipt_t1)  # T-2 never receipted
        journal = Journal.open(tmp_path / "journal" / "sess-1")
        record_compaction_journal_step(journal, receipt_t1)
        record_compaction_journal_step(journal, receipt_t2)

        ok, errors = verify_compaction_receipts(
            chain,
            journal_reader=JournalReader(tmp_path / "journal" / "sess-1"),
            task_id="T-1",
        )
        assert ok, errors

        # Unscoped verification still catches the receipt-less T-2 step.
        ok_all, errors_all = verify_compaction_receipts(
            chain,
            journal_reader=JournalReader(tmp_path / "journal" / "sess-1"),
        )
        assert not ok_all
        assert any("no chain receipt" in err for err in errors_all)

    def test_hash_mismatch_fails_verification(self, tmp_path: Path) -> None:
        receipt = _receipt()
        chain = _chain(tmp_path)
        record_compaction_receipt(chain=chain, receipt=receipt)
        # Journal a different compaction (same correlation id, wrong hashes).
        other = build_receipt(
            task_id=receipt.task_id,
            worker_id=receipt.worker_id,
            trigger="proactive",
            pre_text="different pre",
            post_text="different post",
            tokens_before=10,
            tokens_after=5,
            verdicts=(),
            retry_count=0,
            correlation_id=receipt.correlation_id,
        )
        journal = Journal.open(tmp_path / "journal" / "sess-1")
        record_compaction_journal_step(journal, other)

        ok, errors = verify_compaction_receipts(
            chain,
            journal_reader=JournalReader(tmp_path / "journal" / "sess-1"),
        )
        assert not ok
        assert any("hash" in err for err in errors)

    def test_broken_hmac_chain_fails_verification(self, tmp_path: Path) -> None:
        receipt = _receipt()
        chain = _chain(tmp_path)
        record_compaction_receipt(chain=chain, receipt=receipt)

        # Tamper with the audit file bytes after recording.
        audit_files = sorted((tmp_path / "audit").glob("*.jsonl"))
        assert audit_files
        target = audit_files[0]
        target.write_text(target.read_text(encoding="utf-8").replace("proactive", "reactive-"), encoding="utf-8")

        ok, _errors = verify_compaction_receipts(chain)
        assert not ok

    def test_no_journal_and_no_receipts_is_ok(self, tmp_path: Path) -> None:
        ok, errors = verify_compaction_receipts(_chain(tmp_path))
        assert ok, errors


# ---------------------------------------------------------------------------
# AC #5: receipt tokens reconcile with ledger deltas
# ---------------------------------------------------------------------------


class TestLedgerReconciliation:
    def test_record_and_reconcile_round_trip(self, tmp_path: Path) -> None:
        receipt = _receipt()
        ledger = SpendLedger(path=tmp_path / "ledger.jsonl", run_id="run-1")
        record_ledger_delta(ledger, receipt)

        entries = SpendLedger.load_entries(tmp_path / "ledger.jsonl")
        assert len(entries) == 1
        assert entries[0].feature_label == LEDGER_FEATURE_LABEL
        assert entries[0].cost_usd == 0.0

        ok, errors = reconcile_with_ledger([receipt], entries)
        assert ok, errors

    def test_missing_ledger_row_fails_reconciliation(self) -> None:
        ok, errors = reconcile_with_ledger([_receipt()], [])
        assert not ok
        assert any("no ledger row" in err for err in errors)

    def test_token_mismatch_fails_reconciliation(self, tmp_path: Path) -> None:
        receipt = _receipt()
        ledger = SpendLedger(path=tmp_path / "ledger.jsonl", run_id="run-1")
        record_ledger_delta(ledger, receipt)
        entries = SpendLedger.load_entries(tmp_path / "ledger.jsonl")

        drifted = build_receipt(
            task_id=receipt.task_id,
            worker_id=receipt.worker_id,
            trigger=receipt.trigger,
            pre_text="x",
            post_text="y",
            tokens_before=receipt.tokens_before + 7,
            tokens_after=receipt.tokens_after,
            verdicts=(),
            retry_count=0,
            correlation_id=receipt.correlation_id,
        )
        ok, errors = reconcile_with_ledger([drifted], entries)
        assert not ok
        assert any("tokens" in err for err in errors)

    def test_reconciles_per_task(self, tmp_path: Path) -> None:
        r1 = _receipt(task_id="T-1")
        r2 = _receipt(task_id="T-2", trigger="reactive")
        ledger = SpendLedger(path=tmp_path / "ledger.jsonl", run_id="run-1")
        record_ledger_delta(ledger, r1)
        record_ledger_delta(ledger, r2)
        # Unrelated spend rows must not confuse reconciliation.
        ledger.record(
            tags=CallTags(task_id="T-1", agent_id="sess-1"),
            model="sonnet",
            cost_usd=0.5,
            input_tokens=100,
            output_tokens=50,
        )
        entries = SpendLedger.load_entries(tmp_path / "ledger.jsonl")

        ok, errors = reconcile_with_ledger([r1, r2], entries)
        assert ok, errors
