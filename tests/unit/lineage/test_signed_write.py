"""Tests for the supported signed-lineage write path (issue #2960).

These cover the orchestration ``SignedLineageLog`` / ``seal_write`` perform on
top of `LineageStore`:

  * content_hash computed from the bytes about to be written.
  * Tip lookup → parent_hashes wired correctly (genesis vs successor).
  * JCS canonicalisation + HMAC envelope + Ed25519 detached JWS roundtrip.
  * OTel span emission (no-op when telemetry is disabled, which is the test default).
"""

from __future__ import annotations

import hashlib
import hmac as _hmac
import json
from pathlib import Path

import pytest

from bernstein.core.lineage.entry import canonicalise
from bernstein.core.lineage.identity import (
    AgentCard,
    generate_keypair,
    verify_detached,
)
from bernstein.core.lineage.signed_write import SignedLineageLog
from bernstein.core.lineage.store import LineageStore


@pytest.fixture
def hmac_key() -> bytes:
    # Deterministic key - these tests verify behaviour, not key handling.
    return b"0" * 64


@pytest.fixture
def card_and_keys() -> tuple[AgentCard, str]:
    priv_pem, pub_pem = generate_keypair()
    card = AgentCard(agent_id="agent:worker-1", kid="key-test-001", public_key_pem=pub_pem)
    return card, priv_pem


@pytest.fixture
def recorder(tmp_path: Path, hmac_key: bytes) -> SignedLineageLog:
    return SignedLineageLog(store=LineageStore(tmp_path / "lineage"), operator_hmac_key=hmac_key)


# ---------------------------------------------------------------------------
# Genesis (first write to an artefact)
# ---------------------------------------------------------------------------


def test_genesis_write_creates_entry_with_no_parents(
    tmp_path: Path,
    recorder: SignedLineageLog,
    card_and_keys: tuple[AgentCard, str],
) -> None:
    card, priv = card_and_keys
    h = recorder.record_write(
        artefact_path="src/foo.py",
        new_content=b"hello",
        agent_id=card.agent_id,
        agent_card=card,
        private_key_pem=priv,
        tool_call_id="tc-1",
        span_id="00f067aa0ba902b7",
    )
    entries = list(recorder.store.read_log())
    assert len(entries) == 1
    entry, _jws = entries[0]
    assert entry.parent_hashes == []
    assert entry.content_hash == "sha256:" + hashlib.sha256(b"hello").hexdigest()
    assert entry.agent_id == card.agent_id
    assert entry.tool_call_id == "tc-1"
    assert entry.span_id == "00f067aa0ba902b7"
    assert h.startswith("sha256:")


def test_successor_write_parents_chain_to_prior_tip(
    recorder: SignedLineageLog,
    card_and_keys: tuple[AgentCard, str],
) -> None:
    card, priv = card_and_keys
    h1 = recorder.record_write(
        artefact_path="src/foo.py",
        new_content=b"v1",
        agent_id=card.agent_id,
        agent_card=card,
        private_key_pem=priv,
        tool_call_id="tc-1",
        span_id="span-1",
    )
    h2 = recorder.record_write(
        artefact_path="src/foo.py",
        new_content=b"v2",
        agent_id=card.agent_id,
        agent_card=card,
        private_key_pem=priv,
        tool_call_id="tc-2",
        span_id="span-2",
    )
    assert h1 != h2
    entries = list(recorder.store.read_log())
    assert entries[0][0].parent_hashes == []
    assert entries[1][0].parent_hashes == [h1]


def test_writes_to_distinct_artefacts_are_independent(
    recorder: SignedLineageLog,
    card_and_keys: tuple[AgentCard, str],
) -> None:
    card, priv = card_and_keys
    recorder.record_write(
        artefact_path="src/foo.py",
        new_content=b"a",
        agent_id=card.agent_id,
        agent_card=card,
        private_key_pem=priv,
        tool_call_id="tc-a",
        span_id="span-a",
    )
    recorder.record_write(
        artefact_path="src/bar.py",
        new_content=b"b",
        agent_id=card.agent_id,
        agent_card=card,
        private_key_pem=priv,
        tool_call_id="tc-b",
        span_id="span-b",
    )
    foo, bar = recorder.store.read_log()
    assert foo[0].artefact_path == "src/foo.py"
    assert bar[0].artefact_path == "src/bar.py"
    assert foo[0].parent_hashes == []
    assert bar[0].parent_hashes == []


# ---------------------------------------------------------------------------
# Signature roundtrip
# ---------------------------------------------------------------------------


def test_recorded_jws_verifies_against_card(
    recorder: SignedLineageLog,
    card_and_keys: tuple[AgentCard, str],
) -> None:
    card, priv = card_and_keys
    recorder.record_write(
        artefact_path="src/foo.py",
        new_content=b"hello",
        agent_id=card.agent_id,
        agent_card=card,
        private_key_pem=priv,
        tool_call_id="tc-1",
        span_id="span-1",
    )
    entry, jws = next(iter(recorder.store.read_log()))
    # The signed-write path signs the JCS-canonical entry bytes; the verifier (gate)
    # recomputes the same bytes - see ADR-009 §5.2.
    assert verify_detached(canonicalise(entry), jws, card) is True


def test_signature_does_not_verify_against_wrong_card(
    recorder: SignedLineageLog,
    card_and_keys: tuple[AgentCard, str],
) -> None:
    card, priv = card_and_keys
    recorder.record_write(
        artefact_path="src/foo.py",
        new_content=b"x",
        agent_id=card.agent_id,
        agent_card=card,
        private_key_pem=priv,
        tool_call_id="tc-1",
        span_id="span-1",
    )
    entry, jws = next(iter(recorder.store.read_log()))
    _priv2, pub2 = generate_keypair()
    other_card = AgentCard(agent_id="agent:other", kid=card.kid, public_key_pem=pub2)
    assert verify_detached(canonicalise(entry), jws, other_card) is False


# ---------------------------------------------------------------------------
# HMAC envelope
# ---------------------------------------------------------------------------


def test_operator_hmac_matches_recomputed_envelope(
    recorder: SignedLineageLog,
    card_and_keys: tuple[AgentCard, str],
    hmac_key: bytes,
) -> None:
    """The on-disk ``operator_hmac`` must match HMAC-SHA256 over the entry's
    canonical bytes computed with an empty ``operator_hmac`` field - the
    standard 'envelope around the body sans HMAC' pattern.
    """
    card, priv = card_and_keys
    recorder.record_write(
        artefact_path="src/foo.py",
        new_content=b"x",
        agent_id=card.agent_id,
        agent_card=card,
        private_key_pem=priv,
        tool_call_id="tc-1",
        span_id="span-1",
    )
    entry, _ = next(iter(recorder.store.read_log()))

    # Reconstruct the pre-HMAC canonical body.
    body = json.loads(canonicalise(entry))
    body["operator_hmac"] = ""
    canonical_body = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    expected = _hmac.new(hmac_key, canonical_body, hashlib.sha256).hexdigest()
    assert entry.operator_hmac == expected


def test_operator_hmac_differs_under_tamper(
    recorder: SignedLineageLog,
    card_and_keys: tuple[AgentCard, str],
    hmac_key: bytes,
) -> None:
    card, priv = card_and_keys
    recorder.record_write(
        artefact_path="src/foo.py",
        new_content=b"x",
        agent_id=card.agent_id,
        agent_card=card,
        private_key_pem=priv,
        tool_call_id="tc-1",
        span_id="span-1",
    )
    entry, _ = next(iter(recorder.store.read_log()))

    body = json.loads(canonicalise(entry))
    body["operator_hmac"] = ""
    body["artefact_path"] = "src/EVIL.py"  # tampered
    canonical_body = json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    other = _hmac.new(hmac_key, canonical_body, hashlib.sha256).hexdigest()
    assert entry.operator_hmac != other


# ---------------------------------------------------------------------------
# Path traversal rejection
# ---------------------------------------------------------------------------


def test_rejects_path_traversal(
    recorder: SignedLineageLog,
    card_and_keys: tuple[AgentCard, str],
) -> None:
    card, priv = card_and_keys
    with pytest.raises(ValueError, match="path traversal"):
        recorder.record_write(
            artefact_path="../etc/passwd",
            new_content=b"x",
            agent_id=card.agent_id,
            agent_card=card,
            private_key_pem=priv,
            tool_call_id="tc-1",
            span_id="span-1",
        )


def test_rejects_absolute_path(
    recorder: SignedLineageLog,
    card_and_keys: tuple[AgentCard, str],
) -> None:
    card, priv = card_and_keys
    with pytest.raises(ValueError, match="absolute"):
        recorder.record_write(
            artefact_path="/etc/passwd",
            new_content=b"x",
            agent_id=card.agent_id,
            agent_card=card,
            private_key_pem=priv,
            tool_call_id="tc-1",
            span_id="span-1",
        )


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------


def entry_hash_payload(canonical_bytes: bytes) -> bytes:
    """Legacy helper retained for back-compat with older test imports.

    The signed-write path signs the JCS-canonical entry bytes directly so callers
    should pass ``canonicalise(entry)`` to ``verify_detached``; this helper
    is kept so external test modules that import it keep working.
    """
    return canonical_bytes
