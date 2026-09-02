"""A drop-verify verdict must never read as more than it proves (#5067).

The receipt verifier already distinguishes an integrity-only pass from a
provenance pass; nothing projected that distinction into a document a screen
could render, so a console wrapping it would print a bare tick. These tests
pin the projection: a pass carries the tier it reached and the caveat that
explains it, and a failure carries neither.
"""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.core.security.governance_receipt_verdict import (
    TIER_INTEGRITY_ONLY,
    collect_receipt_verdict,
    receipt_verdict_json,
)

_VECTORS = Path(__file__).resolve().parents[3] / "tests" / "fixtures" / "receipt-vectors"
_VALID = _VECTORS / "valid-run-receipt.json"
_TAMPERED = _VECTORS / "tampered-run-receipt.json"


def test_verified_receipt_is_labelled_integrity_only_not_verified() -> None:
    """A pass over a self-signed receipt reports the tier it actually reached."""
    verdict = collect_receipt_verdict(_VALID.read_bytes())

    assert verdict.ok is True
    assert verdict.status == "ok"
    assert verdict.tier == TIER_INTEGRITY_ONLY


def test_integrity_only_pass_carries_the_embedded_key_caveat() -> None:
    """The caveat names where the key came from, so the tick cannot stand alone."""
    verdict = collect_receipt_verdict(_VALID.read_bytes())

    assert verdict.caveat is not None
    assert "embedded" in verdict.caveat


def test_verified_receipt_reports_the_range_it_attests() -> None:
    """The counts the verifier walked travel with the verdict."""
    verdict = collect_receipt_verdict(_VALID.read_bytes())

    assert verdict.run_id
    assert verdict.journal_events > 0
    assert verdict.spine_entries > 0


def test_tampered_receipt_reports_no_tier_and_no_caveat() -> None:
    """A receipt that did not verify claims nothing, so it qualifies nothing."""
    verdict = collect_receipt_verdict(_TAMPERED.read_bytes())

    assert verdict.ok is False
    assert verdict.status == "tampered"
    assert verdict.tier is None
    assert verdict.caveat is None


def test_tampered_receipt_carries_the_verifier_errors() -> None:
    """The screen shows why, not just that it failed."""
    verdict = collect_receipt_verdict(_TAMPERED.read_bytes())

    assert verdict.errors


def test_input_that_is_not_a_receipt_is_malformed_rather_than_tampered() -> None:
    """ "Not a receipt" and "a tampered receipt" are different answers."""
    verdict = collect_receipt_verdict(b'{"this": "is not a run receipt"}')

    assert verdict.status == "malformed"
    assert verdict.ok is False
    assert verdict.tier is None


def test_empty_input_is_reported_rather_than_raising() -> None:
    """An empty drop is a verdict about the file, not a server error."""
    verdict = collect_receipt_verdict(b"")

    assert verdict.status == "malformed"
    assert verdict.tier is None


def test_document_is_canonical_and_stable() -> None:
    """Two serialisations of the same bytes are byte-identical and sorted."""
    first = receipt_verdict_json(_VALID.read_bytes())
    second = receipt_verdict_json(_VALID.read_bytes())

    assert first == second
    assert first == json.dumps(json.loads(first), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
