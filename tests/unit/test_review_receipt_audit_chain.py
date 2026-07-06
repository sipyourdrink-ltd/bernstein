"""Audit-chain mirror for review receipts (issue #2296).

The consent-style receipt is anchored in the review lineage spine; a
projection is also mirrored into the HMAC-chained audit log so an operator
can prove, from the chain alone, that a signed review receipt was emitted for
a PR without operator override. Only hashes are recorded, never the diff or
issue body.
"""

from __future__ import annotations

from pathlib import Path

from bernstein.core.security.audit_chain import (
    EVENT_REVIEW_RECEIPT,
    AuditChainStore,
    record_review_receipt,
)


def test_record_review_receipt_appends_event(tmp_path: Path) -> None:
    chain = AuditChainStore(tmp_path / "audit", key=b"0" * 32)
    event = record_review_receipt(
        chain=chain,
        pr_url="https://github.com/acme/widget/pull/42",
        issue_hash="sha256:aa",
        plan_hash="sha256:bb",
        journal_head="cc",
        diff_hash="sha256:dd",
        verdict="approve",
        journal_entry_hash="sha256:ee",
    )
    assert event.event_type == EVENT_REVIEW_RECEIPT
    rows = chain.query(event_type=EVENT_REVIEW_RECEIPT)
    assert len(rows) == 1
    details = rows[0].details
    assert details["issue_hash"] == "sha256:aa"
    assert details["diff_hash"] == "sha256:dd"
    assert details["verdict"] == "approve"
    assert details["journal_entry_hash"] == "sha256:ee"
    assert "prev_chain_digest" in details


def test_record_review_receipt_never_carries_diff_body(tmp_path: Path) -> None:
    chain = AuditChainStore(tmp_path / "audit", key=b"0" * 32)
    record_review_receipt(
        chain=chain,
        pr_url="https://github.com/acme/widget/pull/42",
        issue_hash="sha256:aa",
        plan_hash="sha256:bb",
        journal_head="cc",
        diff_hash="sha256:dd",
        verdict="approve",
        journal_entry_hash="sha256:ee",
    )
    rows = chain.query(event_type=EVENT_REVIEW_RECEIPT)
    ok, errors = chain.verify()
    assert ok, errors
    # Only hashes/verdict/url are recorded.
    assert set(rows[0].details) <= {
        "pr_url",
        "issue_hash",
        "plan_hash",
        "journal_head",
        "diff_hash",
        "verdict",
        "journal_entry_hash",
        "prev_chain_digest",
    }
