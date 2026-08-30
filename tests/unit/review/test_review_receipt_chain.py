"""Per-pass review-receipt chain tests (issue #4481).

AC3 -- each pass of the fix-until-green contour emits a receipt binding the
reviewed diff hash, the ruleset digest, the pass index and the verdict; the
chain verifies offline and rejects a receipt whose diff or ruleset moved.
"""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.core.quality.review_pipeline.contour import PassReceiptRequest, receipt_emitter
from bernstein.core.review.receipt import (
    emit_review_receipt,
    load_or_create_review_identity,
    read_review_chain,
    read_review_receipt,
    receipt_path,
    verify_review_chain,
    verify_review_receipt,
)

_KEY = b"0" * 32
_PR_URL = "https://github.com/acme/widget/pull/42"
_ISSUE = "## Problem\nthe fix loop lives outside the product\n"
_RULES_DIGEST = "sha256:" + "a" * 64
_DIFFS = (b"--- a/x\n+++ b/x\n@@\n-a\n+b\n", b"--- a/x\n+++ b/x\n@@\n-a\n+c\n")


def _emit_chain(tmp_path: Path, *, verdicts: tuple[str, ...] = ("request_changes", "approve")) -> tuple[str, ...]:
    """Emit one receipt per pass through the contour's emitter; return anchors."""
    emit = receipt_emitter(
        workdir=tmp_path,
        pr_url=_PR_URL,
        repo="acme/widget",
        issue_body=_ISSUE,
        hmac_key=_KEY,
        timestamp=1000,
    )
    anchors: list[str] = []
    prev = ""
    for index, verdict in enumerate(verdicts, start=1):
        prev = emit(
            PassReceiptRequest(
                pass_index=index,
                diff=_DIFFS[index - 1],
                verdict=verdict,
                ruleset_digest=_RULES_DIGEST,
                prev_entry_hash=prev,
                resolution_hash="sha256:" + "a" * 64,
            )
        )
        anchors.append(prev)
    return tuple(anchors)


def _rewrite(path: Path, **fields: object) -> None:
    row = json.loads(path.read_text(encoding="utf-8"))
    row.update(fields)
    path.write_text(json.dumps(row, ensure_ascii=False, separators=(",", ":"), sort_keys=True), encoding="utf-8")


# ---------------------------------------------------------------------------
# AC3 -- one receipt per pass, chained
# ---------------------------------------------------------------------------


def test_each_pass_emits_a_receipt_binding_diff_ruleset_index_and_verdict(tmp_path: Path) -> None:
    _emit_chain(tmp_path)

    chain = read_review_chain(tmp_path, _PR_URL)

    assert [r.pass_index for r in chain] == [1, 2]
    assert [r.verdict for r in chain] == ["request_changes", "approve"]
    assert {r.ruleset_digest for r in chain} == {_RULES_DIGEST}
    assert chain[0].diff_hash != chain[1].diff_hash
    for receipt in chain:
        binding = json.loads(receipt.to_canonical_bytes())
        assert binding["pass_index"] == receipt.pass_index
        assert binding["ruleset_digest"] == _RULES_DIGEST


def test_receipt_chain_links_each_pass_to_the_previous_anchor(tmp_path: Path) -> None:
    anchors = _emit_chain(tmp_path)

    chain = read_review_chain(tmp_path, _PR_URL)

    assert chain[0].prev_entry_hash == ""
    assert chain[1].prev_entry_hash == anchors[0]
    assert chain[1].journal_entry_hash == anchors[1]


def test_verify_chain_accepts_an_untampered_chain(tmp_path: Path) -> None:
    _emit_chain(tmp_path)

    result = verify_review_chain(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        pr_url=_PR_URL,
        issue_body=_ISSUE,
        diff=_DIFFS[-1],
        ruleset_digest=_RULES_DIGEST,
    )

    assert result.ok, result.reason
    assert result.passes == 2
    assert result.verdict == "approve"


def test_verify_chain_rejects_a_tampered_pass_diff(tmp_path: Path) -> None:
    _emit_chain(tmp_path)

    result = verify_review_chain(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        pr_url=_PR_URL,
        issue_body=_ISSUE,
        diff=b"--- a/x\n+++ b/x\n@@\n-a\n+something else\n",
        ruleset_digest=_RULES_DIGEST,
    )

    assert not result.ok
    assert "diff_hash" in result.reason


def test_verify_chain_rejects_an_altered_ruleset_digest(tmp_path: Path) -> None:
    _emit_chain(tmp_path)

    result = verify_review_chain(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        pr_url=_PR_URL,
        issue_body=_ISSUE,
        diff=_DIFFS[-1],
        ruleset_digest="sha256:" + "b" * 64,
    )

    assert not result.ok
    assert "ruleset" in result.reason


def test_verify_chain_rejects_a_receipt_whose_stored_ruleset_was_rewritten(tmp_path: Path) -> None:
    _emit_chain(tmp_path)
    _rewrite(receipt_path(tmp_path, _PR_URL, pass_index=1), ruleset_digest="sha256:" + "c" * 64)

    result = verify_review_chain(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        pr_url=_PR_URL,
        issue_body=_ISSUE,
    )

    assert not result.ok
    assert "pass 1" in result.reason


def test_verify_chain_rejects_a_broken_chain_link(tmp_path: Path) -> None:
    _emit_chain(tmp_path)
    path = receipt_path(tmp_path, _PR_URL, pass_index=2)
    receipt = read_review_receipt(tmp_path, _PR_URL, pass_index=2)
    assert receipt is not None
    # Re-sign nothing: only the recorded link moves, which the chain walk sees
    # before the signature check can absolve it.
    _rewrite(path, prev_entry_hash="sha256:" + "d" * 64)

    result = verify_review_chain(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        pr_url=_PR_URL,
        issue_body=_ISSUE,
    )

    assert not result.ok
    assert "chain" in result.reason


# ---------------------------------------------------------------------------
# Back-compat -- a single-pass receipt is byte-identical to what shipped before
# ---------------------------------------------------------------------------


def test_single_pass_receipt_binding_is_unchanged_by_the_contour_fields(tmp_path: Path) -> None:
    private_pem, public_pem = load_or_create_review_identity(tmp_path / ".sdd" / "identity")
    receipt = emit_review_receipt(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        private_key_pem=private_pem,
        public_key_pem=public_pem,
        pr_url=_PR_URL,
        repo="acme/widget",
        issue_body=_ISSUE,
        plan="plan",
        journal_head="deadbeef",
        diff=_DIFFS[0],
        findings=(),
        verdict="approve",
        task_id="task-1",
        timestamp=1000,
        resolution_hash="sha256:" + "a" * 64,
    )

    binding = json.loads(receipt.to_canonical_bytes())

    assert "pass_index" not in binding
    assert "ruleset_digest" not in binding
    assert "prev_entry_hash" not in binding
    assert receipt_path(tmp_path, _PR_URL) == receipt_path(tmp_path, _PR_URL, pass_index=0)
    assert verify_review_receipt(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        pr_url=_PR_URL,
        issue_body=_ISSUE,
        diff=_DIFFS[0],
    ).ok
