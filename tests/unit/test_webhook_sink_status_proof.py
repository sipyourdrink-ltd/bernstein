"""Chain-anchored status proofs on the outbound webhook sink (#2512).

The proof rides inside the existing payload as one additional key, so a
consumer written against the plain :meth:`NotificationEvent.to_payload` body
keeps parsing it, while a consumer that cares can verify the reported status
against the audit chain rather than trusting the transport.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from bernstein.core.notifications.protocol import NotificationEvent, NotificationEventKind
from bernstein.core.notifications.sinks.webhook import WebhookSink
from bernstein.core.security.audit import load_or_create_audit_key
from bernstein.core.trigger_sources.receipt import (
    PROOF_ENVELOPE_KEY,
    verify_receipt_document,
)


@pytest.fixture()
def sdd_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("BERNSTEIN_AUDIT_KEY_PATH", str(tmp_path / "audit.key"))
    monkeypatch.delenv("BERNSTEIN_AUTOMATION_BRIDGE_ROOT", raising=False)
    return tmp_path / ".sdd"


def _event(*, event_id: str = "evt-1", severity: str = "error") -> NotificationEvent:
    return NotificationEvent(
        event_id=event_id,
        kind=NotificationEventKind.POST_TASK,
        title="Task t-42 finished",
        body="worker exited non-zero",
        severity=severity,
        task_id="t-42",
        run_id="run-9",
        timestamp=1_700_000_500.0,
        details={"status": "failed"},
    )


def _sink(sdd_dir: Path, **overrides: Any) -> WebhookSink:
    config: dict[str, Any] = {
        "id": "ops",
        "kind": "webhook",
        "url": "https://hooks.example.com/bernstein",
        "sdd_dir": str(sdd_dir),
    }
    return WebhookSink(config | overrides)


def _verify(sdd_dir: Path, tmp_path: Path, envelope: dict[str, Any]):
    return verify_receipt_document(
        envelope,
        audit_dir=sdd_dir / "audit",
        hmac_key=load_or_create_audit_key(tmp_path / "audit.key"),
    )


def test_delivered_body_carries_a_verifiable_status_proof(sdd_dir: Path, tmp_path: Path) -> None:
    """The callback the platform receives re-verifies against the chain."""
    envelope = _sink(sdd_dir)._body(_event())

    assert PROOF_ENVELOPE_KEY in envelope
    result = _verify(sdd_dir, tmp_path, envelope)
    assert result.ok, result.reason
    assert result.chain_status == "failed"


def test_the_envelope_is_additive(sdd_dir: Path) -> None:
    """Every key the plain payload carried survives under its original name."""
    event = _event()
    plain = event.to_payload()
    envelope = _sink(sdd_dir)._body(event)

    for key, value in plain.items():
        assert envelope[key] == value
    assert set(envelope) == set(plain) | {PROOF_ENVELOPE_KEY}


def test_status_flipped_in_transit_fails_and_reports_the_chain_status(
    sdd_dir: Path,
    tmp_path: Path,
) -> None:
    """A downstream step told 'succeeded' learns the chain recorded 'failed'."""
    envelope = _sink(sdd_dir)._body(_event())
    envelope[PROOF_ENVELOPE_KEY]["status"] = "succeeded"

    result = _verify(sdd_dir, tmp_path, envelope)
    assert not result.ok
    assert result.chain_status == "failed"


def test_rewriting_the_carried_payload_fails_verification(sdd_dir: Path, tmp_path: Path) -> None:
    """Editing the event body breaks the producing-event digest."""
    envelope = _sink(sdd_dir)._body(_event())
    envelope["details"] = {"status": "succeeded"}

    result = _verify(sdd_dir, tmp_path, envelope)
    assert not result.ok


def test_retried_delivery_is_byte_identical(sdd_dir: Path) -> None:
    """A re-sent callback repeats the same envelope bytes, not a new claim."""
    sink = _sink(sdd_dir)
    event = _event()

    first = json.dumps(sink._body(event), sort_keys=True)
    second = json.dumps(sink._body(event), sort_keys=True)
    assert first == second


def test_distinct_events_get_distinct_anchors(sdd_dir: Path) -> None:
    """Two different events are anchored separately."""
    sink = _sink(sdd_dir)
    left = sink._body(_event(event_id="evt-1"))
    right = sink._body(_event(event_id="evt-2"))

    assert left[PROOF_ENVELOPE_KEY]["chain_entry_hash"] != right[PROOF_ENVELOPE_KEY]["chain_entry_hash"]


def test_proof_can_be_disabled(sdd_dir: Path) -> None:
    """An operator who does not want the envelope gets the plain payload."""
    envelope = _sink(sdd_dir, status_proof=False)._body(_event())
    assert PROOF_ENVELOPE_KEY not in envelope


def test_unanchorable_install_still_delivers(monkeypatch: pytest.MonkeyPatch, sdd_dir: Path) -> None:
    """A bridge failure degrades to the plain payload rather than dropping it."""
    from bernstein.core.trigger_sources import receipt as receipt_mod

    def _boom(**_kw: Any) -> None:
        raise OSError("audit volume unavailable")

    monkeypatch.setattr(receipt_mod, "emit_status_proof", _boom)
    envelope = _sink(sdd_dir)._body(_event())

    assert PROOF_ENVELOPE_KEY not in envelope
    assert envelope["title"] == "Task t-42 finished"
