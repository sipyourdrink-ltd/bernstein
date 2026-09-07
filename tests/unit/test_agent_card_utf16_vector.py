"""Agent-card UTF-16 property-name-order fixture, from the real signing path (#5551).

``tests/unit/test_canonicalize_jcs_key_order.py`` proves ``canonicalize_jcs``
sorts property names by UTF-16 code units against hand-built dicts. What that
file cannot supply is a record that came out of a real signing path and still
exercises the one case where UTF-16 order disagrees with the code-point
shortcut -- a supplementary-plane name against a name in U+E000..U+FFFF.
Every record this codebase actually emits today has schema-fixed ASCII keys,
so a producer that took the code-point shortcut would pass every corpus we
have and only diverge the first time a key like this one reached it.

``tests/fixtures/agent-card-utf16-vector/agent-card-utf16-vector.json`` closes
that gap: an ``AgentIdentityCard`` whose open-membership ``extensions`` field
carries both U+FFFF and U+1D11E as keys, canonicalised and signed by the
production ``sign_agent_card`` / ``canonicalize_jcs`` path, not by a
generator written for this fixture alone.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from bernstein.core.identity.agent_card import AgentIdentityCard
from bernstein.core.security.agent_card_signer import (
    AgentCardSignature,
    canonicalize_jcs,
    verify_agent_card,
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_VECTORS = _REPO_ROOT / "tests" / "fixtures" / "agent-card-utf16-vector"
_CARD = _VECTORS / "agent-card-utf16-vector.json"
_CARD_SHA256 = _VECTORS / "agent-card-utf16-vector.sha256"
_SIGNATURE = _VECTORS / "agent-card-utf16-vector-signature.json"
_PUBKEY = _VECTORS / "agent-card-utf16-vector-key.pem"
_BUILDER = _VECTORS / "_build_agent_card_utf16_vector.py"

#: The exact disagreeing pair the issue measures.
_BMP_KEY = "￿"
_SUPPLEMENTARY_KEY = "\U0001d11e"


def _card_dict() -> dict[str, Any]:
    return json.loads(_CARD.read_bytes())


def _signature() -> AgentCardSignature:
    payload = json.loads(_SIGNATURE.read_bytes())
    return AgentCardSignature(
        detached_jws=payload["detached_jws"],
        kid=payload["kid"],
        alg=payload["alg"],
    )


def _load_builder() -> Any:
    """Import the vector generator as a module (it must not build on import)."""
    spec = importlib.util.spec_from_file_location("_build_agent_card_utf16_vector", _BUILDER)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# AC1: a fixture record exists, produced by the production signing path,
# carrying at least one supplementary-plane key.
# ---------------------------------------------------------------------------


def test_fixture_extensions_carry_the_supplementary_plane_key() -> None:
    extensions = _card_dict()["extensions"]
    assert _SUPPLEMENTARY_KEY in extensions
    assert _BMP_KEY in extensions


def test_fixture_reproduces_the_committed_bytes() -> None:
    """The committed file *is* its own canonical encoding, byte for byte."""
    on_disk = _CARD.read_bytes()
    assert canonicalize_jcs(json.loads(on_disk)) == on_disk


def test_fixture_matches_its_published_digest() -> None:
    published = _CARD_SHA256.read_text(encoding="utf-8").split()[0]
    import hashlib

    assert hashlib.sha256(_CARD.read_bytes()).hexdigest() == published


# ---------------------------------------------------------------------------
# AC2: the emitted byte order matches UTF-16 code-unit ordering and differs
# from code-point ordering for this input -- verified in both directions.
# ---------------------------------------------------------------------------


def test_emitted_order_matches_utf16_code_units() -> None:
    """U+1D11E (lead surrogate U+D834) sorts *below* U+FFFF in UTF-16 order."""
    on_disk = _CARD.read_bytes().decode("utf-8")
    assert on_disk.index(_SUPPLEMENTARY_KEY) < on_disk.index(_BMP_KEY)


def test_emitted_order_differs_from_code_point_ordering() -> None:
    """Guard: the vector is only meaningful because the two orders disagree.

    Code-point order puts U+FFFF (65535) before U+1D11E (119070) -- the
    opposite of the UTF-16 order asserted above. If a future change made the
    two orders agree here, this test would stop proving anything, so the
    disagreement itself is asserted, not assumed.
    """
    card = _card_dict()
    code_point_bytes = json.dumps(
        card,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    assert canonicalize_jcs(card) != code_point_bytes
    assert canonicalize_jcs(card) == _CARD.read_bytes()


# ---------------------------------------------------------------------------
# AC3: our verifier accepts the record; a canonicalizer sorting by code
# point computes different signing bytes and is rejected. Both directions.
# ---------------------------------------------------------------------------


def test_verifier_accepts_the_production_signature() -> None:
    card = AgentIdentityCard(**_card_dict())
    assert verify_agent_card(card, _signature(), _PUBKEY.read_bytes()) is True


def test_verifier_rejects_a_signature_over_code_point_ordered_bytes() -> None:
    """A hypothetical producer that canonicalised by code point instead of
    UTF-16 code units signs different bytes for the same card, and this
    codebase's verifier -- which always recomputes via ``canonicalize_jcs``,
    the correct RFC 8785 encoder -- must reject that signature rather than
    happen to accept it.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    card_dict = _card_dict()
    card = AgentIdentityCard(**card_dict)

    header = {"alg": "EdDSA", "typ": "agent-card+jws", "kid": _signature().kid}
    header_b64 = canonicalize_jcs(header)

    def _b64url(data: bytes) -> str:
        import base64

        return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")

    code_point_body = json.dumps(
        card_dict,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    wrong_signing_input = f"{_b64url(header_b64)}.{_b64url(code_point_body)}".encode("ascii")

    # Same private key as the fixture (seed b"c" * 32) -- the point under
    # test is the canonicalization, not key custody.
    wrong_private_key = Ed25519PrivateKey.from_private_bytes(b"c" * 32)
    wrong_signature_bytes = wrong_private_key.sign(wrong_signing_input)
    wrong_signature = AgentCardSignature(
        detached_jws=f"{_b64url(header_b64)}..{_b64url(wrong_signature_bytes)}",
        kid=_signature().kid,
        alg="EdDSA",
    )

    assert verify_agent_card(card, wrong_signature, _PUBKEY.read_bytes()) is False
    # And the production signature over the correct (UTF-16) bytes still
    # verifies against the same card and key -- the rejection above is about
    # the ordering, not about the key or the card.
    assert verify_agent_card(card, _signature(), _PUBKEY.read_bytes()) is True


# ---------------------------------------------------------------------------
# AC4 & AC5: documented, and the generation path is reproducible.
# ---------------------------------------------------------------------------


def test_fixture_is_documented() -> None:
    readme = (_VECTORS / "README.md").read_text(encoding="utf-8")
    assert "U+FFFF" in readme
    assert "U+1D11E" in readme


def test_regenerating_the_fixture_is_byte_identical_to_the_committed_files(tmp_path: Path) -> None:
    """Importing the generator builds nothing; calling it reproduces the bytes."""
    builder = _load_builder()
    builder.build(tmp_path)
    assert (tmp_path / _CARD.name).read_bytes() == _CARD.read_bytes()
    assert (tmp_path / _SIGNATURE.name).read_bytes() == _SIGNATURE.read_bytes()
    assert (tmp_path / _PUBKEY.name).read_bytes() == _PUBKEY.read_bytes()


@pytest.mark.parametrize("field", ["agent_id", "role", "adapter", "model"])
def test_fixture_card_has_required_fields(field: str) -> None:
    assert _card_dict()[field]
