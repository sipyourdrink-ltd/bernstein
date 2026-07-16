"""Verifiable claim receipts for the MCP pull-worker loop (issue #2555).

A claim receipt is the returned, client-verifiable object a worker receives
from a claim: a content-addressed digest over the claim decision, embedding
the audit-chain head so the worker can prove the claim it made offline. These
tests isolate state with ``tmp_path`` and never depend on wall-clock ordering.
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING

from bernstein.core.protocols.mcp.claim_receipt import (
    REFUSED_TASK_ID,
    ClaimReceipt,
    backlog_head,
    filter_digest,
    sign_claim_receipt,
    verify_claim_receipt,
)
from bernstein.core.protocols.mcp.tasks_extension import SPEC_REVISION
from bernstein.core.security.audit_chain import AuditChainStore, record_task_claim_receipt
from bernstein.core.tasks.claim import Backlog, BacklogEntry, ClaimFilter, claim_next_entry

if TYPE_CHECKING:
    from pathlib import Path

_KEY = b"claim-receipt-test-key"


def _identity() -> tuple[str, str]:
    from bernstein.core.lineage.identity import generate_keypair

    return generate_keypair()


def _rows(*entries: BacklogEntry) -> list[dict[str, object]]:
    return [entry.to_dict() for entry in entries]


class TestBacklogHead:
    def test_head_is_content_addressed(self) -> None:
        rows = _rows(BacklogEntry(id="t1"), BacklogEntry(id="t2"))
        assert backlog_head(rows).startswith("sha256:")

    def test_head_excludes_wall_clock(self) -> None:
        # Two backlogs identical apart from the claim wall-clock stamp must
        # hash the same, so the receipt reprojects from the post-claim
        # on-disk backlog regardless of when the claim landed.
        early = BacklogEntry(id="t1", status="in_progress", claimer="w", claimed_at=1.0)
        late = BacklogEntry(id="t1", status="in_progress", claimer="w", claimed_at=999.0)
        assert backlog_head(_rows(early)) == backlog_head(_rows(late))

    def test_head_changes_with_row_content(self) -> None:
        assert backlog_head(_rows(BacklogEntry(id="t1"))) != backlog_head(_rows(BacklogEntry(id="t2")))

    def test_head_is_order_sensitive(self) -> None:
        a = _rows(BacklogEntry(id="t1"), BacklogEntry(id="t2"))
        b = _rows(BacklogEntry(id="t2"), BacklogEntry(id="t1"))
        assert backlog_head(a) != backlog_head(b)


class TestFilterDigest:
    def test_digest_is_content_addressed(self) -> None:
        assert filter_digest(ClaimFilter(role="backend")).startswith("sha256:")

    def test_completed_ids_order_does_not_matter(self) -> None:
        a = ClaimFilter(completed_ids={"a", "b", "c"})
        b = ClaimFilter(completed_ids={"c", "b", "a"})
        assert filter_digest(a) == filter_digest(b)

    def test_digest_changes_with_role(self) -> None:
        assert filter_digest(ClaimFilter(role="backend")) != filter_digest(ClaimFilter(role="frontend"))


class TestReceiptHash:
    def _receipt(self, **over: object) -> ClaimReceipt:
        base = {
            "task_id": "t1",
            "claimer_card_fingerprint": "sha256:abc",
            "backlog_head": "sha256:" + "a" * 64,
            "filter_digest": "sha256:" + "b" * 64,
            "chain_head": "c" * 64,
        }
        base.update(over)
        return ClaimReceipt(**base)  # type: ignore[arg-type]

    def test_receipt_hash_is_deterministic(self) -> None:
        assert self._receipt().receipt_hash == self._receipt().receipt_hash

    def test_receipt_hash_default_spec_revision(self) -> None:
        assert self._receipt().spec_revision == SPEC_REVISION

    def test_receipt_hash_changes_with_each_field(self) -> None:
        base = self._receipt()
        assert base.receipt_hash != self._receipt(task_id="t2").receipt_hash
        assert base.receipt_hash != self._receipt(claimer_card_fingerprint="sha256:xyz").receipt_hash
        assert base.receipt_hash != self._receipt(backlog_head="sha256:" + "z" * 64).receipt_hash
        assert base.receipt_hash != self._receipt(filter_digest="sha256:" + "z" * 64).receipt_hash
        assert base.receipt_hash != self._receipt(chain_head="d" * 64).receipt_hash

    def test_signature_is_not_in_pre_image(self) -> None:
        # Signing must not change the content-addressed identity.
        priv, pub = _identity()
        unsigned = self._receipt()
        signed = sign_claim_receipt(unsigned, private_key_pem=priv, public_key_pem=pub)
        assert signed.receipt_hash == unsigned.receipt_hash

    def test_granted_flag_tracks_task_id(self) -> None:
        assert self._receipt().granted is True
        assert self._receipt(task_id=REFUSED_TASK_ID).granted is False

    def test_wire_roundtrip_preserves_canonical_fields(self) -> None:
        priv, pub = _identity()
        signed = sign_claim_receipt(self._receipt(), private_key_pem=priv, public_key_pem=pub)
        restored = ClaimReceipt.from_wire(signed.to_wire())
        assert restored == signed
        assert signed.to_wire()["receiptHash"] == signed.receipt_hash


def _seed_backlog(path: Path, entries: list[BacklogEntry]) -> None:
    Backlog.write(path, entries)


def _claim_and_receipt(
    tmp_path: Path,
    *,
    entries: list[BacklogEntry],
    claim_filter: ClaimFilter,
    claimer_id: str = "worker-1",
    fingerprint: str = "sha256:cardfp",
) -> tuple[ClaimReceipt, AuditChainStore, Path]:
    """Drive the substrate claim path and build a signed receipt.

    Mirrors what the claim route does: claim under the filter, record the
    existing ``task.claim_receipt`` audit event, embed the head that event
    recorded, and sign.
    """
    backlog_path = tmp_path / "task-backlog.json"
    _seed_backlog(backlog_path, entries)
    chain = AuditChainStore(tmp_path / "audit", key=_KEY)
    priv, pub = _identity()

    entry = claim_next_entry(backlog_path, claimer_id=claimer_id, filter=claim_filter)
    rows_after = _rows(*Backlog.load(backlog_path).entries)
    fd = filter_digest(claim_filter)
    bh = backlog_head(rows_after)
    if entry is None:
        chain_head = chain.prev_chain_digest
        receipt = ClaimReceipt.refusal(
            claimer_card_fingerprint=fingerprint,
            backlog_head=bh,
            filter_digest=fd,
            chain_head=chain_head,
        )
    else:
        event = record_task_claim_receipt(
            chain=chain,
            task_id=entry.id,
            role=entry.role or "",
            claimed_by=claimer_id,
            depends_on=list(entry.depends_on),
            task_version=entry.attempts,
            claim_path="mcp_claim",
        )
        chain_head = str(event.details.get("prev_chain_digest", ""))
        receipt = ClaimReceipt.granted_receipt(
            task_id=entry.id,
            claimer_card_fingerprint=fingerprint,
            backlog_head=bh,
            filter_digest=fd,
            chain_head=chain_head,
        )
    return sign_claim_receipt(receipt, private_key_pem=priv, public_key_pem=pub), chain, backlog_path


class TestDependencyGate:
    def test_gated_task_is_refused_as_a_receipt(self, tmp_path: Path) -> None:
        # A task whose depends_on is not complete must not be granted, and the
        # refusal is itself a signed receipt (no silent skip).
        entries = [BacklogEntry(id="t2", depends_on=["t1"])]
        receipt, chain, backlog_path = _claim_and_receipt(
            tmp_path,
            entries=entries,
            claim_filter=ClaimFilter(completed_ids=frozenset()),
        )
        assert receipt.granted is False
        assert receipt.task_id == REFUSED_TASK_ID
        rows = _rows(*Backlog.load(backlog_path).entries)
        ok, reason = verify_claim_receipt(receipt, rows, chain)
        assert ok, reason

    def test_task_granted_once_dependency_completes(self, tmp_path: Path) -> None:
        entries = [BacklogEntry(id="t2", depends_on=["t1"])]
        receipt, chain, backlog_path = _claim_and_receipt(
            tmp_path,
            entries=entries,
            claim_filter=ClaimFilter(completed_ids=frozenset({"t1"})),
        )
        assert receipt.granted is True
        assert receipt.task_id == "t2"
        rows = _rows(*Backlog.load(backlog_path).entries)
        ok, reason = verify_claim_receipt(receipt, rows, chain)
        assert ok, reason


class TestDeterminism:
    def test_replay_at_different_wall_clock_is_byte_identical(self, tmp_path: Path, monkeypatch) -> None:
        import bernstein.core.tasks.claim as claim_mod

        entries = [BacklogEntry(id="t1")]
        claim_filter = ClaimFilter()

        monkeypatch.setattr(claim_mod.time, "time", lambda: 1000.0)
        first, _, _ = _claim_and_receipt(tmp_path / "run-a", entries=list(entries), claim_filter=claim_filter)

        monkeypatch.setattr(claim_mod.time, "time", lambda: 5000.0)
        second, _, _ = _claim_and_receipt(tmp_path / "run-b", entries=list(entries), claim_filter=claim_filter)

        assert first.receipt_hash == second.receipt_hash


class TestTamperDetection:
    def test_tampered_fingerprint_fails(self, tmp_path: Path) -> None:
        receipt, chain, backlog_path = _claim_and_receipt(
            tmp_path, entries=[BacklogEntry(id="t1")], claim_filter=ClaimFilter()
        )
        rows = _rows(*Backlog.load(backlog_path).entries)
        forged = replace(receipt, claimer_card_fingerprint="sha256:evil")
        ok, reason = verify_claim_receipt(forged, rows, chain)
        assert not ok
        assert reason is not None and "signature" in reason

    def test_tampered_backlog_head_fails(self, tmp_path: Path) -> None:
        receipt, chain, backlog_path = _claim_and_receipt(
            tmp_path, entries=[BacklogEntry(id="t1")], claim_filter=ClaimFilter()
        )
        rows = _rows(*Backlog.load(backlog_path).entries)
        forged = replace(receipt, backlog_head="sha256:" + "0" * 64)
        ok, reason = verify_claim_receipt(forged, rows, chain)
        assert not ok
        assert reason is not None and "backlog_head" in reason

    def test_tampered_chain_head_fails(self, tmp_path: Path) -> None:
        receipt, chain, backlog_path = _claim_and_receipt(
            tmp_path, entries=[BacklogEntry(id="t1")], claim_filter=ClaimFilter()
        )
        rows = _rows(*Backlog.load(backlog_path).entries)
        forged = replace(receipt, chain_head="f" * 64)
        ok, _reason = verify_claim_receipt(forged, rows, chain)
        assert not ok

    def test_tampered_audit_chain_fails_end_to_end(self, tmp_path: Path) -> None:
        receipt, _chain, backlog_path = _claim_and_receipt(
            tmp_path, entries=[BacklogEntry(id="t1")], claim_filter=ClaimFilter()
        )
        rows = _rows(*Backlog.load(backlog_path).entries)
        # Corrupt the on-disk audit chain, then verify from a fresh store.
        audit_files = sorted((tmp_path / "audit").glob("*.jsonl"))
        assert audit_files
        content = audit_files[0].read_text(encoding="utf-8")
        audit_files[0].write_text(content.replace("mcp_claim", "tampered"), encoding="utf-8")
        reloaded = AuditChainStore(tmp_path / "audit", key=_KEY)
        ok, reason = verify_claim_receipt(receipt, rows, reloaded)
        assert not ok
        assert reason is not None and "chain" in reason


class TestOfflineStatelessVerify:
    def test_second_store_verifies_from_disk_only(self, tmp_path: Path) -> None:
        # A fresh AuditChainStore over the same on-disk state (a second server
        # process) answers the verify with no in-memory session (#2506).
        receipt, _, backlog_path = _claim_and_receipt(
            tmp_path, entries=[BacklogEntry(id="t1")], claim_filter=ClaimFilter()
        )
        rows = _rows(*Backlog.load(backlog_path).entries)
        second = AuditChainStore(tmp_path / "audit", key=_KEY)
        ok, reason = verify_claim_receipt(receipt, rows, second)
        assert ok, reason
