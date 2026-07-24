"""Tests for :mod:`bernstein.core.trackers.odata_writeback`.

Covers acceptance criterion 3 from issue #2886: typed 412 / 428 conflicts, a
successful write-back that emits an audit event + lineage receipt whose payload
hash matches the sent body, and offline verifiability via ``bernstein audit
verify``.
"""

from __future__ import annotations

import hashlib
import json
import pathlib

import pytest

from bernstein.core.security.audit_chain import EVENT_ODATA_WRITEBACK, AuditChainStore
from bernstein.core.trackers.odata_writeback import (
    WriteBackReceipt,
    canonical_payload_hash,
    update_entity,
)
from bernstein.core.trigger_sources.odata_poll import (
    OdataConflict,
    OdataConnection,
    OdataHttpClient,
)
from tests.unit.odata.fake_service import FakeODataService


def _connection(**overrides: object) -> OdataConnection:
    base: dict[str, object] = {
        "service_root": "http://odata.test",
        "entity_set": "Widgets",
        "timestamp_property": "modified",
        "key_properties": ("id",),
        "name": "erp",
    }
    base.update(overrides)
    return OdataConnection(**base)  # type: ignore[arg-type]


def _chain(tmp_path: pathlib.Path) -> AuditChainStore:
    return AuditChainStore(tmp_path / "audit")


def _seeded() -> FakeODataService:
    svc = FakeODataService(page_size=10)
    svc.seed(1, timestamp="2026-01-01T00:00:01Z", name="alpha")
    return svc


# ---------------------------------------------------------------------------
# Independent payload-hash reference
# ---------------------------------------------------------------------------


def _reference_hash(patch: dict[str, object]) -> str:
    body = json.dumps(patch, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(body).hexdigest()


def test_canonical_payload_hash_matches_independent_sha256() -> None:
    patch = {"name": "renamed", "priority": 3}
    assert canonical_payload_hash(patch) == _reference_hash(patch)


# ---------------------------------------------------------------------------
# AC3 -- successful write-back emits a receipt-anchored audit event
# ---------------------------------------------------------------------------


def test_successful_writeback_emits_receipt_and_audit_event(tmp_path: pathlib.Path) -> None:
    svc = _seeded()
    conn = _connection()
    client = OdataHttpClient(conn, http_client=svc.client())
    chain = _chain(tmp_path)
    patch = {"name": "renamed"}

    receipt = update_entity(conn, {"id": 1}, patch, chain=chain, http_client=client)

    assert isinstance(receipt, WriteBackReceipt)
    assert receipt.entity_set == "Widgets"
    assert receipt.entity_key == "id=1"
    assert receipt.http_status == 200
    assert receipt.etag_observed == 'W/"1"'
    assert receipt.payload_content_hash == _reference_hash(patch)

    events = chain.query(event_type=EVENT_ODATA_WRITEBACK)
    assert len(events) == 1
    details = events[0].details
    assert details["entity_set"] == "Widgets"
    assert details["entity_key"] == "id=1"
    assert details["etag_observed"] == 'W/"1"'
    assert details["payload_content_hash"] == _reference_hash(patch)
    assert details["http_status"] == 200
    # The receipt is bound to the recorded chain event.
    assert receipt.audit_event_hmac == events[0].hmac


def test_writeback_audit_event_verifies_offline(tmp_path: pathlib.Path) -> None:
    svc = _seeded()
    conn = _connection()
    client = OdataHttpClient(conn, http_client=svc.client())
    chain = _chain(tmp_path)

    update_entity(conn, {"id": 1}, {"name": "renamed"}, chain=chain, http_client=client)

    ok, errors = chain.verify()
    assert ok, errors


def test_writeback_verifies_through_audit_verify_cli(tmp_path: pathlib.Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``bernstein audit verify`` covers the write-back receipt with no new verb."""
    from pathlib import Path

    from click.testing import CliRunner

    from bernstein.cli.commands.audit_cmd import audit_group

    monkeypatch.chdir(tmp_path)
    svc = _seeded()
    conn = _connection()
    client = OdataHttpClient(conn, http_client=svc.client())
    chain = AuditChainStore(Path(".sdd/audit"))
    update_entity(conn, {"id": 1}, {"name": "renamed"}, chain=chain, http_client=client)

    ok_run = CliRunner().invoke(audit_group, ["verify", "--hmac-only"])
    assert ok_run.exit_code == 0, ok_run.output

    # Tamper the recorded event and re-run: the CLI must now exit non-zero.
    log = next(iter(sorted(Path(".sdd/audit").glob("*.jsonl"))))
    log.write_text(log.read_text().replace('"entity_key": "id=1"', '"entity_key": "id=9"'))
    bad_run = CliRunner().invoke(audit_group, ["verify", "--hmac-only"])
    assert bad_run.exit_code != 0, bad_run.output


def test_writeback_tamper_breaks_chain(tmp_path: pathlib.Path) -> None:
    svc = _seeded()
    conn = _connection()
    client = OdataHttpClient(conn, http_client=svc.client())
    chain = _chain(tmp_path)
    update_entity(conn, {"id": 1}, {"name": "renamed"}, chain=chain, http_client=client)

    # The raw payload is never stored -- only its content hash. Tamper the
    # recorded entity key (a field bound into the HMAC): verify must fail.
    audit_files = list((tmp_path / "audit").glob("*.jsonl"))
    assert audit_files
    log = audit_files[0]
    text = log.read_text()
    assert '"entity_key": "id=1"' in text
    log.write_text(text.replace('"entity_key": "id=1"', '"entity_key": "id=9"'))

    ok, _errors = chain.verify()
    assert not ok


# ---------------------------------------------------------------------------
# AC3 -- typed conflicts (412 + 428)
# ---------------------------------------------------------------------------


def test_stale_etag_raises_412_conflict(tmp_path: pathlib.Path) -> None:
    svc = _seeded()
    svc.bump_on_entity_get = True  # concurrent edit between GET and PATCH
    conn = _connection()
    client = OdataHttpClient(conn, http_client=svc.client())
    chain = _chain(tmp_path)

    with pytest.raises(OdataConflict) as excinfo:
        update_entity(conn, {"id": 1}, {"name": "renamed"}, chain=chain, http_client=client)
    assert excinfo.value.status == 412
    # No success event is recorded for a conflict.
    assert chain.query(event_type=EVENT_ODATA_WRITEBACK) == []


def test_missing_etag_raises_428_conflict(tmp_path: pathlib.Path) -> None:
    svc = _seeded()
    svc.expose_etag = False  # service does not surface an ETag for the entity
    conn = _connection()
    client = OdataHttpClient(conn, http_client=svc.client())
    chain = _chain(tmp_path)

    with pytest.raises(OdataConflict) as excinfo:
        update_entity(conn, {"id": 1}, {"name": "renamed"}, chain=chain, http_client=client)
    assert excinfo.value.status == 428
    assert chain.query(event_type=EVENT_ODATA_WRITEBACK) == []


# ---------------------------------------------------------------------------
# AC3 -- draft-activate flow
# ---------------------------------------------------------------------------


def test_draft_flow_creates_patches_activates(tmp_path: pathlib.Path) -> None:
    svc = FakeODataService(page_size=10, activate_action="Activate")
    conn = _connection(draft_flow=True, draft_activate_action="Activate")
    client = OdataHttpClient(conn, http_client=svc.client())
    chain = _chain(tmp_path)

    patch = {"name": "drafted"}
    receipt = update_entity(conn, {"id": 0}, patch, chain=chain, http_client=client)

    assert receipt.draft_flow is True
    assert receipt.http_status == 200
    assert receipt.payload_content_hash == _reference_hash(patch)
    events = chain.query(event_type=EVENT_ODATA_WRITEBACK)
    assert len(events) == 1
    assert events[0].details["draft_flow"] is True
    assert events[0].details["activate_action"] == "Activate"
