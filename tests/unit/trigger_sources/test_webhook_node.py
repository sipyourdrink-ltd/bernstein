"""Signed inbound/outbound webhook-node receipt tests (issue #2310).

Each test maps to an acceptance criterion:

* AC1 -- an inbound webhook writes a signed receipt and spawns a run whose
  journal root references the inbound ``event_hash``.
* AC2 -- the outbound webhook carries a signed ``result_hash`` matching the
  journal head.
* AC3 -- ``verify_webhook_event`` recomputes inbound and outbound hashes and
  detects tampering.
* AC4 -- inbound signatures are verified per Standard Webhooks; an invalid
  signature is rejected.
* AC5 -- retry/backoff replays the same inbound event without creating a
  duplicate run.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.trigger_sources.webhook_node import (
    STANDARD_WEBHOOK_ID_HEADER,
    STANDARD_WEBHOOK_SIGNATURE_HEADER,
    STANDARD_WEBHOOK_TIMESTAMP_HEADER,
    WebhookNodeError,
    compute_result_hash,
    emit_outbound_receipt,
    read_inbound_receipt,
    receive_inbound_webhook,
    sign_standard_webhook,
    verify_standard_webhook,
    verify_webhook_event,
)

_KEY = b"0" * 32
_SECRET = "whsec_test_secret_value"
_SOURCE = "nocode-bus"
_BODY = b'{"goal":"ship the widget","priority":"high"}'
_EVENT_ID = "evt_2310_abcdef"
_TIMESTAMP = 1_700_000_000


def _headers(body: bytes = _BODY, *, event_id: str = _EVENT_ID, secret: str = _SECRET) -> dict[str, str]:
    sig = sign_standard_webhook(secret=secret, msg_id=event_id, timestamp=_TIMESTAMP, body=body)
    return {
        STANDARD_WEBHOOK_ID_HEADER: event_id,
        STANDARD_WEBHOOK_TIMESTAMP_HEADER: str(_TIMESTAMP),
        STANDARD_WEBHOOK_SIGNATURE_HEADER: sig,
    }


def _receive(
    tmp_path: Path,
    *,
    body: bytes = _BODY,
    event_id: str = _EVENT_ID,
    headers: dict[str, str] | None = None,
):
    return receive_inbound_webhook(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        identity_dir=tmp_path / ".sdd" / "identity",
        secret=_SECRET,
        source=_SOURCE,
        headers=headers if headers is not None else _headers(body, event_id=event_id),
        body=body,
        timestamp=_TIMESTAMP,
    )


# ---------------------------------------------------------------------------
# Standard Webhooks signing round-trip (AC4 primitive)
# ---------------------------------------------------------------------------


def test_standard_webhook_sign_verify_roundtrip() -> None:
    sig = sign_standard_webhook(secret=_SECRET, msg_id=_EVENT_ID, timestamp=_TIMESTAMP, body=_BODY)
    assert sig.startswith("v1,")
    assert verify_standard_webhook(
        secret=_SECRET, msg_id=_EVENT_ID, timestamp=_TIMESTAMP, body=_BODY, signature_header=sig
    )


def test_standard_webhook_signature_binds_body() -> None:
    sig = sign_standard_webhook(secret=_SECRET, msg_id=_EVENT_ID, timestamp=_TIMESTAMP, body=_BODY)
    assert not verify_standard_webhook(
        secret=_SECRET, msg_id=_EVENT_ID, timestamp=_TIMESTAMP, body=_BODY + b" ", signature_header=sig
    )


def test_standard_webhook_signature_binds_id_and_timestamp() -> None:
    sig = sign_standard_webhook(secret=_SECRET, msg_id=_EVENT_ID, timestamp=_TIMESTAMP, body=_BODY)
    assert not verify_standard_webhook(
        secret=_SECRET, msg_id="evt_other", timestamp=_TIMESTAMP, body=_BODY, signature_header=sig
    )
    assert not verify_standard_webhook(
        secret=_SECRET, msg_id=_EVENT_ID, timestamp=_TIMESTAMP + 1, body=_BODY, signature_header=sig
    )


def test_standard_webhook_multiple_space_separated_signatures() -> None:
    good = sign_standard_webhook(secret=_SECRET, msg_id=_EVENT_ID, timestamp=_TIMESTAMP, body=_BODY)
    header = f"v1,invalidsig {good}"
    assert verify_standard_webhook(
        secret=_SECRET, msg_id=_EVENT_ID, timestamp=_TIMESTAMP, body=_BODY, signature_header=header
    )


# ---------------------------------------------------------------------------
# AC1 -- inbound writes a signed receipt referencing the journal root
# ---------------------------------------------------------------------------


def test_inbound_writes_signed_receipt_anchored_to_journal(tmp_path: Path) -> None:
    result = _receive(tmp_path)
    receipt = result.receipt
    # signed
    assert receipt.signature
    assert receipt.signer_public_key_pem
    # receipt binds {event_hash, source, journal_root}
    assert receipt.source == _SOURCE
    assert receipt.event_hash
    assert receipt.journal_root
    # a run was spawned and its journal root references the event hash
    assert result.spawned is True
    assert result.run_id
    assert receipt.event_hash in receipt.journal_root_events()


def test_inbound_event_hash_recomputes_from_body(tmp_path: Path) -> None:
    from bernstein.core.trigger_sources.webhook_node import compute_event_hash

    result = _receive(tmp_path)
    assert result.receipt.event_hash == compute_event_hash(source=_SOURCE, event_id=_EVENT_ID, body=_BODY)


def test_inbound_receipt_persisted_and_reloadable(tmp_path: Path) -> None:
    result = _receive(tmp_path)
    reloaded = read_inbound_receipt(tmp_path, _EVENT_ID)
    assert reloaded is not None
    assert reloaded.event_hash == result.receipt.event_hash
    assert reloaded.journal_root == result.receipt.journal_root


# ---------------------------------------------------------------------------
# AC2 -- outbound carries a signed result_hash matching the journal head
# ---------------------------------------------------------------------------


def test_outbound_result_hash_matches_journal_head(tmp_path: Path) -> None:
    inbound = _receive(tmp_path)
    outbound = emit_outbound_receipt(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        identity_dir=tmp_path / ".sdd" / "identity",
        event_id=_EVENT_ID,
        result={"status": "succeeded", "pr_url": "https://example/pr/1"},
        journal_head="head-hash-deadbeef",
        timestamp=_TIMESTAMP,
    )
    assert outbound.result_hash == compute_result_hash({"status": "succeeded", "pr_url": "https://example/pr/1"})
    assert outbound.journal_head == "head-hash-deadbeef"
    assert outbound.signature
    assert inbound.receipt.event_hash  # inbound existed first


def test_outbound_delivery_headers_are_standard_webhooks(tmp_path: Path) -> None:
    _receive(tmp_path)
    outbound = emit_outbound_receipt(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        identity_dir=tmp_path / ".sdd" / "identity",
        event_id=_EVENT_ID,
        result={"status": "succeeded"},
        journal_head="head-hash",
        timestamp=_TIMESTAMP,
        delivery_secret=_SECRET,
    )
    headers, body = outbound.delivery(secret=_SECRET)
    assert STANDARD_WEBHOOK_SIGNATURE_HEADER in headers
    assert verify_standard_webhook(
        secret=_SECRET,
        msg_id=headers[STANDARD_WEBHOOK_ID_HEADER],
        timestamp=int(headers[STANDARD_WEBHOOK_TIMESTAMP_HEADER]),
        body=body,
        signature_header=headers[STANDARD_WEBHOOK_SIGNATURE_HEADER],
    )


# ---------------------------------------------------------------------------
# AC3 -- verify recomputes inbound + outbound hashes and detects tampering
# ---------------------------------------------------------------------------


def test_verify_ok_after_inbound_and_outbound(tmp_path: Path) -> None:
    _receive(tmp_path)
    emit_outbound_receipt(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        identity_dir=tmp_path / ".sdd" / "identity",
        event_id=_EVENT_ID,
        result={"status": "succeeded"},
        journal_head="head-hash",
        timestamp=_TIMESTAMP,
    )
    result = verify_webhook_event(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        event_id=_EVENT_ID,
    )
    assert result.ok
    assert result.inbound_ok
    assert result.outbound_ok


def test_verify_detects_tampered_inbound_receipt(tmp_path: Path) -> None:
    _receive(tmp_path)
    path = tmp_path / ".sdd" / "webhook-node" / "inbound" / f"{_EVENT_ID}.json"
    raw = path.read_text(encoding="utf-8")
    path.write_text(raw.replace(_SOURCE, "attacker-bus"), encoding="utf-8")
    result = verify_webhook_event(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        event_id=_EVENT_ID,
    )
    assert not result.ok
    assert not result.inbound_ok


def test_verify_detects_tampered_outbound_result(tmp_path: Path) -> None:
    _receive(tmp_path)
    emit_outbound_receipt(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        identity_dir=tmp_path / ".sdd" / "identity",
        event_id=_EVENT_ID,
        result={"status": "succeeded"},
        journal_head="head-hash",
        timestamp=_TIMESTAMP,
    )
    path = tmp_path / ".sdd" / "webhook-node" / "outbound" / f"{_EVENT_ID}.json"
    raw = path.read_text(encoding="utf-8")
    path.write_text(raw.replace("head-hash", "forged-head"), encoding="utf-8")
    result = verify_webhook_event(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        event_id=_EVENT_ID,
    )
    assert not result.ok
    assert not result.outbound_ok


def test_verify_missing_event_is_not_ok(tmp_path: Path) -> None:
    result = verify_webhook_event(
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_KEY,
        event_id="evt_does_not_exist",
    )
    assert not result.ok
    assert result.receipt is None


# ---------------------------------------------------------------------------
# AC4 -- invalid Standard Webhooks signature is rejected at inbound
# ---------------------------------------------------------------------------


def test_inbound_rejects_invalid_signature(tmp_path: Path) -> None:
    bad_headers = _headers()
    bad_headers[STANDARD_WEBHOOK_SIGNATURE_HEADER] = "v1,dGFtcGVyZWQ="
    with pytest.raises(WebhookNodeError):
        _receive(tmp_path, headers=bad_headers)


def test_inbound_rejects_wrong_secret(tmp_path: Path) -> None:
    wrong = _headers(secret="whsec_wrong")
    with pytest.raises(WebhookNodeError):
        _receive(tmp_path, headers=wrong)


def test_inbound_rejects_missing_signature_header(tmp_path: Path) -> None:
    headers = _headers()
    del headers[STANDARD_WEBHOOK_SIGNATURE_HEADER]
    with pytest.raises(WebhookNodeError):
        _receive(tmp_path, headers=headers)


def test_inbound_rejected_signature_writes_no_receipt(tmp_path: Path) -> None:
    bad_headers = _headers()
    bad_headers[STANDARD_WEBHOOK_SIGNATURE_HEADER] = "v1,dGFtcGVyZWQ="
    with pytest.raises(WebhookNodeError):
        _receive(tmp_path, headers=bad_headers)
    assert read_inbound_receipt(tmp_path, _EVENT_ID) is None


# ---------------------------------------------------------------------------
# AC5 -- retry replays the same event without a duplicate run
# ---------------------------------------------------------------------------


def test_retry_same_event_does_not_spawn_duplicate_run(tmp_path: Path) -> None:
    first = _receive(tmp_path)
    assert first.spawned is True
    second = _receive(tmp_path)
    # replay: same event id -> no new run, same receipt returned
    assert second.spawned is False
    assert second.run_id == first.run_id
    assert second.receipt.event_hash == first.receipt.event_hash


def test_retry_returns_idempotent_receipt_identity(tmp_path: Path) -> None:
    first = _receive(tmp_path)
    second = _receive(tmp_path)
    assert second.receipt.event_hash == first.receipt.event_hash
    assert second.receipt.journal_root == first.receipt.journal_root


def test_distinct_event_ids_spawn_distinct_runs(tmp_path: Path) -> None:
    first = _receive(tmp_path, event_id="evt_a")
    second = _receive(tmp_path, event_id="evt_b")
    assert first.run_id != second.run_id
    assert first.spawned is True
    assert second.spawned is True


# ---------------------------------------------------------------------------
# Determinism / audit-chain mirror
# ---------------------------------------------------------------------------


def test_inbound_signature_verifies_offline(tmp_path: Path) -> None:
    from bernstein.core.skills.catalog.signature import verify_payload

    receipt = _receive(tmp_path).receipt
    outcome = verify_payload(
        receipt.to_canonical_bytes(),
        receipt.signature,
        receipt.signer_public_key_pem,
        allow_unverified=True,
    )
    assert outcome.verified
