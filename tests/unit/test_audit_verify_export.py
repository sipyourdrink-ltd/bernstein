"""Tests for ``bernstein audit verify-export`` CLI command.

Covers: valid bundle passes, tampered prev_hmac fails, gap in sequence fails,
invalid signature fails.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path

from click.testing import CliRunner

from bernstein.cli.commands.audit_cmd import audit_group


def _build_segment_receipt_payload(
    first_sequence: int,
    last_sequence: int,
    chain_head_hash: str,
    key: bytes | None = None,
) -> dict[str, object]:
    """Build a signed segment receipt payload."""
    if key is None:
        key = b"test_audit_export_key"
    payload = {
        "first_sequence": first_sequence,
        "last_sequence": last_sequence,
        "chain_head_hash": chain_head_hash,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    signature = hmac.new(key, canonical, hashlib.sha256).hexdigest()
    return {
        "first_sequence": first_sequence,
        "last_sequence": last_sequence,
        "chain_head_hash": chain_head_hash,
        "signature": signature,
    }


def _make_events(count: int, key: bytes | None = None) -> tuple[list[dict[str, object]], str]:
    """Create a chain of events with prev_hmac and hmac linking.

    Returns (events, genesis_prev_hmac) where genesis_prev_hmac is the
    _GENESIS_HMAC that the first event's prev_hmac should match.
    """
    genesis_prev = "0" * 64
    events = []
    prev_hmac = genesis_prev
    for i in range(1, count + 1):
        hmac_val = f"hmac{i}"
        events.append(
            {
                "timestamp": 1700000000.0 + i,
                "event_type": "task.created",
                "actor": "admin@example.com",
                "resource": f"task-{i}",
                "action": "create",
                "outcome": "success",
                "details": {},
                "hmac": hmac_val,
                "prev_hmac": prev_hmac,
                "sequence": i,
            }
        )
        prev_hmac = hmac_val
    return events, genesis_prev


def _make_bundle(
    events: list[dict[str, object]],
    first_seq: int,
    last_seq: int,
    last_hmac: str,
    key: bytes | None = None,
) -> dict[str, object]:
    """Build a valid export bundle."""
    receipt = _build_segment_receipt_payload(first_seq, last_seq, last_hmac, key)
    return {
        "segment_receipt": receipt,
        "events": events,
    }


# -----------------------------------------------------------------------
# Valid bundle
# -----------------------------------------------------------------------


def test_valid_bundle_passes(tmp_path: Path) -> None:
    """A well-formed bundle with contiguous sequence passes verification."""
    events, _ = _make_events(3)
    last_hmac = events[-1]["hmac"]
    bundle = _make_bundle(events, first_seq=1, last_seq=3, last_hmac=last_hmac)

    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")

    result = CliRunner().invoke(audit_group, ["verify-export", "--bundle", str(bundle_path)])
    assert result.exit_code == 0, result.output
    assert "PASSED" in result.output


def test_valid_bundle_passes_with_explicit_key(tmp_path: Path) -> None:
    """A well-formed bundle verifies with an explicit HMAC key."""
    key = b"test_audit_export_key"
    events, _ = _make_events(3)
    last_hmac = events[-1]["hmac"]
    bundle = _make_bundle(events, first_seq=1, last_seq=3, last_hmac=last_hmac, key=key)

    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")

    key_path = tmp_path / "key.txt"
    key_path.write_bytes(key)

    result = CliRunner().invoke(audit_group, ["verify-export", "--bundle", str(bundle_path), "--key", str(key_path)])
    assert result.exit_code == 0, result.output
    assert "PASSED" in result.output


# -----------------------------------------------------------------------
# Tampered prev_hmac
# -----------------------------------------------------------------------


def test_tampered_prev_hmac_fails(tmp_path: Path) -> None:
    """An event whose prev_hmac doesn't match the prior event's hmac fails."""
    events, _ = _make_events(3)
    # Tamper: flip the first event's prev_hmac
    events[0]["prev_hmac"] = "deadbeef" + "0" * 56

    last_hmac = events[-1]["hmac"]
    bundle = _make_bundle(events, first_seq=1, last_seq=3, last_hmac=last_hmac)

    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")

    result = CliRunner().invoke(audit_group, ["verify-export", "--bundle", str(bundle_path)])
    assert result.exit_code == 1, result.output
    assert "FAILED" in result.output
    assert "prev_hmac mismatch" in result.output


def test_tampered_middle_prev_hmac_fails(tmp_path: Path) -> None:
    """A tampered prev_hmac in the middle of the chain fails."""
    events, _ = _make_events(5)
    # Tamper: corrupt prev_hmac of event 3
    events[2]["prev_hmac"] = "beefdead" + "0" * 56

    last_hmac = events[-1]["hmac"]
    bundle = _make_bundle(events, first_seq=1, last_seq=5, last_hmac=last_hmac)

    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")

    result = CliRunner().invoke(audit_group, ["verify-export", "--bundle", str(bundle_path)])
    assert result.exit_code == 1, result.output
    assert "FAILED" in result.output
    assert "prev_hmac mismatch" in result.output


# -----------------------------------------------------------------------
# Gap in sequence
# -----------------------------------------------------------------------


def test_gap_in_sequence_fails(tmp_path: Path) -> None:
    """A gap in sequence numbers is detected and reported."""
    events, _ = _make_events(5)
    # Remove event 3: creates gap at sequence 3
    del events[2]

    last_hmac = events[-1]["hmac"]
    # Bundle claims first=1, last=4 (but events now have 4 items with seqs 1,2,4,5)
    bundle = _make_bundle(events, first_seq=1, last_seq=4, last_hmac=last_hmac)

    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")

    result = CliRunner().invoke(audit_group, ["verify-export", "--bundle", str(bundle_path)])
    assert result.exit_code == 1, result.output
    assert "FAILED" in result.output
    # Gap detected at sequence 3
    assert "gap" in result.output.lower()


def test_sequence_reorder_fails(tmp_path: Path) -> None:
    """Events with out-of-order sequence numbers are detected."""
    events, _ = _make_events(3)
    # Swap sequence numbers of events 1 and 2
    events[0]["sequence"] = 2
    events[1]["sequence"] = 1

    last_hmac = events[-1]["hmac"]
    bundle = _make_bundle(events, first_seq=1, last_seq=3, last_hmac=last_hmac)

    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")

    result = CliRunner().invoke(audit_group, ["verify-export", "--bundle", str(bundle_path)])
    assert result.exit_code == 1, result.output
    assert "FAILED" in result.output
    assert "reordered" in result.output.lower() or "gap" in result.output.lower()


# -----------------------------------------------------------------------
# Invalid signature
# -----------------------------------------------------------------------


def test_invalid_signature_fails(tmp_path: Path) -> None:
    """A bundle with a bad segment receipt signature fails."""
    key = b"test_audit_export_key"
    events, _ = _make_events(3)
    last_hmac = events[-1]["hmac"]

    # Build bundle with wrong signature
    payload = {
        "first_sequence": 1,
        "last_sequence": 3,
        "chain_head_hash": last_hmac,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    wrong_sig = hmac.new(b"wrong_key", canonical, hashlib.sha256).hexdigest()

    bundle = {
        "segment_receipt": {
            "first_sequence": 1,
            "last_sequence": 3,
            "chain_head_hash": last_hmac,
            "signature": wrong_sig,
        },
        "events": events,
    }

    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")

    key_path = tmp_path / "key.txt"
    key_path.write_bytes(key)

    result = CliRunner().invoke(audit_group, ["verify-export", "--bundle", str(bundle_path), "--key", str(key_path)])
    assert result.exit_code == 1, result.output
    assert "FAILED" in result.output
    assert "signature" in result.output.lower()


def test_missing_signature_fails(tmp_path: Path) -> None:
    """A bundle missing the segment receipt signature field fails structural check."""
    events, _ = _make_events(3)
    last_hmac = events[-1]["hmac"]

    bundle = {
        "segment_receipt": {
            "first_sequence": 1,
            "last_sequence": 3,
            "chain_head_hash": last_hmac,
            # signature intentionally missing
        },
        "events": events,
    }

    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")

    result = CliRunner().invoke(audit_group, ["verify-export", "--bundle", str(bundle_path)])
    assert result.exit_code == 1, result.output
    assert "FAILED" in result.output
    assert "signature" in result.output.lower()


# -----------------------------------------------------------------------
# Structural errors
# -----------------------------------------------------------------------


def test_missing_segment_receipt_fails(tmp_path: Path) -> None:
    """A bundle missing segment_receipt fails."""
    events, _ = _make_events(3)
    bundle = {"events": events}  # no segment_receipt

    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")

    result = CliRunner().invoke(audit_group, ["verify-export", "--bundle", str(bundle_path)])
    assert result.exit_code == 1, result.output
    assert "FAILED" in result.output
    assert "segment_receipt" in result.output.lower()


def test_missing_events_field_fails(tmp_path: Path) -> None:
    """A bundle missing events field fails."""
    last_hmac = "hmac3"
    bundle = {
        "segment_receipt": {
            "first_sequence": 1,
            "last_sequence": 3,
            "chain_head_hash": last_hmac,
            "signature": "sig",
        },
        # no events
    }

    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")

    result = CliRunner().invoke(audit_group, ["verify-export", "--bundle", str(bundle_path)])
    assert result.exit_code == 1, result.output
    assert "FAILED" in result.output
    assert "events" in result.output.lower()


def test_malformed_json_fails(tmp_path: Path) -> None:
    """A bundle that is not valid JSON fails."""
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text("{ this is not json", encoding="utf-8")

    result = CliRunner().invoke(audit_group, ["verify-export", "--bundle", str(bundle_path)])
    assert result.exit_code == 1, result.output
    assert "JSON" in result.output


# -----------------------------------------------------------------------
# Cross-check: receipt vs events
# -----------------------------------------------------------------------


def test_receipt_first_sequence_mismatch_fails(tmp_path: Path) -> None:
    """A segment receipt whose first_sequence doesn't match the first event fails."""
    events, _ = _make_events(3)
    last_hmac = events[-1]["hmac"]
    # Declare first=99 but first event has sequence=1
    bundle = _make_bundle(events, first_seq=99, last_seq=3, last_hmac=last_hmac)

    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")

    result = CliRunner().invoke(audit_group, ["verify-export", "--bundle", str(bundle_path)])
    assert result.exit_code == 1, result.output
    assert "FAILED" in result.output
    assert "first_sequence" in result.output


def test_receipt_last_sequence_mismatch_fails(tmp_path: Path) -> None:
    """A segment receipt whose last_sequence doesn't match the last event fails."""
    events, _ = _make_events(3)
    last_hmac = events[-1]["hmac"]
    # Declare last=99 but last event has sequence=3
    bundle = _make_bundle(events, first_seq=1, last_seq=99, last_hmac=last_hmac)

    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")

    result = CliRunner().invoke(audit_group, ["verify-export", "--bundle", str(bundle_path)])
    assert result.exit_code == 1, result.output
    assert "FAILED" in result.output
    assert "last_sequence" in result.output


def test_receipt_chain_head_hash_mismatch_fails(tmp_path: Path) -> None:
    """A segment receipt whose chain_head_hash doesn't match the last event's hmac fails."""
    events, _ = _make_events(3)
    # Use wrong chain_head_hash
    bundle = _make_bundle(events, first_seq=1, last_seq=3, last_hmac="wrong_hash_0000")

    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")

    result = CliRunner().invoke(audit_group, ["verify-export", "--bundle", str(bundle_path)])
    assert result.exit_code == 1, result.output
    assert "FAILED" in result.output
    assert "chain_head_hash" in result.output.lower()


# -----------------------------------------------------------------------
# Single event (genesis)
# -----------------------------------------------------------------------


def test_single_event_genesis_bundle_passes(tmp_path: Path) -> None:
    """A bundle with a single genesis event passes verification."""
    events, genesis_prev = _make_events(1)
    # For a single genesis event, prev_hmac must be genesis
    assert events[0]["prev_hmac"] == genesis_prev

    last_hmac = events[-1]["hmac"]
    bundle = _make_bundle(events, first_seq=1, last_seq=1, last_hmac=last_hmac)

    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")

    result = CliRunner().invoke(audit_group, ["verify-export", "--bundle", str(bundle_path)])
    assert result.exit_code == 0, result.output
    assert "PASSED" in result.output


# -----------------------------------------------------------------------
# Duplicate sequence
# -----------------------------------------------------------------------


def test_duplicate_sequence_fails(tmp_path: Path) -> None:
    """Duplicate sequence numbers are detected."""
    events, _ = _make_events(3)
    # Duplicate sequence 2
    events[2]["sequence"] = 2

    last_hmac = events[-1]["hmac"]
    bundle = _make_bundle(events, first_seq=1, last_seq=3, last_hmac=last_hmac)

    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(json.dumps(bundle), encoding="utf-8")

    result = CliRunner().invoke(audit_group, ["verify-export", "--bundle", str(bundle_path)])
    assert result.exit_code == 1, result.output
    assert "FAILED" in result.output
    assert "duplicate" in result.output.lower()
