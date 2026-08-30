"""Tests for :mod:`bernstein.core.finding_verify`.

Covers the finding-verify receipt model introduced for issue #2557:
:class:`~bernstein.core.finding_verify.FindingVerifyVerdict`,
:class:`~bernstein.core.finding_verify.FindingVerifyResult`,
:class:`~bernstein.core.finding_verify.FindingVerifyReceipt`, the
``DRIFT_REASON`` taxonomy, and the spine-anchoring helpers.
"""

from __future__ import annotations

import pytest

from bernstein.core.finding_verify import (
    DRIFT_REASON,
    FindingVerifyReceipt,
    FindingVerifyResult,
    FindingVerifyVerdict,
    build_finding_verify_receipt,
    finding_verify_step_id,
)

# ---------------------------------------------------------------------------
# DRIFT_REASON taxonomy
# ---------------------------------------------------------------------------


def test_drift_reason_contains_all_expected_values() -> None:
    assert {
        "feed_changed",
        "rule_changed",
        "target_changed",
        "nondeterministic",
    } == DRIFT_REASON


def test_drift_reason_is_frozenset() -> None:
    assert isinstance(DRIFT_REASON, frozenset)
    # Frozen — can't mutate the taxonomy at runtime.
    with pytest.raises(AttributeError):
        DRIFT_REASON.add("surprise")  # type: ignore[attr-defined]


# ---------------------------------------------------------------------------
# FindingVerifyVerdict
# ---------------------------------------------------------------------------


def test_verdict_match() -> None:
    assert FindingVerifyVerdict.MATCH.value == "match"
    assert FindingVerifyVerdict.DRIFT.value == "drift"


def test_verdict_drift_values() -> None:
    assert FindingVerifyVerdict.DRIFT_FEED_CHANGED.value == "drift_feed_changed"
    assert FindingVerifyVerdict.DRIFT_RULE_CHANGED.value == "drift_rule_changed"
    assert FindingVerifyVerdict.DRIFT_TARGET_CHANGED.value == "drift_target_changed"
    assert FindingVerifyVerdict.DRIFT_NON_DETERMINISTIC.value == "drift_nondeterministic"


# ---------------------------------------------------------------------------
# FindingVerifyResult
# ---------------------------------------------------------------------------


def test_result_round_trip() -> None:
    result = FindingVerifyResult(
        verdict=FindingVerifyVerdict.DRIFT_FEED_CHANGED,
        reason="feed content shifted",
        finding_id="f-42",
        task_id="t-9",
        stored_hash="sha256:aaa",
        computed_hash="sha256:bbb",
    )
    assert result.verdict == FindingVerifyVerdict.DRIFT_FEED_CHANGED
    assert result.reason == "feed content shifted"
    assert result.finding_id == "f-42"
    assert result.task_id == "t-9"
    assert result.stored_hash == "sha256:aaa"
    assert result.computed_hash == "sha256:bbb"


# ---------------------------------------------------------------------------
# FindingVerifyReceipt
# ---------------------------------------------------------------------------


def test_receipt_canonical_bytes_deterministic() -> None:
    """Two identical receipts produce byte-identical canonical output."""
    r1 = build_finding_verify_receipt(
        finding_id="f-1",
        task_id="t-1",
        stored_hash="sha256:old",
        computed_hash="sha256:new",
        verdict=FindingVerifyVerdict.DRIFT_RULE_CHANGED,
        reason="rule edit",
    )
    r2 = build_finding_verify_receipt(
        finding_id="f-1",
        task_id="t-1",
        stored_hash="sha256:old",
        computed_hash="sha256:new",
        verdict=FindingVerifyVerdict.DRIFT_RULE_CHANGED,
        reason="rule edit",
    )
    assert r1.canonical_bytes() == r2.canonical_bytes()
    assert r1.content_hash() == r2.content_hash()


def test_receipt_canonical_bytes_excludes_spine_entry_hash() -> None:
    """Anchoring (setting spine_entry_hash) must not change the content hash."""
    unanchored = build_finding_verify_receipt(
        finding_id="f-2",
        task_id="t-2",
        stored_hash="sha256:aaa",
        computed_hash="sha256:bbb",
        verdict=FindingVerifyVerdict.MATCH,
    )
    anchored = unanchored.with_entry_hash("spine-entry-hash-abc")
    assert unanchored.content_hash() == anchored.content_hash()
    assert unanchored.artifact_path() == anchored.artifact_path()
    assert anchored.spine_entry_hash == "spine-entry-hash-abc"


def test_receipt_artifact_path_format() -> None:
    receipt = build_finding_verify_receipt(
        finding_id="f-3",
        task_id="t-3",
        stored_hash="sha256:x",
        computed_hash="sha256:y",
        verdict=FindingVerifyVerdict.MATCH,
    )
    # Path must be repo-relative and POSIX so LineageSpine._reject_unsafe_artifact_path
    # accepts it (see FINDING_VERIFY_ARTIFACT_DIR in the module).
    assert not receipt.artifact_path().startswith("/")
    assert ".." not in receipt.artifact_path()
    assert receipt.artifact_path().endswith(".json")


def test_receipt_from_result() -> None:
    result = FindingVerifyResult(
        verdict=FindingVerifyVerdict.DRIFT_TARGET_CHANGED,
        reason="target moved",
        finding_id="f-4",
        task_id="t-4",
        stored_hash="sha256:a",
        computed_hash="sha256:b",
    )
    receipt = FindingVerifyReceipt.from_result(result)
    assert receipt.finding_id == result.finding_id
    assert receipt.verdict == result.verdict
    assert receipt.spine_entry_hash is None  # not yet anchored


def test_receipt_from_result_passes_spine_entry_hash() -> None:
    result = FindingVerifyResult(
        verdict=FindingVerifyVerdict.MATCH,
        reason="",
        finding_id="f-5",
        task_id="t-5",
        stored_hash="sha256:a",
        computed_hash="sha256:a",
    )
    receipt = FindingVerifyReceipt.from_result(result, spine_entry_hash="entry-abc")
    assert receipt.spine_entry_hash == "entry-abc"


def test_finding_verify_step_id() -> None:
    receipt = build_finding_verify_receipt(
        finding_id="f-99",
        task_id="t-99",
        stored_hash="sha256:a",
        computed_hash="sha256:b",
        verdict=FindingVerifyVerdict.DRIFT_NON_DETERMINISTIC,
    )
    assert finding_verify_step_id(receipt) == "finding-verify:f-99"


def test_match_verdict_has_empty_reason_by_default() -> None:
    receipt = build_finding_verify_receipt(
        finding_id="f-6",
        task_id="t-6",
        stored_hash="sha256:a",
        computed_hash="sha256:a",
        verdict=FindingVerifyVerdict.MATCH,
    )
    assert receipt.reason == ""
