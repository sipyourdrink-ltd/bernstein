"""Review receipt resolution_hash binding tests (issue #3752)."""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.core.quality.review_pipeline.contour import PassReceiptRequest, receipt_emitter
from bernstein.core.review.receipt import (
    emit_review_receipt,
    load_or_create_review_identity,
    receipt_path,
    verify_review_chain,
    verify_review_receipt,
)

_KEY = b"0" * 32
_PR_URL = "https://github.com/acme/widget/pull/42"
_ISSUE = "## Problem\nscope\n"
_DIFF = b"--- a/x\n+++ b/x\n@@\n-a\n+b\n"
_RESOLUTION_HASH = "sha256:" + "a" * 64


def _identity(tmp_path: Path) -> tuple[str, str]:
    return load_or_create_review_identity(tmp_path / ".sdd" / "identity")


def test_receipt_without_resolution_hash_fails_verify(tmp_path: Path) -> None:
    priv, pub = _identity(tmp_path)
    emit_review_receipt(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        private_key_pem=priv,
        public_key_pem=pub,
        pr_url=_PR_URL,
        repo="acme/widget",
        issue_body=_ISSUE,
        plan="plan",
        journal_head="deadbeef",
        diff=_DIFF,
        findings=(),
        verdict="approve",
        task_id="task-1",
        timestamp=1000,
    )
    result = verify_review_receipt(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        pr_url=_PR_URL,
        issue_body=_ISSUE,
        diff=_DIFF,
    )
    assert not result.ok
    assert "resolution_hash" in result.reason


def test_receipt_direct_construction_without_hash_fails_verify(tmp_path: Path) -> None:
    # Direct ReviewReceipt with empty resolution_hash should also fail via _verify_one chain
    priv, pub = _identity(tmp_path)
    # Emit without hash so file exists with empty hash
    emit_review_receipt(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        private_key_pem=priv,
        public_key_pem=pub,
        pr_url=_PR_URL,
        repo="acme/widget",
        issue_body=_ISSUE,
        plan="plan",
        journal_head="deadbeef",
        diff=_DIFF,
        findings=(),
        verdict="approve",
        task_id="task-1",
        timestamp=1001,
    )
    # Ensure empty resolution_hash on disk
    raw = json.loads(receipt_path(tmp_path, _PR_URL).read_text())
    assert raw.get("resolution_hash") == ""
    result = verify_review_receipt(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        pr_url=_PR_URL,
        issue_body=_ISSUE,
        diff=_DIFF,
    )
    assert not result.ok
    assert "resolution_hash" in result.reason


def test_bound_resolution_hash_round_trips_and_verifies(tmp_path: Path) -> None:
    priv, pub = _identity(tmp_path)
    emit_review_receipt(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        private_key_pem=priv,
        public_key_pem=pub,
        pr_url=_PR_URL,
        repo="acme/widget",
        issue_body=_ISSUE,
        plan="plan",
        journal_head="deadbeef",
        diff=_DIFF,
        findings=(),
        verdict="approve",
        task_id="task-1",
        timestamp=1000,
        resolution_hash=_RESOLUTION_HASH,
    )
    result = verify_review_receipt(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        pr_url=_PR_URL,
        issue_body=_ISSUE,
        diff=_DIFF,
    )
    assert result.ok, result.reason
    assert result.receipt is not None
    assert result.receipt.resolution_hash == _RESOLUTION_HASH
    # to_dict round-trip
    raw = json.loads(receipt_path(tmp_path, _PR_URL).read_text())
    assert raw["resolution_hash"] == _RESOLUTION_HASH


def test_verify_chain_rejects_unbound_receipt(tmp_path: Path) -> None:
    emit = receipt_emitter(
        workdir=tmp_path,
        pr_url=_PR_URL,
        repo="acme/widget",
        issue_body=_ISSUE,
        hmac_key=_KEY,
        timestamp=1000,
    )
    # Emit without resolution_hash (default "")
    emit(PassReceiptRequest(pass_index=1, diff=_DIFF, verdict="approve", ruleset_digest="sha256:" + "b" * 64))
    result = verify_review_chain(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        pr_url=_PR_URL,
        issue_body=_ISSUE,
        diff=_DIFF,
    )
    assert not result.ok
    assert "resolution_hash" in result.reason


def test_verify_chain_accepts_bound_receipt(tmp_path: Path) -> None:
    emit = receipt_emitter(
        workdir=tmp_path,
        pr_url=_PR_URL,
        repo="acme/widget",
        issue_body=_ISSUE,
        hmac_key=_KEY,
        timestamp=1000,
    )
    emit(
        PassReceiptRequest(
            pass_index=1,
            diff=_DIFF,
            verdict="approve",
            ruleset_digest="sha256:" + "b" * 64,
            resolution_hash=_RESOLUTION_HASH,
        )
    )
    result = verify_review_chain(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        pr_url=_PR_URL,
        issue_body=_ISSUE,
        diff=_DIFF,
    )
    assert result.ok, result.reason
