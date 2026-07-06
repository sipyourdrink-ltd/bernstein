"""Tracker-comment projection tests (issue #2296, AC5).

The tracker comment is a projection of the receipt (short verdict + offline
verify command), never the full receipt body. These tests pin the projection
so a change that leaks the whole receipt into a comment fails.
"""

from __future__ import annotations

from pathlib import Path

from bernstein.core.review.receipt import (
    Finding,
    emit_review_receipt,
    load_or_create_review_identity,
)
from bernstein.github_app.review_projection import build_review_projection

_KEY = b"0" * 32
_PR_URL = "https://github.com/acme/widget/pull/42"


def _receipt(tmp_path: Path):
    priv, pub = load_or_create_review_identity(tmp_path / ".sdd" / "identity")
    return emit_review_receipt(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        private_key_pem=priv,
        public_key_pem=pub,
        pr_url=_PR_URL,
        repo="acme/widget",
        issue_body="body",
        plan="plan",
        journal_head="head",
        diff=b"diff",
        findings=(Finding(rule="R", path="p", line=1, summary="s"),),
        verdict="approve",
        task_id="task-1",
        timestamp=1000,
    )


def test_projection_carries_verdict_and_verify_command(tmp_path: Path) -> None:
    receipt = _receipt(tmp_path)
    body = build_review_projection(receipt)
    assert "approve" in body.lower()
    assert "bernstein review-receipt verify" in body
    assert _PR_URL in body
    # A stable marker so the responder can find/update its own comment.
    assert "<!-- bernstein-review-receipt -->" in body


def test_projection_references_not_embeds_receipt(tmp_path: Path) -> None:
    receipt = _receipt(tmp_path)
    body = build_review_projection(receipt)
    # The signature and the full anchor are NOT dumped into the comment;
    # the comment references the receipt, it is not the receipt.
    assert receipt.signature not in body
    assert receipt.signer_public_key_pem not in body
    # Short anchor prefix may appear, but never the whole finding payloads.
    assert '"summary"' not in body


def test_projection_is_short(tmp_path: Path) -> None:
    receipt = _receipt(tmp_path)
    body = build_review_projection(receipt)
    assert len(body) < 600
