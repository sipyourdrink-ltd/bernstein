"""CLI verify path for claim receipts (#2555, Phase 4).

``bernstein backlog verify-claim`` walks a claim receipt against the on-disk
backlog and audit chain, offline. These tests build a real receipt through
the substrate claim path, then verify (and tamper-check) it through the CLI.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from click.testing import CliRunner

from bernstein.cli.commands.task_cmd import backlog_group
from bernstein.core.protocols.mcp.claim_receipt import (
    ClaimReceipt,
    backlog_head,
    filter_digest,
    sign_claim_receipt,
)
from bernstein.core.security.audit_chain import AuditChainStore, record_task_claim_receipt
from bernstein.core.server.dashboard_tokens import resolve_dashboard_hmac_key
from bernstein.core.tasks.claim import Backlog, BacklogEntry, ClaimFilter, claim_next_entry

if TYPE_CHECKING:
    from pathlib import Path


def _build_receipt(sdd_dir: Path, backlog_path: Path) -> ClaimReceipt:
    Backlog.write(backlog_path, [BacklogEntry(id="t1", role="backend")])
    key = resolve_dashboard_hmac_key(sdd_dir)
    chain = AuditChainStore(sdd_dir / "audit", key=key)
    claim_filter = ClaimFilter(role="backend")
    entry = claim_next_entry(backlog_path, claimer_id="worker-1", filter=claim_filter)
    assert entry is not None
    event = record_task_claim_receipt(
        chain=chain,
        task_id=entry.id,
        role=entry.role or "",
        claimed_by="worker-1",
        depends_on=list(entry.depends_on),
        task_version=entry.attempts,
        claim_path="mcp_claim",
    )
    rows = [e.to_dict() for e in Backlog.load(backlog_path).entries]
    receipt = ClaimReceipt.granted_receipt(
        task_id=entry.id,
        claimer_card_fingerprint="sha256:fp",
        backlog_head=backlog_head(rows),
        filter_digest=filter_digest(claim_filter),
        chain_head=str(event.details.get("prev_chain_digest", "")),
    )
    from bernstein.core.lineage.identity import load_or_create_signing_identity

    priv, pub = load_or_create_signing_identity(
        sdd_dir / "identity", private_name="claim_signing.pem", public_name="claim_signing.pub"
    )
    return sign_claim_receipt(receipt, private_key_pem=priv, public_key_pem=pub)


def test_verify_claim_ok(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    sdd.mkdir()
    backlog_path = sdd / "runtime" / "task-backlog.json"
    receipt = _build_receipt(sdd, backlog_path)
    receipt_file = tmp_path / "receipt.json"
    receipt_file.write_text(json.dumps(receipt.to_wire()), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        backlog_group,
        [
            "verify-claim",
            "--receipt",
            str(receipt_file),
            "--backlog",
            str(backlog_path),
            "--audit-dir",
            str(sdd / "audit"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert "OK" in result.output


def test_verify_claim_detects_tamper(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    sdd.mkdir()
    backlog_path = sdd / "runtime" / "task-backlog.json"
    receipt = _build_receipt(sdd, backlog_path)
    wire = receipt.to_wire()
    wire["claimerCardFingerprint"] = "sha256:evil"  # tamper without re-signing
    receipt_file = tmp_path / "receipt.json"
    receipt_file.write_text(json.dumps(wire), encoding="utf-8")

    runner = CliRunner()
    result = runner.invoke(
        backlog_group,
        [
            "verify-claim",
            "--receipt",
            str(receipt_file),
            "--backlog",
            str(backlog_path),
            "--audit-dir",
            str(sdd / "audit"),
        ],
    )
    assert result.exit_code == 1
    assert "FAIL" in result.output
