"""Attested review-receipt tests (issue #2296).

Each test maps to an acceptance criterion:

* AC1 -- a review run emits a signed receipt binding ``issue_hash``,
  ``plan_hash``, ``journal_head`` and ``diff_hash``, anchored in the
  review lineage spine.
* AC2 -- ``verify_review_receipt`` recomputes ``issue_hash`` / ``diff_hash``
  from the PR inputs and confirms the Ed25519 signature offline, proving no
  operator override occurred.
* AC3 -- autofix runs in an isolated worktree and emits a
  finding-to-fix-to-test receipt.
* AC4 -- a tampered diff makes verify fail because ``diff_hash`` no longer
  matches.
* AC5 -- the tracker comment is a projection referencing the receipt, not the
  receipt body (covered in ``test_review_projection.py``).
"""

from __future__ import annotations

from pathlib import Path

from bernstein.core.review.receipt import (
    Finding,
    ReviewReceipt,
    compute_diff_hash,
    compute_issue_hash,
    compute_plan_hash,
    emit_review_receipt,
    load_or_create_review_identity,
    read_review_receipt,
    verify_review_receipt,
)

_KEY = b"0" * 32

_ISSUE_BODY = "## Problem\nThe login path leaks tokens.\n"
_PLAN = "1. add regression test\n2. redact the token\n"
_DIFF = b"--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-leak(token)\n+redact(token)\n"
_PR_URL = "https://github.com/acme/widget/pull/42"


def _identity(tmp_path: Path) -> tuple[str, str]:
    return load_or_create_review_identity(tmp_path / ".sdd" / "identity")


def _emit(tmp_path: Path, *, diff: bytes = _DIFF, findings: tuple[Finding, ...] = ()) -> ReviewReceipt:
    priv, pub = _identity(tmp_path)
    return emit_review_receipt(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        private_key_pem=priv,
        public_key_pem=pub,
        pr_url=_PR_URL,
        repo="acme/widget",
        issue_body=_ISSUE_BODY,
        plan=_PLAN,
        journal_head="deadbeef",
        diff=diff,
        findings=findings,
        verdict="approve",
        task_id="task-1",
        timestamp=1000,
        resolution_hash="sha256:" + "a" * 64,
    )


# ---------------------------------------------------------------------------
# AC1 -- signed receipt binds the four hashes and anchors in the spine
# ---------------------------------------------------------------------------


def test_emit_binds_all_four_hashes(tmp_path: Path) -> None:
    receipt = _emit(tmp_path)
    assert receipt.issue_hash == compute_issue_hash(_ISSUE_BODY)
    assert receipt.plan_hash == compute_plan_hash(_PLAN)
    assert receipt.diff_hash == compute_diff_hash(_DIFF)
    assert receipt.journal_head == "deadbeef"
    assert receipt.verdict == "approve"
    # anchored + signed
    assert receipt.journal_entry_hash
    assert receipt.signature
    assert receipt.signer_public_key_pem


def test_emit_persists_receipt_and_reloads(tmp_path: Path) -> None:
    receipt = _emit(tmp_path)
    reloaded = read_review_receipt(tmp_path, _PR_URL)
    assert reloaded is not None
    assert reloaded.to_dict() == receipt.to_dict()


def test_emit_records_findings(tmp_path: Path) -> None:
    findings = (
        Finding(rule="BLE001", path="x.py", line=3, summary="broad except"),
        Finding(rule="S105", path="x.py", line=1, summary="hardcoded token"),
    )
    receipt = _emit(tmp_path, findings=findings)
    assert receipt.findings == findings
    reloaded = read_review_receipt(tmp_path, _PR_URL)
    assert reloaded is not None
    assert reloaded.findings == findings


# ---------------------------------------------------------------------------
# AC2 -- offline verify recomputes hashes and checks the signature
# ---------------------------------------------------------------------------


def test_verify_ok_recomputes_from_pr_inputs(tmp_path: Path) -> None:
    _emit(tmp_path)
    result = verify_review_receipt(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        pr_url=_PR_URL,
        issue_body=_ISSUE_BODY,
        diff=_DIFF,
    )
    assert result.ok, result.reason
    assert result.receipt is not None
    assert result.verdict == "approve"


def test_verify_no_receipt(tmp_path: Path) -> None:
    (tmp_path / ".sdd").mkdir()
    result = verify_review_receipt(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        pr_url=_PR_URL,
        issue_body=_ISSUE_BODY,
        diff=_DIFF,
    )
    assert not result.ok
    assert result.receipt is None


# ---------------------------------------------------------------------------
# AC4 -- a tampered diff fails verify because diff_hash diverges
# ---------------------------------------------------------------------------


def test_verify_tampered_diff_fails(tmp_path: Path) -> None:
    _emit(tmp_path)
    result = verify_review_receipt(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        pr_url=_PR_URL,
        issue_body=_ISSUE_BODY,
        diff=_DIFF + b"# sneaky extra line\n",
    )
    assert not result.ok
    assert "diff_hash" in result.reason


def test_verify_tampered_issue_fails(tmp_path: Path) -> None:
    _emit(tmp_path)
    result = verify_review_receipt(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        pr_url=_PR_URL,
        issue_body=_ISSUE_BODY + "extra",
        diff=_DIFF,
    )
    assert not result.ok
    assert "issue_hash" in result.reason


def test_verify_tampered_receipt_signature_fails(tmp_path: Path) -> None:
    _emit(tmp_path)
    from bernstein.core.review.receipt import receipt_path

    path = receipt_path(tmp_path, _PR_URL)
    raw = path.read_text(encoding="utf-8").replace("approve", "reject")
    path.write_text(raw, encoding="utf-8")
    result = verify_review_receipt(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        pr_url=_PR_URL,
        issue_body=_ISSUE_BODY,
        diff=_DIFF,
    )
    assert not result.ok


def test_verify_tampered_spine_fails(tmp_path: Path) -> None:
    _emit(tmp_path)
    spine_log = next((tmp_path / ".sdd" / "lineage").rglob("spine.jsonl"))
    raw = spine_log.read_bytes().replace(b'"actor":"bernstein.review_receipt"', b'"actor":"bernstein.review_tampered"')
    assert b"review_tampered" in raw
    spine_log.write_bytes(raw)
    result = verify_review_receipt(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        pr_url=_PR_URL,
        issue_body=_ISSUE_BODY,
        diff=_DIFF,
    )
    assert not result.ok


# ---------------------------------------------------------------------------
# Determinism -- identical inputs anchor byte-identically
# ---------------------------------------------------------------------------


def test_hashes_are_deterministic(tmp_path: Path) -> None:
    assert compute_issue_hash(_ISSUE_BODY) == compute_issue_hash(_ISSUE_BODY)
    assert compute_diff_hash(_DIFF) == compute_diff_hash(_DIFF)
    assert compute_issue_hash(_ISSUE_BODY) != compute_issue_hash(_ISSUE_BODY + " ")


def test_identity_is_stable_across_loads(tmp_path: Path) -> None:
    priv1, pub1 = _identity(tmp_path)
    priv2, pub2 = _identity(tmp_path)
    assert priv1 == priv2
    assert pub1 == pub2
