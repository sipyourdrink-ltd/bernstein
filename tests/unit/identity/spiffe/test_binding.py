"""Card-to-SVID binding receipts anchored in the audit chain (issue #2363, AC 2).

The binding between the platform identity (the SVID) and the card identity is
itself a verifiable record: ``bind_svid_to_card`` appends a
``spiffe.svid_binding`` event into the HMAC chain, and ``verify_binding`` /
``verify_binding_against_event`` re-derive the SPIFFE id deterministically and
recompute the binding content hash so tampering is detectable offline from the
chain alone.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from bernstein.core.identity.spiffe import (
    SvidBinding,
    bind_svid_to_card,
    svid_reference_from_x509,
    verify_binding,
    verify_binding_against_event,
)
from bernstein.core.identity.spiffe.binding import BindingError
from bernstein.core.identity.spiffe.svid import X509Svid
from bernstein.core.security.agent_identity import AgentIdentityCard, issue_identity_card
from bernstein.core.security.audit_chain import (
    EVENT_SPIFFE_SVID_BINDING,
    AuditChainStore,
    record_spiffe_svid_binding,
)


def _chain(tmp_path: Path) -> AuditChainStore:
    return AuditChainStore(tmp_path / "audit", key=b"k" * 32)


def _svid(spiffe_id: str, cert_pem: bytes, key_pem: bytes) -> X509Svid:
    return X509Svid(
        spiffe_id=spiffe_id,
        cert_chain_pem=cert_pem,
        private_key_pem=key_pem,
        bundle_pem=cert_pem,
        expires_at=0.0,
    )


def _bind(tmp_path: Path, install_keypair, svid_leaf_factory, agent_id="backend-1"):
    _priv, pub = install_keypair
    from bernstein.core.identity.spiffe import derive_spiffe_id_from_key

    sid = derive_spiffe_id_from_key(trust_domain="ex.org", install_public_key_pem=pub, agent_id=agent_id)
    cert_pem, key_pem = svid_leaf_factory(sid)
    ref = svid_reference_from_x509(_svid(sid, cert_pem, key_pem))
    card = issue_identity_card(agent_id, "backend", "claude", "opus")
    chain = _chain(tmp_path)
    updated, binding, event = bind_svid_to_card(
        card=card,
        svid_reference=ref,
        install_public_key_pem=pub,
        trust_domain="ex.org",
        chain=chain,
    )
    return pub, updated, binding, event, chain, ref


def test_binding_records_chained_event(tmp_path, install_keypair, svid_leaf_factory) -> None:
    _pub, _updated, binding, event, chain, ref = _bind(tmp_path, install_keypair, svid_leaf_factory)
    assert event.event_type == EVENT_SPIFFE_SVID_BINDING
    assert isinstance(binding, SvidBinding)
    assert binding.spiffe_id == ref.spiffe_id
    assert "prev_chain_digest" in event.details
    ok, errors = chain.verify()
    assert ok, errors


def test_card_carries_svid_reference(tmp_path, install_keypair, svid_leaf_factory) -> None:
    _pub, updated, _binding, _event, _chain, ref = _bind(tmp_path, install_keypair, svid_leaf_factory)
    assert isinstance(updated, AgentIdentityCard)
    assert updated.svid_reference == ref.spiffe_id


def test_binding_verifies_from_key(tmp_path, install_keypair, svid_leaf_factory) -> None:
    pub, _updated, binding, _event, _chain, _ref = _bind(tmp_path, install_keypair, svid_leaf_factory)
    ok, reason = verify_binding(binding=binding, install_public_key_pem=pub, trust_domain="ex.org")
    assert ok, reason


def test_binding_fails_wrong_trust_domain(tmp_path, install_keypair, svid_leaf_factory) -> None:
    pub, _updated, binding, _event, _chain, _ref = _bind(tmp_path, install_keypair, svid_leaf_factory)
    ok, _reason = verify_binding(binding=binding, install_public_key_pem=pub, trust_domain="other.org")
    assert not ok


def test_binding_fails_wrong_install_key(tmp_path, install_keypair, svid_leaf_factory) -> None:
    _pub, _updated, binding, _event, _chain, _ref = _bind(tmp_path, install_keypair, svid_leaf_factory)
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    other_pub = (
        ed25519.Ed25519PrivateKey.generate()
        .public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo)
    )
    ok, _reason = verify_binding(binding=binding, install_public_key_pem=other_pub, trust_domain="ex.org")
    assert not ok


def test_event_detects_binding_tamper(tmp_path, install_keypair, svid_leaf_factory) -> None:
    _pub, _updated, binding, event, _chain, _ref = _bind(tmp_path, install_keypair, svid_leaf_factory)
    # Unmodified binding matches the chained event.
    ok, reason = verify_binding_against_event(binding, event)
    assert ok, reason
    # Tamper the spiffe id after the fact -> content hash diverges from the chain.
    tampered = dataclasses.replace(binding, spiffe_id="spiffe://ex.org/bernstein/deadbeefdeadbeef/evil")
    ok2, _reason2 = verify_binding_against_event(tampered, event)
    assert not ok2


def test_mismatched_reference_refused(tmp_path, install_keypair, svid_leaf_factory) -> None:
    """A SVID reference whose id disagrees with the derived id is refused."""
    _priv, pub = install_keypair
    cert_pem, key_pem = svid_leaf_factory("spiffe://ex.org/bernstein/deadbeefdeadbeef/mismatch")
    ref = svid_reference_from_x509(_svid("spiffe://ex.org/bernstein/deadbeefdeadbeef/mismatch", cert_pem, key_pem))
    card = issue_identity_card("backend-1", "backend", "claude", "opus")
    chain = _chain(tmp_path)
    with pytest.raises(BindingError):
        bind_svid_to_card(
            card=card,
            svid_reference=ref,
            install_public_key_pem=pub,
            trust_domain="ex.org",
            chain=chain,
        )


def test_binding_dict_round_trip(tmp_path, install_keypair, svid_leaf_factory) -> None:
    _pub, _updated, binding, _event, _chain, _ref = _bind(tmp_path, install_keypair, svid_leaf_factory)
    restored = SvidBinding.from_dict(binding.to_dict())
    assert restored == binding


def test_record_helper_only_hashes(tmp_path) -> None:
    """The audit helper records identifiers and hashes -- never key material."""
    chain = _chain(tmp_path)
    event = record_spiffe_svid_binding(
        chain=chain,
        agent_id="backend-1",
        spiffe_id="spiffe://ex.org/bernstein/deadbeefdeadbeef/backend-1",
        install_id="deadbeefdeadbeef",
        card_hash="0011223344556677",
        svid_sha256="sha256:" + "ab" * 32,
        binding_hash="sha256:" + "cd" * 32,
        trust_domain="ex.org",
    )
    assert event.details["spiffe_id"].startswith("spiffe://")
    assert "private_key" not in event.details
    assert "PRIVATE" not in str(event.details)
    ok, _ = chain.verify()
    assert ok
