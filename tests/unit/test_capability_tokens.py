"""Unit tests for attenuated delegation capability tokens (issue #2611).

Covers the crypto core: mint, subset attenuation, offline chain verification,
the three-way tamper test (JWS byte flip, widened caveat rewrite, issuer-pubkey
swap) with no registry, the ``max_depth`` monotonic caveat, the RFC 8693
``to_actor_claims`` projection, and the ``delegation_minted`` audit anchor.
"""

from __future__ import annotations

import time

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.core.security import capability_tokens as ct
from bernstein.core.security.agent_card_signer import (
    canonicalize_jcs,
    generate_ed25519_keypair,
    sign_detached_jws_over_canonical,
)

#: Fixed reference time so caveats minted across a chain share an identical
#: ``not_after`` (equal expiry is narrowing; a later re-computed one would widen).
_NOW: float = 1_800_000_000.0


def _kp() -> tuple[bytes, bytes]:
    """Fresh (private_pem, public_pem) Ed25519 keypair."""
    return generate_ed25519_keypair()


def _caveats(
    *,
    permissions: set[str] | None = None,
    remaining_depth: int = 3,
    not_after: float | None = None,
    task_ids: set[str] | None = None,
    path_prefixes: set[str] | None = None,
    max_uses: int | None = None,
) -> ct.Caveats:
    return ct.Caveats(
        permissions=frozenset(permissions or {"files:read", "files:write", "tasks:read"}),
        remaining_depth=remaining_depth,
        not_after=not_after if not_after is not None else _NOW + 3600,
        task_ids=frozenset(task_ids) if task_ids is not None else None,
        path_prefixes=frozenset(path_prefixes) if path_prefixes is not None else None,
        max_uses=max_uses,
    )


def _three_hop() -> tuple[ct.CapabilityChain, dict[str, bytes], list[bytes]]:
    """Build a valid principal -> orchestrator -> sub-agent -> leaf chain.

    Returns the chain, a map of private keys by role, and the trust anchors
    (the principal's public key).
    """
    p_priv, p_pub = _kp()
    o_priv, o_pub = _kp()
    s_priv, s_pub = _kp()
    l_priv, l_pub = _kp()

    root = ct.mint_root(
        issuer_identity_id="principal",
        issuer_private_key=p_priv,
        subject_identity_id="orchestrator",
        subject_pubkey=o_pub,
        caveats=_caveats(remaining_depth=3),
    )
    hop1 = ct.attenuate(
        root,
        issuer_private_key=o_priv,
        subject_identity_id="sub-agent",
        subject_pubkey=s_pub,
        caveats=_caveats(permissions={"files:read", "tasks:read"}, remaining_depth=2),
    )
    hop2 = ct.attenuate(
        hop1,
        issuer_private_key=s_priv,
        subject_identity_id="leaf",
        subject_pubkey=l_pub,
        caveats=_caveats(permissions={"files:read"}, remaining_depth=1),
    )
    chain = ct.CapabilityChain(tokens=(root, hop1, hop2))
    privs = {"principal": p_priv, "orchestrator": o_priv, "sub-agent": s_priv, "leaf": l_priv}
    return chain, privs, [p_pub]


# ---------------------------------------------------------------------------
# AC1 - mint produces a detached JWS over the JCS body
# ---------------------------------------------------------------------------


def test_mint_root_populates_signed_fields() -> None:
    p_priv, _p_pub = _kp()
    _o_priv, o_pub = _kp()
    tok = ct.mint_root(
        issuer_identity_id="principal",
        issuer_private_key=p_priv,
        subject_identity_id="orchestrator",
        subject_pubkey=o_pub,
        caveats=_caveats(),
    )
    assert tok.issuer_pubkey  # derived from private key
    assert tok.subject_identity_id == "orchestrator"
    assert tok.caveats.permissions
    assert tok.parent_token_hash == ct.GENESIS_PARENT
    assert tok.audit_head  # populated (genesis by default)
    # Detached JWS: header..signature with the token typ.
    assert tok.jws.count(".") == 2
    _header_b64, payload_b64, _sig = tok.jws.split(".")
    assert payload_b64 == ""  # detached
    assert tok.verify_signature()


def test_mint_root_jws_carries_token_typ() -> None:
    import base64
    import json

    p_priv, _ = _kp()
    _o_priv, o_pub = _kp()
    tok = ct.mint_root(
        issuer_identity_id="principal",
        issuer_private_key=p_priv,
        subject_identity_id="orchestrator",
        subject_pubkey=o_pub,
        caveats=_caveats(),
    )
    header_b64 = tok.jws.split(".")[0]
    pad = "=" * (-len(header_b64) % 4)
    header = json.loads(base64.urlsafe_b64decode(header_b64 + pad))
    assert header["typ"] == ct.TOKEN_TYP == "delegation-capability+jws"
    assert header["alg"] == "EdDSA"


# ---------------------------------------------------------------------------
# AC2 - attenuate enforces subset semantics
# ---------------------------------------------------------------------------


def test_attenuate_narrows_ok() -> None:
    chain, _privs, _anchors = _three_hop()
    assert len(chain.tokens) == 3


def test_attenuate_rejects_widened_permission() -> None:
    p_priv, _ = _kp()
    o_priv, o_pub = _kp()
    _s_priv, s_pub = _kp()
    root = ct.mint_root(
        issuer_identity_id="principal",
        issuer_private_key=p_priv,
        subject_identity_id="orchestrator",
        subject_pubkey=o_pub,
        caveats=_caveats(permissions={"files:read"}, remaining_depth=3),
    )
    with pytest.raises(ct.AttenuationError):
        ct.attenuate(
            root,
            issuer_private_key=o_priv,
            subject_identity_id="sub-agent",
            subject_pubkey=s_pub,
            caveats=_caveats(permissions={"files:read", "files:write"}, remaining_depth=2),
        )


def test_attenuate_rejects_wrong_signer_key() -> None:
    p_priv, _ = _kp()
    _o_priv, o_pub = _kp()
    wrong_priv, _ = _kp()
    _s_priv, s_pub = _kp()
    root = ct.mint_root(
        issuer_identity_id="principal",
        issuer_private_key=p_priv,
        subject_identity_id="orchestrator",
        subject_pubkey=o_pub,
        caveats=_caveats(remaining_depth=3),
    )
    # A key that is not the subject of the parent must not be able to attenuate.
    with pytest.raises(ct.AttenuationError):
        ct.attenuate(
            root,
            issuer_private_key=wrong_priv,
            subject_identity_id="sub-agent",
            subject_pubkey=s_pub,
            caveats=_caveats(remaining_depth=2),
        )


def test_attenuate_rejects_expiry_extension_and_max_uses_growth() -> None:
    p_priv, _ = _kp()
    o_priv, o_pub = _kp()
    _s_priv, s_pub = _kp()
    now = time.time()
    root = ct.mint_root(
        issuer_identity_id="principal",
        issuer_private_key=p_priv,
        subject_identity_id="orchestrator",
        subject_pubkey=o_pub,
        caveats=_caveats(remaining_depth=3, not_after=now + 100, max_uses=5),
    )
    with pytest.raises(ct.AttenuationError):
        ct.attenuate(
            root,
            issuer_private_key=o_priv,
            subject_identity_id="sub-agent",
            subject_pubkey=s_pub,
            caveats=_caveats(remaining_depth=2, not_after=now + 200, max_uses=5),
        )
    with pytest.raises(ct.AttenuationError):
        ct.attenuate(
            root,
            issuer_private_key=o_priv,
            subject_identity_id="sub-agent",
            subject_pubkey=s_pub,
            caveats=_caveats(remaining_depth=2, not_after=now + 100, max_uses=10),
        )


# ---------------------------------------------------------------------------
# max_depth caveat (design addition 1)
# ---------------------------------------------------------------------------


def test_attenuate_requires_strictly_decreasing_depth() -> None:
    p_priv, _ = _kp()
    o_priv, o_pub = _kp()
    _s_priv, s_pub = _kp()
    root = ct.mint_root(
        issuer_identity_id="principal",
        issuer_private_key=p_priv,
        subject_identity_id="orchestrator",
        subject_pubkey=o_pub,
        caveats=_caveats(remaining_depth=2),
    )
    # Equal depth is not narrowing.
    with pytest.raises(ct.AttenuationError):
        ct.attenuate(
            root,
            issuer_private_key=o_priv,
            subject_identity_id="sub-agent",
            subject_pubkey=s_pub,
            caveats=_caveats(remaining_depth=2),
        )


def test_chain_deeper_than_root_authorized_fails_mint() -> None:
    p_priv, _ = _kp()
    o_priv, o_pub = _kp()
    s_priv, s_pub = _kp()
    _l_priv, l_pub = _kp()
    root = ct.mint_root(
        issuer_identity_id="principal",
        issuer_private_key=p_priv,
        subject_identity_id="orchestrator",
        subject_pubkey=o_pub,
        caveats=_caveats(remaining_depth=1),
    )
    hop1 = ct.attenuate(
        root,
        issuer_private_key=o_priv,
        subject_identity_id="sub-agent",
        subject_pubkey=s_pub,
        caveats=_caveats(remaining_depth=0),
    )
    # remaining_depth is now 0; one more hop would need depth < 0 -> reject.
    with pytest.raises(ct.AttenuationError):
        ct.attenuate(
            hop1,
            issuer_private_key=s_priv,
            subject_identity_id="leaf",
            subject_pubkey=l_pub,
            caveats=_caveats(remaining_depth=-1),
        )


# ---------------------------------------------------------------------------
# AC3 - offline verify_chain returns the resolved authority path
# ---------------------------------------------------------------------------


def test_verify_chain_offline_happy_path() -> None:
    chain, _privs, anchors = _three_hop()
    result = ct.verify_chain(chain, trust_anchors=set(_pem_set(anchors)))
    assert result.valid
    assert result.principal_path == ["principal", "orchestrator", "sub-agent", "leaf"]
    assert all(hop.ok for hop in result.hops)


def test_verify_chain_rejects_untrusted_root() -> None:
    chain, _privs, _anchors = _three_hop()
    _other_priv, other_pub = _kp()
    result = ct.verify_chain(chain, trust_anchors=set(_pem_set([other_pub])))
    assert not result.valid
    assert not result.hops[0].ok


# ---------------------------------------------------------------------------
# AC4 - three-way tamper test with the registry unavailable
# ---------------------------------------------------------------------------


def test_tamper_jws_byte_flip_rejected() -> None:
    chain, _privs, anchors = _three_hop()
    tokens = list(chain.tokens)
    victim = tokens[1]
    # Flip one significant base64url char of the signature segment. The first
    # char encodes 6 full signature bits (the last char's low bits are padding),
    # so flipping index 0 is guaranteed to change the decoded signature bytes.
    header, _payload, sig = victim.jws.split(".")
    flipped_char = "A" if sig[0] != "A" else "B"
    bad_jws = f"{header}..{flipped_char}{sig[1:]}"
    tokens[1] = _replace_jws(victim, bad_jws)
    tampered = ct.CapabilityChain(tokens=tuple(tokens))
    result = ct.verify_chain(tampered, trust_anchors=set(_pem_set(anchors)))
    assert not result.valid
    assert not result.hops[1].ok


def test_tamper_widened_caveat_rejected_from_bytes_only() -> None:
    """A re-signed, structurally-continuous but widened hop is rejected on
    attenuation alone, with no registry and no audit log consulted."""
    chain, privs, anchors = _three_hop()
    tokens = list(chain.tokens)
    parent = tokens[0]
    victim = tokens[1]
    # Forge a hop signed by the correct issuer key (orchestrator) but whose
    # caveats widen the parent: add a permission the parent never held.
    widened = ct.Caveats(
        permissions=victim.caveats.permissions | {"agents:spawn"},
        remaining_depth=victim.caveats.remaining_depth,
        not_after=victim.caveats.not_after,
        task_ids=victim.caveats.task_ids,
        path_prefixes=victim.caveats.path_prefixes,
        max_uses=victim.caveats.max_uses,
    )
    forged = ct.sign_token(
        token_id=victim.token_id,
        issuer_identity_id=victim.issuer_identity_id,
        issuer_private_key=privs["orchestrator"],
        subject_identity_id=victim.subject_identity_id,
        subject_pubkey=victim.subject_pubkey.encode(),
        caveats=widened,
        parent_token_hash=parent.token_hash(),
        audit_head=victim.audit_head,
        granted_at=victim.granted_at,
    )
    assert forged.verify_signature()  # signature is valid
    tokens[1] = forged
    # Re-mint hop2 so its parent hash points to the forged hop1 (structure intact).
    tokens[2] = ct.attenuate(
        forged,
        issuer_private_key=privs["sub-agent"],
        subject_identity_id="leaf",
        subject_pubkey=serialization.load_pem_private_key(privs["leaf"], password=None)
        .public_key()
        .public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo),
        caveats=_caveats(permissions={"files:read"}, remaining_depth=0),
    )
    tampered = ct.CapabilityChain(tokens=tuple(tokens))
    # No audit_chain passed: purely from signed token bytes.
    result = ct.verify_chain(tampered, trust_anchors=set(_pem_set(anchors)))
    assert not result.valid
    assert not result.hops[1].ok
    assert any("widen" in e or "narrow" in e for e in result.hops[1].errors)


def test_tamper_issuer_pubkey_swap_rejected() -> None:
    chain, _privs, anchors = _three_hop()
    tokens = list(chain.tokens)
    victim = tokens[1]
    # Attacker swaps hop1.issuer_pubkey to their own identity and re-signs.
    atk_priv = Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    )
    forged = ct.sign_token(
        token_id=victim.token_id,
        issuer_identity_id=victim.issuer_identity_id,
        issuer_private_key=atk_priv,
        subject_identity_id=victim.subject_identity_id,
        subject_pubkey=victim.subject_pubkey.encode(),
        caveats=victim.caveats,
        parent_token_hash=tokens[0].token_hash(),
        audit_head=victim.audit_head,
        granted_at=victim.granted_at,
    )
    assert forged.verify_signature()  # self-consistent signature
    tokens[1] = forged
    tampered = ct.CapabilityChain(tokens=tuple(tokens))
    result = ct.verify_chain(tampered, trust_anchors=set(_pem_set(anchors)))
    assert not result.valid
    assert not result.hops[1].ok
    assert any("pubkey" in e or "continu" in e for e in result.hops[1].errors)


# ---------------------------------------------------------------------------
# to_actor_claims (design addition 2)
# ---------------------------------------------------------------------------


def test_to_actor_claims_nested_shape() -> None:
    chain, _privs, anchors = _three_hop()
    claims = ct.to_actor_claims(chain, trust_anchors=set(_pem_set(anchors)))
    assert claims == {
        "sub": "principal",
        "act": {
            "sub": "orchestrator",
            "act": {
                "sub": "sub-agent",
                "act": {"sub": "leaf"},
            },
        },
    }


def test_to_actor_claims_refuses_unverified_chain() -> None:
    chain, _privs, _anchors = _three_hop()
    _other_priv, other_pub = _kp()
    with pytest.raises(ct.TokenVerificationError):
        ct.to_actor_claims(chain, trust_anchors=set(_pem_set([other_pub])))


# ---------------------------------------------------------------------------
# Round-trip serialization
# ---------------------------------------------------------------------------


def test_chain_json_round_trip_verifies() -> None:
    chain, _privs, anchors = _three_hop()
    blob = chain.to_json()
    restored = ct.CapabilityChain.from_json(blob)
    result = ct.verify_chain(restored, trust_anchors=set(_pem_set(anchors)))
    assert result.valid


def test_token_hash_is_jcs_stable() -> None:
    chain, _privs, _anchors = _three_hop()
    tok = chain.tokens[0]
    again = ct.CapabilityToken.from_dict(tok.to_dict())
    assert again.token_hash() == tok.token_hash()
    # Hash equals sha256 of the JCS body.
    import hashlib

    assert tok.token_hash() == hashlib.sha256(canonicalize_jcs(tok.body())).hexdigest()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pem_set(pubs: list[bytes]) -> list[str]:
    return [p.decode() for p in pubs]


def _replace_jws(token: ct.CapabilityToken, jws: str) -> ct.CapabilityToken:
    import dataclasses

    return dataclasses.replace(token, jws=jws)


def test_sign_detached_helper_matches_verify() -> None:
    """The new public sign helper round-trips through the existing verifier."""
    priv, pub = _kp()
    body = canonicalize_jcs({"z": 1, "a": [2, 3]})
    jws = sign_detached_jws_over_canonical(body, priv, typ="x+jws", kid="k1")
    from bernstein.core.security.agent_card_signer import (
        verify_detached_jws_over_canonical,
    )

    assert verify_detached_jws_over_canonical(body, jws, pub, expected_typ="x+jws")
    assert not verify_detached_jws_over_canonical(body, jws, pub, expected_typ="other+jws")
