"""Deterministic SPIFFE-ID derivation and parsing (issue #2363, AC 4).

The mapping from the Ed25519 install identity plus an agent card to a
``spiffe://<trust-domain>/bernstein/<install>/<agent>`` id must be a pure,
byte-stable function so two operators deriving the id for the same install and
agent arrive at the same string, and so a verifier can re-derive it later.
"""

from __future__ import annotations

import pytest

from bernstein.core.identity.spiffe import (
    SpiffeId,
    SpiffeIdError,
    TrustDomainError,
    derive_spiffe_id,
    derive_spiffe_id_from_key,
    install_segment,
    parse_spiffe_id,
    validate_path_segment,
    validate_trust_domain,
)


class TestTrustDomain:
    def test_accepts_dns_like_lowercase(self) -> None:
        assert validate_trust_domain("example.org") == "example.org"
        assert validate_trust_domain("prod-cluster.internal") == "prod-cluster.internal"

    @pytest.mark.parametrize(
        "bad",
        ["Example.org", "spiffe://example.org", "has space", "", "a" * 256, "trust/domain"],
    )
    def test_rejects_invalid(self, bad: str) -> None:
        with pytest.raises(TrustDomainError):
            validate_trust_domain(bad)


class TestPathSegment:
    def test_accepts_valid(self) -> None:
        assert validate_path_segment("backend-1") == "backend-1"
        assert validate_path_segment("agent_x.y") == "agent_x.y"

    @pytest.mark.parametrize("bad", ["", ".", "..", "has/slash", "sp ace", "emoji✨"])
    def test_rejects_invalid(self, bad: str) -> None:
        with pytest.raises(SpiffeIdError):
            validate_path_segment(bad)


class TestDerivation:
    def test_scheme_and_shape(self, install_keypair: tuple[bytes, bytes]) -> None:
        _priv, pub = install_keypair
        sid = derive_spiffe_id_from_key(trust_domain="example.org", install_public_key_pem=pub, agent_id="backend-1")
        seg = install_segment(pub)
        assert sid == f"spiffe://example.org/bernstein/{seg}/backend-1"

    def test_deterministic_same_inputs(self, install_keypair: tuple[bytes, bytes]) -> None:
        _priv, pub = install_keypair
        a = derive_spiffe_id_from_key(trust_domain="ex.org", install_public_key_pem=pub, agent_id="a1")
        b = derive_spiffe_id_from_key(trust_domain="ex.org", install_public_key_pem=pub, agent_id="a1")
        assert a == b

    def test_install_segment_is_16_hex(self, install_keypair: tuple[bytes, bytes]) -> None:
        _priv, pub = install_keypair
        seg = install_segment(pub)
        assert len(seg) == 16
        assert all(c in "0123456789abcdef" for c in seg)

    def test_different_agent_ids_differ(self, install_keypair: tuple[bytes, bytes]) -> None:
        _priv, pub = install_keypair
        a = derive_spiffe_id_from_key(trust_domain="ex.org", install_public_key_pem=pub, agent_id="a1")
        b = derive_spiffe_id_from_key(trust_domain="ex.org", install_public_key_pem=pub, agent_id="a2")
        assert a != b

    def test_derive_from_precomputed_install_id(self) -> None:
        sid = derive_spiffe_id(trust_domain="ex.org", install_id="deadbeefdeadbeef", agent_id="a1")
        assert sid == "spiffe://ex.org/bernstein/deadbeefdeadbeef/a1"

    def test_rejects_bad_agent_id(self) -> None:
        with pytest.raises(SpiffeIdError):
            derive_spiffe_id(trust_domain="ex.org", install_id="deadbeefdeadbeef", agent_id="bad/id")


class TestParse:
    def test_round_trip(self) -> None:
        uri = "spiffe://ex.org/bernstein/deadbeefdeadbeef/backend-1"
        parsed = parse_spiffe_id(uri)
        assert isinstance(parsed, SpiffeId)
        assert parsed.trust_domain == "ex.org"
        assert parsed.install_id == "deadbeefdeadbeef"
        assert parsed.agent_id == "backend-1"
        assert parsed.uri == uri

    @pytest.mark.parametrize(
        "bad",
        [
            "https://ex.org/bernstein/x/y",
            "spiffe://ex.org",
            "spiffe://ex.org/other/x/y",
            "spiffe:///bernstein/x/y",
        ],
    )
    def test_rejects_non_bernstein(self, bad: str) -> None:
        with pytest.raises(SpiffeIdError):
            parse_spiffe_id(bad)
