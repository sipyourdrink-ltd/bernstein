"""Tests for RFC 7638 JWK thumbprint kid in Trust Records (issue #6087).

Covers:
1. `test_kid_is_the_rfc7638_thumbprint_of_the_signing_key` (load-bearing):
   Asserts cnf.jwk.kid equals the RFC 7638 thumbprint of the Ed25519 signing key.
2. `test_two_keys_never_share_a_kid`:
   Different signing keys yield distinct kid values.
3. `test_kid_is_identical_with_identity_emission_on_and_off`:
   Identity emission flag has zero impact on kid derivation.
4. `test_hop_records_and_aggregate_of_one_run_carry_the_same_kid`:
   Worker hop records and run-level aggregate share the same kid.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.core.observability.trust_record import TrustRecordEmitter
from bernstein.core.security.agent_card_signer import _b64url

_TEST_SEED_1 = b"kid-test-seed-111111111111111111"
_TEST_SEED_2 = b"kid-test-seed-222222222222222222"


def _keypair(seed: bytes) -> tuple[bytes, bytes]:
    key = Ed25519PrivateKey.from_private_bytes(seed)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    public_raw = key.public_key().public_bytes(
        serialization.Encoding.Raw,
        serialization.PublicFormat.Raw,
    )
    return private_pem, public_raw


from tests.unit.core.observability.test_trust_record import _create_journal


def _rfc7638_thumbprint(public_raw: bytes) -> str:
    x = _b64url(public_raw)
    canonical = json.dumps(
        {"crv": "Ed25519", "kty": "OKP", "x": x},
        sort_keys=True,
        separators=(",", ":"),
    ).encode("ascii")
    digest = hashlib.sha256(canonical).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def test_kid_is_the_rfc7638_thumbprint_of_the_signing_key(tmp_path: Path) -> None:
    """Load-bearing: cnf.jwk.kid must equal the RFC 7638 thumbprint of the signing key.

    On main, kid was f"install-{install_rev}", which defaults to
    'install-0000000000000000' on default installs.
    """
    private_pem, public_raw = _keypair(_TEST_SEED_1)
    emitter = TrustRecordEmitter(
        get_private_key_pem=lambda: private_pem,
        get_installed_digest=lambda: "sha256:" + "00" * 32,
    )
    journal_path = _create_journal(
        tmp_path,
        [
            {"type": "run_started", "gate_config": {"lint": True}},
            {"type": "agent_spawned", "model_id": "claude-haiku-5", "model_provider": "anthropic"},
            {"type": "run_completed", "ts": 1.0},
        ],
    )
    raw = emitter.emit_trust_record(journal_path, "run-1", "exec-1")
    parsed = json.loads(raw)
    actual_kid = parsed["cnf"]["jwk"]["kid"]
    expected_kid = _rfc7638_thumbprint(public_raw)

    assert actual_kid == expected_kid


def test_two_keys_never_share_a_kid(tmp_path: Path) -> None:
    """Two different signing keys must produce different kid values."""
    priv1, _ = _keypair(_TEST_SEED_1)
    priv2, _ = _keypair(_TEST_SEED_2)

    emitter1 = TrustRecordEmitter(
        get_private_key_pem=lambda: priv1,
        get_installed_digest=lambda: "sha256:" + "00" * 32,
    )
    emitter2 = TrustRecordEmitter(
        get_private_key_pem=lambda: priv2,
        get_installed_digest=lambda: "sha256:" + "00" * 32,
    )

    journal_path = _create_journal(
        tmp_path,
        [
            {"type": "run_started", "gate_config": {"lint": True}},
            {"type": "agent_spawned", "model_id": "claude-haiku-5", "model_provider": "anthropic"},
            {"type": "run_completed", "ts": 1.0},
        ],
    )

    rec1 = json.loads(emitter1.emit_trust_record(journal_path, "run-1", "exec-1"))
    rec2 = json.loads(emitter2.emit_trust_record(journal_path, "run-1", "exec-1"))

    assert rec1["cnf"]["jwk"]["kid"] != rec2["cnf"]["jwk"]["kid"]


def test_kid_is_identical_with_identity_emission_on_and_off(tmp_path: Path, monkeypatch) -> None:
    """The kid derivation is purely a function of the key, independent of identity emission."""
    import bernstein.core.identity.install_rev as irev

    priv, pub_raw = _keypair(_TEST_SEED_1)
    expected_kid = _rfc7638_thumbprint(pub_raw)

    journal_path = _create_journal(
        tmp_path,
        [
            {"type": "run_started", "gate_config": {"lint": True}},
            {"type": "agent_spawned", "model_id": "claude-haiku-5", "model_provider": "anthropic"},
            {"type": "run_completed", "ts": 1.0},
        ],
    )

    # 1. With identity emission disabled (default sentinel)
    monkeypatch.setattr(irev, "IDENTITY_EMISSION_ENABLED", False)
    monkeypatch.delenv("BERNSTEIN_DISABLE_IDENTITY", raising=False)
    emitter_off = TrustRecordEmitter(
        get_private_key_pem=lambda: priv,
        get_installed_digest=lambda: "sha256:" + "00" * 32,
    )
    rec_off = json.loads(emitter_off.emit_trust_record(journal_path, "run-1", "exec-1"))

    # 2. With identity emission enabled
    monkeypatch.setattr(irev, "IDENTITY_EMISSION_ENABLED", True)
    emitter_on = TrustRecordEmitter(
        get_private_key_pem=lambda: priv,
        get_installed_digest=lambda: "sha256:" + "00" * 32,
    )
    rec_on = json.loads(emitter_on.emit_trust_record(journal_path, "run-1", "exec-1"))

    assert rec_off["cnf"]["jwk"]["kid"] == expected_kid
    assert rec_on["cnf"]["jwk"]["kid"] == expected_kid


def test_hop_records_and_aggregate_of_one_run_carry_the_same_kid(tmp_path: Path) -> None:
    """Worker hop records and run-level aggregate share the same kid when signed by the same key."""
    priv, pub_raw = _keypair(_TEST_SEED_1)
    expected_kid = _rfc7638_thumbprint(pub_raw)

    emitter = TrustRecordEmitter(
        get_private_key_pem=lambda: priv,
        get_installed_digest=lambda: "sha256:" + "00" * 32,
    )

    journal_path = _create_journal(
        tmp_path,
        [
            {"type": "run_started", "gate_config": {"lint": True}},
            {
                "type": "agent_spawned",
                "agent_id": "worker-1",
                "model_id": "claude-haiku-5",
                "model_provider": "anthropic",
            },
            {"type": "tool_call", "agent_id": "worker-1", "tool": "test", "ts": 1.0},
            {
                "type": "agent_spawned",
                "agent_id": "worker-2",
                "model_id": "claude-sonnet-5",
                "model_provider": "anthropic",
            },
            {"type": "run_completed", "ts": 2.0},
        ],
    )

    hop_result = emitter.emit_hop_records(journal_path, "run-1")
    assert len(hop_result.records) == 2
    for hop in hop_result.records:
        parsed_hop = json.loads(hop.record)
        assert parsed_hop["cnf"]["jwk"]["kid"] == expected_kid

    parsed_aggregate = json.loads(hop_result.aggregate)
    assert parsed_aggregate["cnf"]["jwk"]["kid"] == expected_kid
