"""Additive audit-chain events for taint decisions and quarantine.

Every egress taint decision and every quarantine extraction is anchored into
the HMAC-chained audit log via ``log_with_prev_digest`` so the decision is
reconstructable offline to the bytes it acted on.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest

from bernstein.core.security.audit import AUDIT_KEY_ENV
from bernstein.core.security.audit_chain import (
    EVENT_PROVENANCE_QUARANTINE,
    EVENT_PROVENANCE_TAINT_DECISION,
    AuditChainStore,
    record_provenance_quarantine,
    record_taint_decision,
)


@pytest.fixture
def chain(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AuditChainStore:
    key_path = tmp_path / "audit.key"
    key_path.write_bytes(b"k" * 64)
    key_path.chmod(stat.S_IRUSR | stat.S_IWUSR)
    monkeypatch.setenv(AUDIT_KEY_ENV, str(key_path))
    return AuditChainStore(tmp_path / "audit")


def test_event_constants_are_stable_strings() -> None:
    assert EVENT_PROVENANCE_TAINT_DECISION == "provenance.taint_decision"
    assert EVENT_PROVENANCE_QUARANTINE == "provenance.quarantine"


def test_record_taint_decision_embeds_prev_digest(chain: AuditChainStore) -> None:
    event = record_taint_decision(
        chain=chain,
        target="sha256:" + "2" * 64,
        trust="public",
        tainted=True,
        decision="ask",
        actor="agent:worker",
    )
    assert event.event_type == EVENT_PROVENANCE_TAINT_DECISION
    assert "prev_chain_digest" in event.details
    assert event.details["trust"] == "public"
    assert event.details["tainted"] is True
    ok, errs = chain.verify()
    assert ok, errs


def test_record_quarantine_anchors_source_hash(chain: AuditChainStore) -> None:
    event = record_provenance_quarantine(
        chain=chain,
        source_content_hash="sha256:" + "a" * 64,
        extracted_fields=("number", "labels"),
        withheld_fields=("body", "title"),
        actor="mcp:gateway",
    )
    assert event.event_type == EVENT_PROVENANCE_QUARANTINE
    assert event.details["source_content_hash"] == "sha256:" + "a" * 64
    assert "body" in event.details["withheld_fields"]
    ok, errs = chain.verify()
    assert ok, errs
