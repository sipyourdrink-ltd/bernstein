"""RFC 8785 key ordering, proved on a record we actually emit (issue #5551).

RFC 8785 sorts object property names as arrays of UTF-16 code units (3.2.3).
The obvious shortcut sorts by Unicode code point. The two agree for every name
below U+D800 and disagree the moment a supplementary-plane name meets one in
U+E000..U+FFFF, because the supplementary name starts with a high surrogate:

    key        code point   UTF-16 code units
    U+FFFF     65535        FFFF
    U+1D11E    119070       D834 DD1E

Code-point order puts U+FFFF first; UTF-16 order puts U+1D11E first. Opposite
results from the same two keys.

Every record this codebase emits has schema-fixed ASCII property names, so the
divergence never fires and the property was unfalsifiable from outside: a
verifier had evidence only that we had never been asked to sort correctly.

The fixture in tests/fixtures/utf16-ordering-vector closes that. It is signed
by the production path -- sign_agent_card over _card_to_dict -- through the one
open-membership map on the card, so it is a record we genuinely emit rather
than a vector written for its own test.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization

from bernstein.core.identity.agent_card import AgentIdentityCard
from bernstein.core.security.agent_card_signer import (
    AgentCardSignature,
    _b64url,
    _b64url_decode,
    _card_to_dict,
    canonicalize_jcs,
    verify_agent_card,
)

VECTOR_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "utf16-ordering-vector"

#: U+FFFF, one UTF-16 code unit.
BMP_KEY = "￿"
#: U+1D11E MUSICAL SYMBOL G CLEF, surrogate pair D834 DD1E.
SUPPLEMENTARY_KEY = "\U0001d11e"


def _load(name: str) -> str:
    return (VECTOR_DIR / name).read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def card_body() -> dict:
    return json.loads(_load("card.json"))


@pytest.fixture(scope="module")
def signature() -> AgentCardSignature:
    raw = json.loads(_load("signature.json"))
    return AgentCardSignature(detached_jws=raw["detached_jws"], kid=raw["kid"])


def _code_point_canonical(value: object) -> bytes:
    """What a canonicaliser that sorted by code point would produce.

    ``json.dumps(sort_keys=True)`` sorts Python strings, which compare by code
    point. Everything else about the encoding matches: minimal separators, no
    ASCII escaping. So the *only* difference from our output is the ordering
    rule, which is what makes it a fair stand-in for a shortcut implementation.
    """
    return json.dumps(
        value, sort_keys=True, ensure_ascii=False, separators=(",", ":")
    ).encode("utf-8")


def test_the_fixture_carries_a_supplementary_plane_key(card_body: dict) -> None:
    extensions = card_body["extensions"]
    assert SUPPLEMENTARY_KEY in extensions
    assert BMP_KEY in extensions
    assert ord(SUPPLEMENTARY_KEY) > 0xFFFF, "the key must be outside the BMP"


def test_the_two_orderings_actually_disagree_on_this_input(card_body: dict) -> None:
    """Without this the rest of the file could pass on an input that proves nothing.

    A vector where both rules agree would satisfy every ordering assertion
    below while demonstrating nothing at all, so the divergence itself is
    asserted first.
    """
    extensions = card_body["extensions"]
    ours = canonicalize_jcs(extensions).decode("utf-8")
    theirs = _code_point_canonical(extensions).decode("utf-8")
    assert ours != theirs

    assert ours.index(SUPPLEMENTARY_KEY) < ours.index(BMP_KEY)
    assert theirs.index(BMP_KEY) < theirs.index(SUPPLEMENTARY_KEY)


def test_emitted_bytes_follow_utf16_code_unit_order(card_body: dict) -> None:
    canonical = canonicalize_jcs(card_body).decode("utf-8")
    assert canonical.index(SUPPLEMENTARY_KEY) < canonical.index(BMP_KEY)


def test_a_code_point_canonicalizer_computes_different_signing_bytes(
    card_body: dict,
) -> None:
    """The failure a verifier would actually hit."""
    assert canonicalize_jcs(card_body["extensions"]) != _code_point_canonical(
        card_body["extensions"]
    )


def test_our_verifier_accepts_the_record(
    card_body: dict, signature: AgentCardSignature
) -> None:
    card = AgentIdentityCard(**card_body)
    public_key = (VECTOR_DIR / "public-key.pem").read_bytes()
    assert verify_agent_card(card, signature, public_key) is True


def test_a_code_point_ordering_would_be_rejected(
    card_body: dict, signature: AgentCardSignature
) -> None:
    """The other direction: wrong order, wrong signing input, no verification.

    The JWS is detached, so its payload segment is empty and the signing input
    is rebuilt at verify time as ``header_b64 + "." + b64url(canonical(body))``.
    A producer that sorted by code point would therefore compute a different
    second segment, and its signature would not verify against this key -- so
    the assertion is on the signing input the verifier actually reconstructs,
    not on bytes carried inside the token.
    """
    header_b64 = signature.detached_jws.split(".")[0]

    ours = f"{header_b64}.{_b64url(canonicalize_jcs(card_body))}".encode("ascii")
    theirs = f"{header_b64}.{_b64url(_code_point_canonical(card_body))}".encode("ascii")
    assert ours != theirs, "the two rules must disagree on this record"

    public_key_pem = (VECTOR_DIR / "public-key.pem").read_bytes()
    public_key = serialization.load_pem_public_key(public_key_pem)
    raw_signature = _b64url_decode(signature.detached_jws.split(".")[2])

    # Ours verifies.
    public_key.verify(raw_signature, ours)

    # A code-point canonicalizer's signing input does not.
    with pytest.raises(InvalidSignature):
        public_key.verify(raw_signature, theirs)


def test_the_card_round_trips_through_the_dataclass(card_body: dict) -> None:
    """The fixture is the real emitted shape, not a hand-edited JSON blob."""
    card = AgentIdentityCard(**card_body)
    assert _card_to_dict(card) == card_body


def test_the_vector_is_documented(card_body: dict) -> None:
    readme = _load("README.md")
    assert "UTF-16" in readme
    assert "code point" in readme
    assert "U+1D11E" in readme
    assert "U+FFFF" in readme
