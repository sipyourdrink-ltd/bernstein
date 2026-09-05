"""Bughunt for the A2A v1.0 signed agent card surface.

Each test pins a specific JWS / JCS failure mode in the verifier
surface. Findings:

#1 (FIXED):
    ``verify_agent_card`` raised ``AttributeError`` when a JWS protected
    header decoded to a non-object JSON value (``[]``, ``null``, ``42``,
    ``"str"``). Network-controlled input → 500 / unhandled exception. Now
    returns ``False`` defensively (``isinstance(header, dict)`` guard).

#2 (FIXED - RFC 8785 §3.2.2.3 number serialisation):
    JCS-canonicalised numbers used to differ from the spec for
    integer-valued floats (``10.0`` → ``"10.0"``, should be ``"10"``), for
    scientific notation on either side of the ES6 thresholds (``1e-7`` →
    ``"1e-07"``, should be ``"1e-7"``), and for negative zero. Cards carry
    ``max_budget_usd``, ``created_at`` and ``expires_at`` as floats, so a
    strictly RFC-8785-compliant verifier computed different bytes than the
    signer whenever one of those landed on an integer boundary.
    ``canonicalize_jcs`` now implements the ECMAScript ``Number::toString``
    rule the spec cites, and the official reference vector
    ``structures.json`` passes (``56.0`` → ``56``).

#3 (FIXED in #3105 - RFC 8785 §3.2.3 key-sort order):
    Object keys used to be sorted by Unicode code point (Python
    ``sort_keys``) rather than by UTF-16 code units. For BMP-only keys the
    two agree; once a key crosses U+FFFF (surrogate pair) the bytes diverged
    from spec, because ``😂`` (U+1F602, UTF-16 high surrogate 0xD83D) sorts
    before ``שּ`` (U+FB33) under UTF-16 and after it under code-point order.
    ``canonicalize_jcs`` now sorts property names by UTF-16 code units, so
    ``test_rfc_8785_utf16_keysort`` below is a positive assertion.

#4 (operational, xfail - verifier accepts expired cards):
    ``verify_agent_card`` does not consult ``card.is_expired()`` -
    integrators must remember to call it themselves. Replay-by-stale-card
    is not addressed at the cryptographic verifier layer.

#5 (operational, documented - ephemeral per-process keypair):
    Each orchestrator process mints a fresh keypair on first JWKS hit. A
    federated verifier polling JWKS from a different replica than the one
    that signed will fail verification. Tracked in well_known.py docstring
    - persistence is deferred.

#6 (FIXED - JWKS rotation grace window):
    The orchestrator used to publish exactly one key, so a rotation broke
    every in-flight verifier holding the previous one. ``agent_json_keys``
    now appends every archived public key still inside the keystore's
    grace window (24h by default) after the current one, which is what
    RFC 7517 expects of a rotation window.

#7 (FIXED - private signing key file mode):
    Persistence landed as :class:`AgentCardKeystore`, and it enforces
    ``0600`` in three places: the private PEM is created with ``O_EXCL``
    and mode ``0600``, chmodded again after write, and a key already on
    disk with looser permissions is *refused* rather than loaded. The
    placeholder here asserted a path the implementation never used
    (``.sdd/security/keys/agent_signing/``, against a keystore rooted at
    ``.bernstein/keys``), so it reported the control missing on a tree
    that had it.

#8 (FIXED - RFC 8707 resource indicators):
    ``auth_middleware`` consults the JWT ``resource`` claim through
    ``_resource_indicator_check`` and answers a mismatch with the RFC 6750
    challenge ``Bearer error="invalid_token",
    error_description="resource indicator mismatch"``. Enforcement is
    keyed on a configured ``expected_resource`` and is skipped when that is
    unset, so a deployment that has not opted in is unaffected. The
    middleware-level coverage lives in
    ``tests/unit/test_auth_middleware_resource_indicator.py``.

#9 (FIXED - ``typ`` cross-context replay):
    Confirmed via :class:`TestTypReplayContext`. The verifier rejects any
    JWS whose protected header carries a different ``typ`` value, even if
    signed by the same Ed25519 key.

#10 (operational, xfail - rotation archive is not bounded): rotation has
    landed, and every rotation moves the previous keypair under
    ``archive/<isoformat>/``. ``list_archived`` filters that directory by
    the grace window when publishing the JWKS, but nothing removes an entry
    once it falls outside: its docstring says old archives "may be GC'd by
    the operator out-of-band". Repeated rotation therefore grows a
    directory of retired *private* keys without bound.

The ``typ: agent-card+jws`` cross-context replay invariant is verified
positively in :class:`TestTypReplayContext`.

RFC 8785 reference vector status (cyberphone/json-canonicalization):
    arrays.json    PASS
    french.json    PASS
    values.json    PASS
    structures.json PASS (since #2 was fixed)
    weird.json     FAIL (U+007F escaping in a string value; its key order,
                   the #3 surrogate-pair case, is correct since #3105, and
                   its numbers are correct since #2)
"""

from __future__ import annotations

import base64
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest
from cryptography.hazmat.primitives import serialization
from hypothesis import given, settings
from hypothesis import strategies as st

from bernstein.core.identity.agent_card import (
    AgentIdentityCard,
    issue_identity_card,
)
from bernstein.core.security.agent_card_signer import (
    AgentCardSignature,
    canonicalize_jcs,
    generate_ed25519_keypair,
    sign_agent_card,
    verify_agent_card,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _jwk_x(public_pem: bytes) -> str:
    """Return the base64url ``x`` an Ed25519 public PEM produces in a JWK."""
    raw = serialization.load_pem_public_key(public_pem).public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _jwks_request() -> Any:
    """Return the minimal request object ``agent_json_keys`` reads a tenant from."""

    class _Request:
        headers: dict[str, str] = {}
        query_params: dict[str, str] = {}

    return _Request()


def _stable_card(agent_id: str = "claude-test-id", *, max_budget: float = 5.0) -> AgentIdentityCard:
    """A reproducible card body for property tests."""
    return issue_identity_card(
        agent_id=agent_id,
        role="security",
        adapter="claude-cli",
        model="claude-opus-4-7",
        scope=["src/", "tests/"],
        max_budget_usd=max_budget,
        ttl_seconds=3600,
    )


def _b64url(b: bytes) -> str:
    return base64.urlsafe_b64encode(b).rstrip(b"=").decode("ascii")


# ---------------------------------------------------------------------------
# Property tests over the sign / verify roundtrip
# ---------------------------------------------------------------------------


@settings(max_examples=50, deadline=None)
@given(
    agent_id=st.text(
        alphabet=st.characters(min_codepoint=0x20, max_codepoint=0x7E, blacklist_characters='\\"'),
        min_size=1,
        max_size=24,
    ),
    budget=st.floats(min_value=0.01, max_value=1000.0, allow_nan=False, allow_infinity=False),
)
def test_property_roundtrip_succeeds(agent_id: str, budget: float) -> None:
    """For any valid card, a freshly minted signature must verify."""
    priv, pub = generate_ed25519_keypair()
    card = _stable_card(agent_id, max_budget=budget)
    sig = sign_agent_card(card, priv)
    assert verify_agent_card(card, sig, pub) is True


@settings(max_examples=30, deadline=None)
@given(
    flip_index=st.integers(min_value=0, max_value=255),
)
def test_property_byte_flip_in_signature_breaks_verify(flip_index: int) -> None:
    """One-bit flip anywhere in the signature segment must fail verification."""
    priv, pub = generate_ed25519_keypair()
    card = _stable_card()
    sig = sign_agent_card(card, priv)

    header_b64, _empty, sig_b64 = sig.detached_jws.split(".")
    raw = bytearray(base64.urlsafe_b64decode(sig_b64 + "=" * (-len(sig_b64) % 4)))
    if not raw:
        return
    idx = flip_index % len(raw)
    raw[idx] ^= 0x01
    bad_b64 = base64.urlsafe_b64encode(bytes(raw)).rstrip(b"=").decode("ascii")
    forged = AgentCardSignature(detached_jws=f"{header_b64}..{bad_b64}", kid=sig.kid)
    assert verify_agent_card(card, forged, pub) is False


def test_field_reordering_produces_identical_canonical_bytes() -> None:
    """JCS object-key sort means ``{a:1,b:2}`` and ``{b:2,a:1}`` canonicalise
    bit-identically - the signing input must not depend on insertion order.
    """
    a = canonicalize_jcs({"alpha": 1, "beta": 2, "gamma": 3})
    b = canonicalize_jcs({"gamma": 3, "alpha": 1, "beta": 2})
    c = canonicalize_jcs({"beta": 2, "gamma": 3, "alpha": 1})
    assert a == b == c


# ---------------------------------------------------------------------------
# #1: malformed-header crash (FIXED - defensive in verifier)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw_header_json",
    [
        b"[]",
        b"null",
        b"42",
        b'"agent-card+jws"',
        b"true",
    ],
)
def test_non_object_jws_header_returns_false_not_crash(raw_header_json: bytes) -> None:
    """JWS header must be a JSON object per RFC 7515 §4.

    A network attacker controls the header bytes. Before the fix the
    verifier called ``.get(...)`` on whatever ``json.loads`` returned and
    crashed for arrays / null / scalars. Now it must reject cleanly with
    ``False`` so an unhandled 500 cannot leak from the
    ``/.well-known/agent.json`` verifier path or any internal call.
    """
    _priv, pub = generate_ed25519_keypair()
    card = _stable_card()
    bad_header_b64 = _b64url(raw_header_json)
    sig = AgentCardSignature(detached_jws=f"{bad_header_b64}..AA", kid="k")
    assert verify_agent_card(card, sig, pub) is False


def test_jws_with_extra_segments_returns_false() -> None:
    """A JWS with >3 dot-separated segments must be rejected without crashing."""
    _priv, pub = generate_ed25519_keypair()
    card = _stable_card()
    bad = AgentCardSignature(detached_jws="a.b.c.d", kid="k")
    assert verify_agent_card(card, bad, pub) is False


def test_jws_with_invalid_base64_signature_returns_false() -> None:
    """A signature segment containing non-base64url chars must be False."""
    _priv, pub = generate_ed25519_keypair()
    card = _stable_card()
    header_b64 = _b64url(b'{"alg":"EdDSA","typ":"agent-card+jws"}')
    bad = AgentCardSignature(detached_jws=f"{header_b64}..!!!notb64!!!", kid="k")
    assert verify_agent_card(card, bad, pub) is False


def test_jws_with_invalid_base64_header_returns_false() -> None:
    """A header segment containing non-base64url chars must be False."""
    _priv, pub = generate_ed25519_keypair()
    card = _stable_card()
    bad = AgentCardSignature(detached_jws="!!!.. !! ", kid="k")
    assert verify_agent_card(card, bad, pub) is False


# ---------------------------------------------------------------------------
# #2: RFC 8785 §3.2.2.3 - integer-valued floats and small scientific
# ---------------------------------------------------------------------------


def test_rfc_8785_integer_valued_floats_lose_decimal() -> None:
    """An integer-valued float serialises as an integer (FIXED).

    This was an ``xfail(strict=True)``: Python's ``repr`` keeps the
    trailing ``.0`` that RFC 8785 §3.2.2.3 drops, so a strict verifier
    (a Java JOSE library, say) computed different bytes than the signer
    for any card whose ``max_budget_usd``, ``created_at`` or
    ``expires_at`` landed on an integer boundary.
    """
    assert canonicalize_jcs(10.0) == b"10"
    assert canonicalize_jcs(0.0) == b"0"
    assert canonicalize_jcs(1.0) == b"1"


def test_rfc_8785_small_scientific_exponent_format() -> None:
    """The exponent carries no padding zero (FIXED): ``1e-7``, not ``1e-07``."""
    assert canonicalize_jcs(1e-7) == b"1e-7"


def test_rfc_8785_negative_zero_normalised() -> None:
    """Negative zero normalises to ``0`` (FIXED); Python's ``repr`` kept the sign."""
    assert canonicalize_jcs(-0.0) == b"0"


# ---------------------------------------------------------------------------
# #3: RFC 8785 §3.2.3 - UTF-16 code-unit key sort (FIXED in #3105)
# ---------------------------------------------------------------------------


def test_rfc_8785_utf16_keysort() -> None:
    """Property names sort by UTF-16 code unit, not by code point.

    This was an ``xfail(strict=True)`` recording a known deviation: a
    supplementary-plane name starts with a high surrogate in
    U+D800..U+DBFF, which sorts below U+E000..U+FFFF in UTF-16 and above
    it by code point. ``AgentIdentityCard.extensions`` is a free-form map
    that reaches the signed body, so a card could carry such a name and a
    conformant third-party verifier would compute different bytes.
    """
    bmp = ""  # codepoint 0xE000 (BMP private use)
    smp = "\U0001f600"  # codepoint 0x1F600, UTF-16 surrogate pair starting 0xD83D
    # UTF-16 code-unit order: smp (0xD83D) < bmp (0xE000) → smp key first.
    got = canonicalize_jcs({bmp: 1, smp: 2}).decode("utf-8")
    expected = '{"' + smp + '":2,"' + bmp + '":1}'
    assert got == expected


# ---------------------------------------------------------------------------
# #4: verifier doesn't enforce expiry - replay-by-stale-card
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    reason=(
        "verify_agent_card is a pure crypto primitive - it does not consult "
        "AgentIdentityCard.is_expired(). Integrators MUST also call "
        "card.is_expired() after verifying the signature, otherwise an "
        "attacker who captured an old card can replay it indefinitely. "
        "Documenting via xfail until either the verifier composes the expiry "
        "check or the docstring grows an explicit warning."
    ),
    strict=True,
)
def test_expired_card_signature_should_be_rejected() -> None:
    """If a card is expired, verify_agent_card should return False."""
    import time

    priv, pub = generate_ed25519_keypair()
    card = _stable_card()
    card.expires_at = time.time() - 7200  # two hours ago
    sig = sign_agent_card(card, priv)
    assert card.is_expired() is True
    assert verify_agent_card(card, sig, pub) is False


# ---------------------------------------------------------------------------
# ``typ: agent-card+jws`` blocks cross-context replay
# ---------------------------------------------------------------------------


class TestTypReplayContext:
    """Pins the invariant that ``typ: agent-card+jws`` prevents a signature
    minted for one JWS context from verifying as an agent card.
    """

    def test_typ_jwt_rejected(self) -> None:
        """A JWS with ``typ: jwt`` over the same body must NOT verify."""
        priv_pem, pub_pem = generate_ed25519_keypair()
        priv = serialization.load_pem_private_key(priv_pem, password=None)
        card = _stable_card()
        body_b64 = _b64url(canonicalize_jcs(asdict(card)))
        # Cross-context header - same alg + same key, different typ.
        hdr_b64 = _b64url(canonicalize_jcs({"alg": "EdDSA", "typ": "jwt"}))
        signing_input = f"{hdr_b64}.{body_b64}".encode("ascii")
        sig_bytes = priv.sign(signing_input)
        forged = AgentCardSignature(
            detached_jws=f"{hdr_b64}..{_b64url(sig_bytes)}",
            kid="k",
        )
        assert verify_agent_card(card, forged, pub_pem) is False

    def test_typ_with_trailing_whitespace_rejected(self) -> None:
        """Defends against header-normalisation games - exact match required."""
        priv_pem, pub_pem = generate_ed25519_keypair()
        priv = serialization.load_pem_private_key(priv_pem, password=None)
        card = _stable_card()
        body_b64 = _b64url(canonicalize_jcs(asdict(card)))
        hdr_b64 = _b64url(canonicalize_jcs({"alg": "EdDSA", "typ": "agent-card+jws "}))
        signing_input = f"{hdr_b64}.{body_b64}".encode("ascii")
        sig_bytes = priv.sign(signing_input)
        forged = AgentCardSignature(
            detached_jws=f"{hdr_b64}..{_b64url(sig_bytes)}",
            kid="k",
        )
        assert verify_agent_card(card, forged, pub_pem) is False


# ---------------------------------------------------------------------------
# JWKS endpoint: cold-start key generation + thread-safety smoke
# ---------------------------------------------------------------------------


def test_jwks_cold_start_generates_keypair() -> None:
    """First JWKS request after reset must mint a keypair, not 500."""
    from bernstein.core.routes.well_known import (
        _agent_card_payload,
        _reset_signing_keypair_for_tests,
    )

    _reset_signing_keypair_for_tests()
    payload = _agent_card_payload()
    assert "signatures" in payload
    sig = payload["signatures"][0]
    parts = sig["jws"].split(".")
    assert len(parts) == 3
    assert parts[1] == ""  # detached payload


def test_jwks_threaded_first_call_does_not_race() -> None:
    """Two concurrent first-callers should observe the same keypair."""
    import threading

    from bernstein.core.routes.well_known import (
        _get_signing_keypair,
        _reset_signing_keypair_for_tests,
    )

    _reset_signing_keypair_for_tests()
    results: list[tuple[bytes, bytes]] = []
    barrier = threading.Barrier(4)

    def worker() -> None:
        barrier.wait()
        results.append(_get_signing_keypair())

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len({r[0] for r in results}) == 1, "Race produced more than one private key"
    assert len({r[1] for r in results}) == 1, "Race produced more than one public key"


# ---------------------------------------------------------------------------
# JCS edge cases that we DO accept correctly today (regression guards)
# ---------------------------------------------------------------------------


def test_jcs_control_chars_use_lowercase_hex_short_form_where_defined() -> None:
    """RFC 8785 §3.2.2.2 short-form escapes for known control characters."""
    assert canonicalize_jcs({"k": "\x08"}) == b'{"k":"\\b"}'
    assert canonicalize_jcs({"k": "\x09"}) == b'{"k":"\\t"}'
    assert canonicalize_jcs({"k": "\x0a"}) == b'{"k":"\\n"}'
    assert canonicalize_jcs({"k": "\x0c"}) == b'{"k":"\\f"}'
    assert canonicalize_jcs({"k": "\x0d"}) == b'{"k":"\\r"}'
    # Unknown control chars use lowercase hex \uXXXX
    assert canonicalize_jcs({"k": "\x1f"}) == b'{"k":"\\u001f"}'
    assert canonicalize_jcs({"k": "\x00"}) == b'{"k":"\\u0000"}'


def test_jcs_does_not_escape_forward_slash() -> None:
    """RFC 8785 §3.2.2.2 - / is not on the escape list."""
    assert canonicalize_jcs("https://example.com/x") == b'"https://example.com/x"'


def test_jcs_unicode_emitted_as_utf8_bytes_no_normalisation() -> None:
    """RFC 8785 forbids NFC/NFD normalisation."""
    assert canonicalize_jcs({"k": "é"}) == '{"k":"é"}'.encode()
    assert canonicalize_jcs({"k": "中"}) == '{"k":"中"}'.encode()


def test_jcs_nan_and_infinity_rejected() -> None:
    """RFC 8785 §3.2.2.3: NaN and ±Infinity not allowed in JSON."""
    with pytest.raises(ValueError):
        canonicalize_jcs(float("nan"))
    with pytest.raises(ValueError):
        canonicalize_jcs(float("inf"))


# ---------------------------------------------------------------------------
# Helper: keep dataclass-asdict round-trip stable
# ---------------------------------------------------------------------------


def test_dataclass_asdict_roundtrip_is_stable_under_permutation() -> None:
    """``_card_to_dict`` (asdict) must be deterministic for the same card."""
    card_a = _stable_card("X")
    card_b = _stable_card("X")
    card_b.created_at = card_a.created_at
    card_b.expires_at = card_a.expires_at
    assert canonicalize_jcs(asdict(card_a)) == canonicalize_jcs(asdict(card_b))


# ---------------------------------------------------------------------------
# RFC 8785 reference test vectors (cyberphone/json-canonicalization)
# ---------------------------------------------------------------------------
# Source: https://github.com/cyberphone/json-canonicalization/tree/master/testdata
# These are the canonical interop checks every JCS implementation is expected
# to pass. The module docstring lists which pass and which xfail with the
# underlying root cause.


def test_rfc_8785_vector_arrays() -> None:
    """RFC 8785 reference vector ``arrays.json`` - numeric and string keys
    sort lexicographically; nested empty arrays preserved.
    """
    inp: Any = [56, {"d": True, "10": None, "1": []}]
    expected = b'[56,{"1":[],"10":null,"d":true}]'
    assert canonicalize_jcs(inp) == expected


def test_rfc_8785_vector_french() -> None:
    """RFC 8785 reference vector ``french.json`` - locale-independent sort
    over Latin-with-diacritics keys; UTF-8 byte output, no escaping.
    """
    inp = {
        "peach": "This sorting order",
        "péché": "is wrong according to French",
        "pêche": "but canonicalization MUST",
        "sin": "ignore locale",
    }
    expected = (
        b'{"peach":"This sorting order",'
        b'"p\xc3\xa9ch\xc3\xa9":"is wrong according to French",'
        b'"p\xc3\xaache":"but canonicalization MUST",'
        b'"sin":"ignore locale"}'
    )
    assert canonicalize_jcs(inp) == expected


def test_rfc_8785_vector_values_numbers() -> None:
    """RFC 8785 reference vector ``values.json`` - numbers known to
    round-trip via Python ``json.dumps`` (the easy slice of the vector).
    """
    assert canonicalize_jcs(333333333.33333329) == b"333333333.3333333"
    assert canonicalize_jcs(1e30) == b"1e+30"
    assert canonicalize_jcs(4.50) == b"4.5"
    assert canonicalize_jcs(2e-3) == b"0.002"
    assert canonicalize_jcs(1e-27) == b"1e-27"


def test_rfc_8785_vector_structures() -> None:
    """RFC 8785 reference vector ``structures.json`` (PASSES since #2).

    The vector's ``56.0`` is the integer-valued-float case, so this is the
    reference-vector form of the #2 assertion rather than a number we
    picked ourselves.
    """
    inp = {
        "1": {"f": {"f": "hi", "F": 5}, "\n": 56.0},
        "10": {},
        "": "empty",
        "a": {},
        "111": [{"e": "yes", "E": "no"}],
        "A": {},
    }
    expected = b'{"":"empty","1":{"\\n":56,"f":{"F":5,"f":"hi"}},"10":{},"111":[{"E":"no","e":"yes"}],"A":{},"a":{}}'
    assert canonicalize_jcs(inp) == expected


@pytest.mark.xfail(
    reason=(
        "The key-sort half of this vector passes since #3105: 😂 (U+1F602, "
        "UTF-16 high surrogate 0xD83D) now sorts before שּ (U+FB33), which is "
        "the RFC 8785 §3.2.3 order. The remaining divergence is unrelated to "
        "key order - it is the escaping of U+007F inside a string VALUE, "
        "which belongs to §3.2.2.2 string serialization and is tracked "
        "separately. Do not fold it into a key-ordering change: it moves "
        "signed bytes for a different class of payload."
    ),
    strict=True,
)
def test_rfc_8785_vector_weird() -> None:
    inp = {
        "€": "Euro Sign",
        "\r": "Carriage Return",
        "\n": "Newline",
        "1": "One",
        "": "Control",
        "😂": "Smiley",
        "ö": "Latin Small Letter O With Diaeresis",
        "שּ": "Hebrew Letter Dalet With Dagesh",
        "</script>": "Browser Challenge",
    }
    expected = (
        b'{"\\n":"Newline","\\r":"Carriage Return","1":"One",'
        b'"</script>":"Browser Challenge",'
        b'"\xc2\x80":"Control\\u007f",'
        b'"\xc3\xb6":"Latin Small Letter O With Diaeresis",'
        b'"\xe2\x82\xac":"Euro Sign",'
        b'"\xf0\x9f\x98\x82":"Smiley",'
        b'"\xef\xac\xb3":"Hebrew Letter Dalet With Dagesh"}'
    )
    assert canonicalize_jcs(inp) == expected


# ---------------------------------------------------------------------------
# Numeric equivalence - the JCS-classic gotcha
# ---------------------------------------------------------------------------
# The classic JCS gotcha: per RFC 8785 §3.2.2.3, ``1``, ``1.0``, ``1e0``,
# ``100e-2`` MUST canonicalise identically. We get this right for the int
# value ``1`` but Python emits ``1.0`` for every float-typed equivalent -
# meaning a card whose ``max_budget_usd`` lands on an integer boundary
# signs to different bytes than a Java verifier following the spec.


@pytest.mark.parametrize(
    "value",
    [1.0, 1e0, 100e-2, 100e-2],
)
def test_rfc_8785_numeric_equivalence_floats_canonicalise_to_int(value: float) -> None:
    """Every spelling of one canonicalises to ``1`` (FIXED)."""
    assert canonicalize_jcs(value) == b"1"


def test_rfc_8785_numeric_equivalence_int_one_works() -> None:
    """The int ``1`` canonicalises correctly - only the float path is broken."""
    assert canonicalize_jcs(1) == b"1"


# ---------------------------------------------------------------------------
# Unicode in JCS - no NFC normalisation
# ---------------------------------------------------------------------------


def test_jcs_no_nfc_normalisation_on_string_value() -> None:
    """RFC 8785 forbids any Unicode normalisation.

    Composed (``é`` U+00E9) and decomposed (``e`` + ``́``) MUST hash
    differently. If the canonicaliser silently NFC-normalised both, two
    semantically-identical cards would sign to different bytes - a very
    subtle source of verification failures across platforms with different
    default normalisation.
    """
    composed = "é"  # é precomposed
    decomposed = "é"  # e + COMBINING ACUTE ACCENT
    assert canonicalize_jcs(composed) != canonicalize_jcs(decomposed)
    assert canonicalize_jcs(composed) == b'"\xc3\xa9"'
    assert canonicalize_jcs(decomposed) == b'"e\xcc\x81"'


def test_jcs_emoji_emitted_as_utf8_4_byte_sequence() -> None:
    """Emoji (SMP) should serialise as raw UTF-8 bytes - NOT \\uXXXX escape."""
    assert canonicalize_jcs("\U0001f916") == b'"\xf0\x9f\xa4\x96"'


# ---------------------------------------------------------------------------
# JWKS rotation grace + cold-start + persistence (operational xfails)
# ---------------------------------------------------------------------------


def test_jwks_rotation_grace_window_keeps_old_key_verifying(tmp_path: Path) -> None:
    """A rotation keeps the retired key in the JWKS for the grace window (FIXED).

    This was an xfail claiming the orchestrator publishes exactly one key,
    so a rotation 401s every in-flight verifier holding the previous one.
    ``agent_json_keys`` now appends every archived key still inside the
    keystore's grace window, which is what RFC 7517 expects.

    The old test simulated rotation by resetting the in-process cache
    twice. That predates persistence: resetting the cache now reloads the
    same key from disk, so it was asserting against a rotation that never
    happened.
    """
    from bernstein.core.routes.well_known import (
        _get_keystore,
        _get_signing_keypair,
        _reset_signing_keypair_for_tests,
        agent_json_keys,
    )

    _reset_signing_keypair_for_tests(tmp_path / "keys")
    try:
        _old_priv, old_pub = _get_signing_keypair()
        _get_keystore().rotate()
        _reset_signing_keypair_for_tests(tmp_path / "keys")
        _new_priv, new_pub = _get_signing_keypair()
        assert new_pub != old_pub, "rotate() did not actually rotate"

        jwks = agent_json_keys(_jwks_request())
        advertised = {jwk["x"] for jwk in jwks["keys"]}
        assert _jwk_x(new_pub) in advertised, "the current key is not advertised"
        assert _jwk_x(old_pub) in advertised, "retired key dropped from JWKS at rotation - verifiers cached on it 401"
    finally:
        _reset_signing_keypair_for_tests()


def test_jwks_drops_a_key_once_the_grace_window_closes(tmp_path: Path) -> None:
    """The window is a window: a key past it stops being advertised."""
    from bernstein.core.security.agent_card_keystore import AgentCardKeystore

    keystore = AgentCardKeystore(tmp_path / "keys", grace_seconds=0)
    _priv, old_pub = keystore.load_or_generate()
    keystore.rotate()
    assert _jwk_x(old_pub) not in {_jwk_x(archived.public_pem) for archived in keystore.list_archived()}


def test_jwks_cold_start_under_concurrent_load_does_not_500() -> None:
    """Repeated cold-starts followed by immediate JWKS calls must not 500.

    DOS-adjacent - confirms the lazy-init path is bounded in time and the
    per-call cost stays cheap.
    """
    import time

    from bernstein.core.routes.well_known import (
        _get_signing_keypair,
        _reset_signing_keypair_for_tests,
    )

    deadline = time.monotonic() + 1.0
    iterations = 0
    while time.monotonic() < deadline and iterations < 50:
        _reset_signing_keypair_for_tests()
        priv, pub = _get_signing_keypair()
        assert priv and pub
        iterations += 1
    # 50 cold-starts in a second is comfortably below worst-case Ed25519
    # keygen budgets (~1ms each on modern hardware). If this regresses to
    # under 10/s the JWKS path has become a DOS vector.
    assert iterations >= 10, (
        f"Cold-start path too slow: only {iterations} keygen+cache cycles "
        "completed in 1s; risk of starvation under burst load."
    )


def test_persisted_signing_key_file_mode_is_0600(tmp_path: Path) -> None:
    """The persisted private PEM is owner-only (FIXED).

    This was an xfail asserting ``.sdd/security/keys/agent_signing/private.pem``
    exists. Persistence landed as :class:`AgentCardKeystore`, rooted at
    ``.bernstein/keys`` (or ``BERNSTEIN_AGENT_CARD_KEY_DIR``), so the old
    assertion failed on "no persisted key file" and reported the control
    missing on a tree that had it.
    """
    from bernstein.core.security.agent_card_keystore import AgentCardKeystore

    keystore = AgentCardKeystore(tmp_path / "keys")
    keystore.load_or_generate()
    private_pem = tmp_path / "keys" / "agent-card.ed25519"
    assert private_pem.is_file()
    if sys.platform == "win32":
        pytest.skip("POSIX file modes are not meaningful on Windows")
    assert (private_pem.stat().st_mode & 0o777) == 0o600


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX file modes only")
def test_a_loosened_signing_key_is_refused_rather_than_loaded(tmp_path: Path) -> None:
    """Enforcing the mode at write is not enough if a later chmod is honoured."""
    from bernstein.core.security.agent_card_keystore import AgentCardKeystore

    keystore = AgentCardKeystore(tmp_path / "keys")
    keystore.load_or_generate()
    (tmp_path / "keys" / "agent-card.ed25519").chmod(0o644)

    with pytest.raises(PermissionError, match="refusing to load"):
        AgentCardKeystore(tmp_path / "keys").load_or_generate()


# ---------------------------------------------------------------------------
# RFC 8707 resource indicators
# ---------------------------------------------------------------------------


def test_rfc_8707_resource_indicator_mismatch_rejected() -> None:
    """A JWT whose ``resource`` claim points elsewhere is rejected (FIXED).

    This was an xfail whose body raised unconditionally, saying
    ``auth_middleware`` does not consult the claim. It does:
    ``_resource_indicator_check`` answers a mismatch with the RFC 6750
    challenge, and the middleware calls it before the request reaches its
    handler. End-to-end coverage lives in
    ``tests/unit/test_auth_middleware_resource_indicator.py``; this pins the
    predicate the finding was about.
    """
    from bernstein.core.security.auth_middleware import _resource_indicator_check

    expected = ("https://bernstein.example",)
    assert _resource_indicator_check({"resource": "https://bernstein.example"}, expected) is None
    assert _resource_indicator_check({"resource": ["https://bernstein.example"]}, expected) is None

    mismatch = _resource_indicator_check({"resource": "https://other.example"}, expected)
    assert mismatch is not None, "a token minted for another resource server was accepted"

    malformed = _resource_indicator_check({"resource": [1, 2]}, expected)
    assert malformed is not None, "a non-string resource indicator was accepted"


def test_rfc_8707_enforcement_is_opt_in() -> None:
    """With no configured resource, the check is skipped so upgrades do not break."""
    from bernstein.core.security.auth_middleware import _resource_indicator_check

    assert _resource_indicator_check({"resource": "https://other.example"}, ()) is None
    assert _resource_indicator_check({}, ("https://bernstein.example",)) is None


# ---------------------------------------------------------------------------
# Replay scenarios - across processes, after rotation
# ---------------------------------------------------------------------------


def test_card_minted_in_process_a_verifies_with_pubkey_from_process_a() -> None:
    """A card signed by a captured private key still verifies after the
    in-memory keypair is reset (i.e. the orchestrator process restarted).

    Confirms the verifier is purely a function of (card, signature, pubkey)
    - there is no hidden process-local state that would tie a verification
    to the signing process. This is what makes JWKS-based federation work.
    """
    priv, pub = generate_ed25519_keypair()
    card = _stable_card()
    sig = sign_agent_card(card, priv)

    # Simulate a process restart by re-importing well_known and resetting.
    from bernstein.core.routes.well_known import (
        _reset_signing_keypair_for_tests,
    )

    _reset_signing_keypair_for_tests()
    # The captured (priv, pub, sig) still verify even though the in-process
    # cache moved on.
    assert verify_agent_card(card, sig, pub) is True


def test_card_does_not_verify_with_post_rotation_pubkey() -> None:
    """A card signed by the old key must NOT verify with the new pubkey."""
    old_priv, _old_pub = generate_ed25519_keypair()
    _new_priv, new_pub = generate_ed25519_keypair()
    card = _stable_card()
    sig = sign_agent_card(card, old_priv)
    assert verify_agent_card(card, sig, new_pub) is False


# ---------------------------------------------------------------------------
# DOS - repeated rotation must not leak archive files
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    reason=(
        "Finding #10 - no on-disk rotation today, so no archive directory "
        "to grow. Once persistence lands, repeated rotation MUST garbage-"
        "collect old archived PEMs after the grace window expires; "
        "otherwise an attacker who can force rotations exhausts disk."
    ),
    strict=True,
)
def test_repeated_rotation_does_not_grow_archive_unboundedly() -> None:
    from pathlib import Path

    archive = Path(".sdd/security/keys/agent_signing/archive")
    assert archive.exists(), "persistence not landed - nothing to bound"
    # Once persistence lands: simulate N rotations and assert the archive
    # size stays under a fixed bound (e.g. grace_window_keys + 1).
    raise AssertionError("rotation archive bound not implemented")


# ---------------------------------------------------------------------------
# typ binding in both directions
# ---------------------------------------------------------------------------


def test_typ_jwt_signature_does_not_verify_as_agent_card() -> None:
    """A signature minted with ``typ: jwt`` must not verify as an agent card."""
    priv_pem, pub_pem = generate_ed25519_keypair()
    priv = serialization.load_pem_private_key(priv_pem, password=None)
    card = _stable_card()
    body_b64 = _b64url(canonicalize_jcs(asdict(card)))
    hdr_b64 = _b64url(canonicalize_jcs({"alg": "EdDSA", "typ": "jwt"}))
    signing_input = f"{hdr_b64}.{body_b64}".encode("ascii")
    sig_bytes = priv.sign(signing_input)
    forged = AgentCardSignature(
        detached_jws=f"{hdr_b64}..{_b64url(sig_bytes)}",
        kid="k",
    )
    assert verify_agent_card(card, forged, pub_pem) is False


def test_typ_missing_in_header_does_not_verify_as_agent_card() -> None:
    """A signature with NO ``typ`` claim must not verify - defaults are unsafe."""
    priv_pem, pub_pem = generate_ed25519_keypair()
    priv = serialization.load_pem_private_key(priv_pem, password=None)
    card = _stable_card()
    body_b64 = _b64url(canonicalize_jcs(asdict(card)))
    hdr_b64 = _b64url(canonicalize_jcs({"alg": "EdDSA"}))
    signing_input = f"{hdr_b64}.{body_b64}".encode("ascii")
    sig_bytes = priv.sign(signing_input)
    forged = AgentCardSignature(
        detached_jws=f"{hdr_b64}..{_b64url(sig_bytes)}",
        kid="k",
    )
    assert verify_agent_card(card, forged, pub_pem) is False


def test_typ_with_unicode_lookalike_rejected() -> None:
    """Cyrillic ``а`` masquerading as Latin ``a`` in ``typ`` must be rejected."""
    priv_pem, pub_pem = generate_ed25519_keypair()
    priv = serialization.load_pem_private_key(priv_pem, password=None)
    card = _stable_card()
    body_b64 = _b64url(canonicalize_jcs(asdict(card)))
    # Cyrillic small a (U+0430) instead of Latin a (U+0061) - the lookalike is
    # the entire point of the test, hence the noqa.
    spoof = "аgent-card+jws"  # noqa: RUF001
    hdr_b64 = _b64url(canonicalize_jcs({"alg": "EdDSA", "typ": spoof}))
    signing_input = f"{hdr_b64}.{body_b64}".encode("ascii")
    sig_bytes = priv.sign(signing_input)
    forged = AgentCardSignature(
        detached_jws=f"{hdr_b64}..{_b64url(sig_bytes)}",
        kid="k",
    )
    assert verify_agent_card(card, forged, pub_pem) is False
