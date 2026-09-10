"""The ``alg`` header is a claim about the signature; it has to be true.

``JWTManager`` took an ``algorithm``, wrote it into the token header, and
compared it on the way back in - but signed with SHA-256 unconditionally. The
manager therefore agreed with itself for any value, so nothing inside it could
notice, while the header told every other reader something false.

``auth.verify_jwt`` is the reader that matters: it selects its digest from the
``algorithm`` argument, and ``BERNSTEIN_AUTH_JWT_ALGORITHM`` feeds the same
value to both sides. Configure ``HS384`` and the issuer signs SHA-256 under an
``HS384`` header while the verifier computes SHA-384 - every token the system
issues is rejected by the system's own verifier.

``auth.create_jwt`` next door has always honoured all three and raised on
anything else. These tests pin the manager to that behaviour.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json

import pytest

from bernstein.core.preview.token_issuer import PreviewTokenIssuer
from bernstein.core.security.auth import verify_jwt
from bernstein.core.security.jwt_tokens import JWTManager

_SECRET = "shared-secret"

_DIGESTS = {"HS256": hashlib.sha256, "HS384": hashlib.sha384, "HS512": hashlib.sha512}


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _header(token: str) -> dict[str, str]:
    return json.loads(_b64url_decode(token.split(".")[0]))


def _signature(token: str) -> bytes:
    return _b64url_decode(token.split(".")[2])


@pytest.mark.parametrize("algorithm", sorted(_DIGESTS))
def test_the_signature_uses_the_digest_the_header_names(algorithm: str) -> None:
    """The bug, stated directly: recompute the HMAC the header claims."""
    token = JWTManager(secret=_SECRET, algorithm=algorithm).create_token("s-1")
    signing_input = ".".join(token.split(".")[:2])

    expected = hmac.new(_SECRET.encode(), signing_input.encode(), _DIGESTS[algorithm]).digest()

    assert _header(token)["alg"] == algorithm
    assert _signature(token) == expected


@pytest.mark.parametrize("algorithm", sorted(_DIGESTS))
def test_a_token_this_manager_issues_is_one_auth_verify_jwt_accepts(algorithm: str) -> None:
    """Both sides read ``BERNSTEIN_AUTH_JWT_ALGORITHM``; they have to agree.

    On main this returns ``None`` for HS384 and HS512: the manager signed
    SHA-256 and the verifier computed the digest the header named.
    """
    token = JWTManager(secret=_SECRET, algorithm=algorithm).create_token("s-1")

    assert verify_jwt(token, _SECRET, algorithm) is not None


@pytest.mark.parametrize("algorithm", sorted(_DIGESTS))
def test_signature_length_matches_the_named_digest(algorithm: str) -> None:
    """A cheap, independent read on the same fact: SHA-384 is 48 bytes."""
    token = JWTManager(secret=_SECRET, algorithm=algorithm).create_token("s-1")

    assert len(_signature(token)) == _DIGESTS[algorithm]().digest_size


@pytest.mark.parametrize("algorithm", sorted(_DIGESTS))
def test_the_manager_still_round_trips_its_own_token(algorithm: str) -> None:
    """Sign and verify move together, so this passed before the fix too.

    It is here as the control: it is what made the defect invisible from
    inside the class, and it must keep passing.
    """
    manager = JWTManager(secret=_SECRET, algorithm=algorithm)
    payload = manager.verify_token(manager.create_token("s-1", scopes=["a"]))

    assert payload is not None
    assert payload.session_id == "s-1"
    assert payload.scopes == ["a"]


def test_a_token_signed_under_one_algorithm_is_rejected_by_a_manager_on_another() -> None:
    """The alg-confusion guard still holds once the digests actually differ."""
    token = JWTManager(secret=_SECRET, algorithm="HS512").create_token("s-1")

    assert JWTManager(secret=_SECRET, algorithm="HS256").verify_token(token) is None


@pytest.mark.parametrize("algorithm", ["RS256", "ES256", "none", "HS255", "hs256", ""])
def test_an_algorithm_the_manager_cannot_sign_with_is_refused(algorithm: str) -> None:
    """Refused, not silently downgraded to HS256.

    ``RS256`` is the case that matters: it names asymmetric signing, and a
    token labelled ``RS256`` but signed with an HMAC invites a verifier to
    treat the shared secret as a public key. Never issuing that header is the
    only guarantee available here. ``auth.create_jwt`` already raises on the
    same input.
    """
    with pytest.raises(ValueError, match="Unsupported algorithm"):
        JWTManager(secret=_SECRET, algorithm=algorithm)


def test_the_default_algorithm_is_unchanged() -> None:
    assert _header(JWTManager(secret=_SECRET).create_token("s-1"))["alg"] == "HS256"


class TestPreviewTokenIssuer:
    """The one caller that threads an operator-supplied algorithm through."""

    def test_a_bad_algorithm_is_refused_when_the_issuer_is_built(self) -> None:
        """Not on the first preview link somebody tries to create.

        The issuer constructs its manager inside ``issue()``, so without this
        the misconfiguration surfaces arbitrarily far from its cause.
        """
        with pytest.raises(ValueError, match="Unsupported algorithm"):
            PreviewTokenIssuer(_SECRET, algorithm="RS256")

    @pytest.mark.parametrize("algorithm", sorted(_DIGESTS))
    def test_a_supported_algorithm_issues_a_verifiable_token(self, algorithm: str) -> None:
        issued = PreviewTokenIssuer(_SECRET, algorithm=algorithm).issue(
            preview_id="p-1",
            mode="token",
            expires_in_seconds=600,
        )

        assert issued.token is not None
        assert _header(issued.token)["alg"] == algorithm
        assert verify_jwt(issued.token, _SECRET, algorithm) is not None
