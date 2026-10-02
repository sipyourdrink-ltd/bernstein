"""Signed pass receipts over a batch ledger.

A batch pass processes a flat list of items; the
:class:`~bernstein.core.persistence.batch_ledger.BatchLedger` records only the
successes. Nothing said, in a form a third party could check, what the pass
did with the rest: which items failed and why, how many attempts each took,
what the process exited with, what its output ended on. These tests pin the
receipt that answers those questions:

* Correctness - success/failed/skipped buckets, per-item attempts and exit
  codes, an output-tail digest, and the failure reason lifted from the last
  error line, with ``error_breakdown`` counting reasons across the failures.
* Verifiability - the receipt is a registered kind of the one receipt
  protocol: signed with Ed25519, verified offline from its own bytes, and
  anchored to the ledger's chain heads so the successes it claims are exactly
  the entries the ledger appended during the pass.
* Determinism - two builds over the same outcomes produce byte-identical
  canonical bytes; no wall-clock value enters the payload.
* Tamper evidence - moving an item between buckets, editing a count, or
  editing any signed byte is reported, not silently accepted.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from bernstein.core.persistence.batch_ledger import GENESIS_HASH, BatchLedger
from bernstein.core.persistence.batch_receipt import (
    BATCH_PASS_SCHEMA_VERSION,
    RECEIPT_KIND,
    BatchItemOutcome,
    batch_pass_payload_errors,
    build_batch_pass_payload,
    failure_reason_from_tail,
    output_tail_digest,
    verify_batch_pass_against_ledger,
)
from bernstein.core.receipts.protocol import (
    canonical_receipt_bytes,
    registered_kinds,
    sign_receipt,
    verify_receipt,
)
from bernstein.core.skills.catalog.signature import generate_signer_keypair

if TYPE_CHECKING:
    from pathlib import Path

_TAIL_OK = "processing 3 rows\nwrote 3 rows\nok\n"
_TAIL_FAIL = "processing 5 rows\nTraceback (most recent call last):\n  ...\nConnectionError: upstream 503\n\n"


def _items() -> list[BatchItemOutcome]:
    return [
        BatchItemOutcome.from_output("acct-1", "success", attempts=1, exit_code=0, output_tail=_TAIL_OK),
        BatchItemOutcome.from_output("acct-2", "failed", attempts=3, exit_code=1, output_tail=_TAIL_FAIL),
        BatchItemOutcome.from_output("acct-3", "success", attempts=2, exit_code=0, output_tail=_TAIL_OK),
        BatchItemOutcome.from_output("acct-4", "failed", attempts=3, exit_code=1, output_tail=_TAIL_FAIL),
        BatchItemOutcome.from_output("acct-5", "skipped"),
    ]


def _payload(**overrides: Any) -> dict[str, Any]:
    kwargs: dict[str, Any] = {
        "batch_id": "nightly-accounts",
        "pass_id": "2026-09-25",
        "items": _items(),
        "ledger_head_before": "a" * 64,
        "ledger_head_after": "b" * 64,
    }
    kwargs.update(overrides)
    return build_batch_pass_payload(**kwargs)


@pytest.fixture
def keys() -> tuple[str, str]:
    return generate_signer_keypair()


# ---------------------------------------------------------------------------
# Correctness: the shape a pass leaves behind
# ---------------------------------------------------------------------------


class TestItemOutcome:
    def test_failure_reason_is_the_last_non_empty_line(self) -> None:
        assert failure_reason_from_tail(_TAIL_FAIL) == "ConnectionError: upstream 503"

    def test_failure_reason_is_bounded(self) -> None:
        assert len(failure_reason_from_tail("x" * 1000 + "\n")) <= 200

    def test_empty_tail_gives_empty_reason(self) -> None:
        assert failure_reason_from_tail("\n\n  \n") == ""

    def test_from_output_derives_digest_and_reason_for_a_failure(self) -> None:
        item = BatchItemOutcome.from_output("acct-2", "failed", attempts=3, exit_code=1, output_tail=_TAIL_FAIL)
        assert item.output_tail_sha256 == output_tail_digest(_TAIL_FAIL)
        assert item.failure_reason == "ConnectionError: upstream 503"

    def test_from_output_leaves_reason_empty_for_a_success(self) -> None:
        item = BatchItemOutcome.from_output("acct-1", "success", attempts=1, exit_code=0, output_tail=_TAIL_FAIL)
        assert item.failure_reason == ""
        assert item.output_tail_sha256 == output_tail_digest(_TAIL_FAIL)

    def test_a_skipped_item_has_no_attempt_and_no_output(self) -> None:
        item = BatchItemOutcome.from_output("acct-5", "skipped")
        assert item.attempts == 0
        assert item.exit_code is None
        assert item.output_tail_sha256 == ""
        assert item.failure_reason == ""

    def test_unknown_outcome_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="outcome"):
            BatchItemOutcome.from_output("acct-9", "meh")


class TestBuild:
    def test_buckets_follow_processing_order(self) -> None:
        payload = _payload()
        assert payload["schema_version"] == BATCH_PASS_SCHEMA_VERSION
        assert payload["success"] == ["acct-1", "acct-3"]
        assert payload["failed"] == ["acct-2", "acct-4"]
        assert payload["skipped"] == ["acct-5"]

    def test_error_breakdown_counts_reasons_across_failures(self) -> None:
        assert _payload()["error_breakdown"] == {"ConnectionError: upstream 503": 2}

    def test_items_carry_attempts_exit_code_and_tail_digest(self) -> None:
        by_id = {item["entity_id"]: item for item in _payload()["items"]}
        assert by_id["acct-2"] == {
            "entity_id": "acct-2",
            "outcome": "failed",
            "attempts": 3,
            "exit_code": 1,
            "output_tail_sha256": output_tail_digest(_TAIL_FAIL),
            "failure_reason": "ConnectionError: upstream 503",
        }
        assert by_id["acct-5"]["exit_code"] is None

    def test_duplicate_entity_ids_are_rejected(self) -> None:
        items = [*_items(), BatchItemOutcome.from_output("acct-1", "skipped")]
        with pytest.raises(ValueError, match="acct-1"):
            _payload(items=items)

    def test_malformed_ledger_head_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="ledger_head_after"):
            _payload(ledger_head_after="not-a-hash")

    def test_no_wall_clock_enters_the_payload(self) -> None:
        text = json.dumps(_payload())
        assert "at" not in _payload()
        assert "timestamp" not in text
        assert "2026-09-25T" not in text


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_two_builds_over_the_same_outcomes_are_byte_identical(self) -> None:
        assert canonical_receipt_bytes(_payload()) == canonical_receipt_bytes(_payload())

    def test_a_different_attempt_count_changes_the_bytes(self) -> None:
        items = _items()
        items[0] = BatchItemOutcome.from_output("acct-1", "success", attempts=2, exit_code=0, output_tail=_TAIL_OK)
        assert canonical_receipt_bytes(_payload()) != canonical_receipt_bytes(_payload(items=items))


# ---------------------------------------------------------------------------
# Verifiability: the one protocol, offline
# ---------------------------------------------------------------------------


class TestProtocol:
    def test_kind_is_registered_with_the_protocol(self) -> None:
        assert RECEIPT_KIND in registered_kinds()

    def test_well_formed_payload_has_no_errors(self) -> None:
        assert batch_pass_payload_errors(_payload()) == ()

    def test_signed_receipt_verifies_from_its_own_bytes(self, keys: tuple[str, str]) -> None:
        private_pem, public_pem = keys
        envelope = sign_receipt(RECEIPT_KIND, _payload(), private_key_pem=private_pem, public_key_pem=public_pem)
        document = json.loads(json.dumps(envelope.to_dict()))
        result = verify_receipt(document)
        assert result.ok, result.errors
        assert result.kind == RECEIPT_KIND

    def test_editing_a_signed_byte_fails_the_signature(self, keys: tuple[str, str]) -> None:
        private_pem, public_pem = keys
        envelope = sign_receipt(RECEIPT_KIND, _payload(), private_key_pem=private_pem, public_key_pem=public_pem)
        document = envelope.to_dict()
        document["payload"]["items"][1]["attempts"] = 1
        document["payload_digest"] = ""
        result = verify_receipt(document)
        assert not result.ok
        assert any("signature" in error for error in result.errors)


class TestPayloadCheck:
    def test_moving_an_item_between_buckets_is_named(self) -> None:
        payload = _payload()
        payload["success"].append(payload["failed"].pop())
        errors = batch_pass_payload_errors(payload)
        assert any("acct-4" in error for error in errors), errors

    def test_edited_error_breakdown_is_named(self) -> None:
        payload = _payload()
        payload["error_breakdown"] = {"ConnectionError: upstream 503": 1}
        errors = batch_pass_payload_errors(payload)
        assert any("error_breakdown" in error for error in errors), errors

    def test_a_failure_without_a_reason_is_named(self) -> None:
        payload = _payload()
        payload["items"][1]["failure_reason"] = ""
        payload["error_breakdown"] = {"ConnectionError: upstream 503": 1}
        errors = batch_pass_payload_errors(payload)
        assert any("acct-2" in error and "failure_reason" in error for error in errors), errors

    def test_a_success_with_a_reason_is_named(self) -> None:
        payload = _payload()
        payload["items"][0]["failure_reason"] = "oops"
        errors = batch_pass_payload_errors(payload)
        assert any("acct-1" in error and "failure_reason" in error for error in errors), errors

    def test_a_skipped_item_with_attempts_is_named(self) -> None:
        payload = _payload()
        payload["items"][4]["attempts"] = 1
        errors = batch_pass_payload_errors(payload)
        assert any("acct-5" in error and "attempts" in error for error in errors), errors

    def test_wrong_schema_version_is_named(self) -> None:
        payload = _payload()
        payload["schema_version"] = "batch-pass/v0"
        assert any("schema_version" in error for error in batch_pass_payload_errors(payload))

    def test_malformed_tail_digest_is_named(self) -> None:
        payload = _payload()
        payload["items"][0]["output_tail_sha256"] = "zz"
        assert any("output_tail_sha256" in error for error in batch_pass_payload_errors(payload))

    def test_missing_fields_are_named_not_raised(self) -> None:
        errors = batch_pass_payload_errors({"schema_version": BATCH_PASS_SCHEMA_VERSION})
        assert errors
        assert any("items" in error for error in errors)


# ---------------------------------------------------------------------------
# Ledger anchoring: the successes are the chain segment, nothing else
# ---------------------------------------------------------------------------


def _ledger_with_pass(tmp_path: Path) -> tuple[BatchLedger, str, str]:
    ledger = BatchLedger(tmp_path)
    ledger.record("acct-0", at=1_700_000_000.0)
    before = ledger.head_hash()
    ledger.record("acct-1", at=1_700_000_100.0)
    ledger.record("acct-3", at=1_700_000_200.0)
    after = ledger.head_hash()
    return ledger, before, after


class TestLedgerAnchor:
    def test_receipt_matches_the_segment_the_pass_appended(self, tmp_path: Path) -> None:
        ledger, before, after = _ledger_with_pass(tmp_path)
        payload = _payload(ledger_head_before=before, ledger_head_after=after)
        assert verify_batch_pass_against_ledger(payload, ledger) == ()

    def test_a_pass_from_genesis_matches_the_whole_ledger(self, tmp_path: Path) -> None:
        ledger = BatchLedger(tmp_path)
        ledger.record("acct-1", at=1.0)
        ledger.record("acct-3", at=2.0)
        payload = _payload(ledger_head_before=GENESIS_HASH, ledger_head_after=ledger.head_hash())
        assert verify_batch_pass_against_ledger(payload, ledger) == ()

    def test_a_claimed_success_the_ledger_never_recorded_is_named(self, tmp_path: Path) -> None:
        ledger, before, after = _ledger_with_pass(tmp_path)
        items = [*_items(), BatchItemOutcome.from_output("acct-6", "success", attempts=1, exit_code=0)]
        payload = _payload(items=items, ledger_head_before=before, ledger_head_after=after)
        errors = verify_batch_pass_against_ledger(payload, ledger)
        assert any("acct-6" in error for error in errors), errors

    def test_a_recorded_success_the_receipt_omits_is_named(self, tmp_path: Path) -> None:
        ledger, before, after = _ledger_with_pass(tmp_path)
        items = [item for item in _items() if item.entity_id != "acct-3"]
        payload = _payload(items=items, ledger_head_before=before, ledger_head_after=after)
        errors = verify_batch_pass_against_ledger(payload, ledger)
        assert any("acct-3" in error for error in errors), errors

    def test_an_unknown_head_is_named(self, tmp_path: Path) -> None:
        ledger, before, _after = _ledger_with_pass(tmp_path)
        payload = _payload(ledger_head_before=before, ledger_head_after="f" * 64)
        errors = verify_batch_pass_against_ledger(payload, ledger)
        assert any("ledger_head_after" in error for error in errors), errors

    def test_an_edited_ledger_is_named(self, tmp_path: Path) -> None:
        ledger, before, after = _ledger_with_pass(tmp_path)
        lines = ledger.path.read_text(encoding="utf-8").splitlines()
        lines[1] = lines[1].replace('"acct-1"', '"acct-X"')
        ledger.path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        payload = _payload(ledger_head_before=before, ledger_head_after=after)
        errors = verify_batch_pass_against_ledger(payload, ledger)
        assert any("chain" in error or "hash" in error for error in errors), errors


# ---------------------------------------------------------------------------
# `bernstein verify <receipt.json>` routes here by the envelope's kind
# ---------------------------------------------------------------------------


class TestDispatch:
    @pytest.fixture(autouse=True)
    def _fresh_registry(self) -> Any:
        from bernstein.cli.commands import verify_kinds
        from bernstein.core.verify_dispatch import unregister_all

        unregister_all()
        verify_kinds._registered = False
        yield
        unregister_all()
        verify_kinds._registered = False

    def test_a_signed_receipt_file_verifies_through_the_dispatcher(self, tmp_path: Path, keys: tuple[str, str]) -> None:
        from bernstein.cli.commands.verify_kinds import register_default_verifiers
        from bernstein.core.verify_dispatch import dispatch_verify

        private_pem, public_pem = keys
        envelope = sign_receipt(RECEIPT_KIND, _payload(), private_key_pem=private_pem, public_key_pem=public_pem)
        path = tmp_path / "pass.receipt.json"
        path.write_text(json.dumps(envelope.to_dict(), indent=2), encoding="utf-8")

        register_default_verifiers()
        outcome = dispatch_verify(path)
        assert outcome.kind == RECEIPT_KIND
        assert outcome.ok, outcome.message
        assert outcome.exit_code == 0

    def test_a_tampered_receipt_file_fails_with_exit_1(self, tmp_path: Path, keys: tuple[str, str]) -> None:
        from bernstein.cli.commands.verify_kinds import register_default_verifiers
        from bernstein.core.verify_dispatch import dispatch_verify

        private_pem, public_pem = keys
        envelope = sign_receipt(RECEIPT_KIND, _payload(), private_key_pem=private_pem, public_key_pem=public_pem)
        document = envelope.to_dict()
        document["payload"]["success"].append("acct-7")
        path = tmp_path / "pass.receipt.json"
        path.write_text(json.dumps(document), encoding="utf-8")

        register_default_verifiers()
        outcome = dispatch_verify(path)
        assert outcome.kind == RECEIPT_KIND
        assert not outcome.ok
        assert outcome.exit_code == 1
