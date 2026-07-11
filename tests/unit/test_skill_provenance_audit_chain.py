"""Audit-chain event tests for skill provenance (issue #2301)."""

from __future__ import annotations

from pathlib import Path

from bernstein.core.security.audit_chain import (
    EVENT_SKILL_INSTALL_RECEIPT,
    EVENT_SKILL_USAGE,
    AuditChainStore,
    record_skill_install_receipt,
    record_skill_usage,
)

_KEY = b"0" * 32


def test_record_skill_install_receipt_embeds_anchor_and_chains(tmp_path: Path) -> None:
    chain = AuditChainStore(tmp_path, key=_KEY)
    ev = record_skill_install_receipt(
        chain=chain,
        skill_hash="a" * 64,
        manifest_hash="b" * 64,
        install_id="i1",
        spine_anchor="sha256:" + "c" * 64,
    )
    assert ev.event_type == EVENT_SKILL_INSTALL_RECEIPT
    assert ev.details["spine_anchor"] == "sha256:" + "c" * 64
    assert ev.details["prev_chain_digest"]  # chain head embedded
    ok, errors = chain.verify()
    assert ok, errors


def test_record_skill_usage_binds_run_head(tmp_path: Path) -> None:
    chain = AuditChainStore(tmp_path, key=_KEY)
    ev = record_skill_usage(
        chain=chain,
        skill_hash="a" * 64,
        run_id="run-1",
        journal_head="sha256:" + "d" * 64,
    )
    assert ev.event_type == EVENT_SKILL_USAGE
    assert ev.details["run_id"] == "run-1"
    assert ev.details["journal_head"] == "sha256:" + "d" * 64
    ok, _ = chain.verify()
    assert ok
