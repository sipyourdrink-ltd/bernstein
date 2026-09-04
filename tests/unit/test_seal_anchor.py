"""External existence proofs for a run's sealed journal head (issue #4205).

The HMAC audit chain and the Ed25519 lineage signatures prove *what* a run
recorded, to whoever holds the key. Neither pins *when* the head existed, and
neither stops a key holder from rewriting the journal and re-sealing it: the
rewrite is internally consistent and nothing outside the install ever saw the
original.

An RFC 3161 timestamp token over the sealed head closes that. The TSA signs
``genTime`` together with the head digest, so the token is an independent
witness that this exact head existed before that instant - and it stays
checkable offline, from the token and an operator-pinned trust bundle alone.

These tests use the real FreeTSA fixture already checked into
``tests/fixtures/rfc3161/`` so the chain being exercised is a genuine one.
The fixture token covers ``sha256(freetsa_payload.txt)``, so the tests treat
that digest as the run's sealed head - the head is opaque hex to the anchor
either way.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from bernstein.core.security.rfc3161_verifier import load_trusted_tsa_certs
from bernstein.core.security.seal_anchor import (
    ANCHOR_FILENAME,
    AnchorStatus,
    SealAnchor,
    SealAnchorError,
    _require_echoed_nonce,
    build_rfc3161_anchor,
    build_timestamp_request,
    load_anchor,
    request_timestamp_token,
    verify_anchor,
    write_anchor,
)

_FIXTURE_DIR = Path(__file__).parent.parent / "fixtures" / "rfc3161"


@pytest.fixture(scope="module")
def freetsa_token() -> bytes:
    return (_FIXTURE_DIR / "freetsa_token_with_certs.tsr").read_bytes()


@pytest.fixture(scope="module")
def sealed_head() -> str:
    """The head the fixture token actually imprints."""
    return hashlib.sha256((_FIXTURE_DIR / "freetsa_payload.txt").read_bytes()).hexdigest()


@pytest.fixture(scope="module")
def freetsa_trust() -> list:
    return load_trusted_tsa_certs(_FIXTURE_DIR / "freetsa_cacert.pem")


def _anchor(head: str, token: bytes) -> SealAnchor:
    return build_rfc3161_anchor(
        run_id="run-anchor",
        head_sha256=head,
        token_der=token,
        tsa_url="https://freetsa.org/tsr",
    )


def test_anchor_verifies_the_sealed_head_it_was_issued_for(
    freetsa_token: bytes,
    sealed_head: str,
    freetsa_trust: list,
) -> None:
    """A token whose imprint is the sealed head verifies, and carries the TSA's time."""
    result = verify_anchor(
        _anchor(sealed_head, freetsa_token),
        sealed_head=sealed_head,
        trusted_tsa_certs=freetsa_trust,
    )

    assert result.status is AnchorStatus.VERIFIED, result.errors
    assert result.errors == []
    # The point of the anchor: a time nobody on this install chose.
    assert result.gen_time is not None
    assert result.tsa_subject is not None


def test_mutated_head_fails_anchor_verification(
    freetsa_token: bytes,
    sealed_head: str,
    freetsa_trust: list,
) -> None:
    """Rewriting the run and re-sealing it does not carry the anchor along.

    The load-bearing property: the anchor witnesses one head. Present it
    against any other head and the verdict is a loud mismatch, never a pass
    and never the weaker "unverifiable".
    """
    result = verify_anchor(
        _anchor(sealed_head, freetsa_token),
        sealed_head="0" * 64,
        trusted_tsa_certs=freetsa_trust,
    )

    assert result.status is AnchorStatus.MISMATCHED
    assert any("head" in err for err in result.errors)


def test_tampered_timestamp_token_fails_anchor_verification(
    freetsa_token: bytes,
    sealed_head: str,
    freetsa_trust: list,
) -> None:
    """Editing the stored token breaks the TSA's signature rather than degrading quietly."""
    mutated = bytearray(freetsa_token)
    mutated[-1] ^= 0xFF

    result = verify_anchor(
        _anchor(sealed_head, bytes(mutated)),
        sealed_head=sealed_head,
        trusted_tsa_certs=freetsa_trust,
    )

    assert result.status is AnchorStatus.INVALID
    assert result.errors


def test_anchor_without_trust_anchors_is_unverifiable_never_verified(
    freetsa_token: bytes,
    sealed_head: str,
) -> None:
    """No pinned TSA roots means no verdict - the anchor must not read as proven."""
    result = verify_anchor(
        _anchor(sealed_head, freetsa_token),
        sealed_head=sealed_head,
        trusted_tsa_certs=[],
    )

    assert result.status is AnchorStatus.UNVERIFIABLE
    assert result.gen_time is None


def test_anchor_record_round_trips_through_disk(
    tmp_path: Path,
    freetsa_token: bytes,
    sealed_head: str,
) -> None:
    """The stored record is the anchor - reloading it changes nothing."""
    anchor = _anchor(sealed_head, freetsa_token)
    path = tmp_path / ANCHOR_FILENAME
    write_anchor(path, anchor)

    assert load_anchor(path) == anchor
    # No local wall clock is recorded: the only time the anchor carries is the
    # TSA's, inside the token.
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert "created_at" not in stored
    assert "timestamp" not in stored


def test_anchor_rejects_a_head_that_is_not_a_sha256_digest(freetsa_token: bytes) -> None:
    """The head is the TSA messageImprint - a non-digest can never be one."""
    with pytest.raises(SealAnchorError):
        build_rfc3161_anchor(
            run_id="run-anchor",
            head_sha256="not-a-digest",
            token_der=freetsa_token,
            tsa_url="https://freetsa.org/tsr",
        )


def test_loading_a_record_with_an_unknown_anchor_kind_is_refused(
    tmp_path: Path,
    freetsa_token: bytes,
    sealed_head: str,
) -> None:
    """An unrecognised anchor kind is refused, not verified by the wrong rules."""
    path = tmp_path / ANCHOR_FILENAME
    write_anchor(path, _anchor(sealed_head, freetsa_token))
    record = json.loads(path.read_text(encoding="utf-8"))
    record["anchor_kind"] = "rfc6962"
    path.write_text(json.dumps(record), encoding="utf-8")

    with pytest.raises(SealAnchorError):
        load_anchor(path)


def test_timestamp_request_imprints_the_sealed_head(sealed_head: str) -> None:
    """The request we send a TSA carries the head digest and nothing else about the run."""
    from asn1crypto import tsp

    request = tsp.TimeStampReq.load(build_timestamp_request(sealed_head, nonce=42))

    assert request["message_imprint"]["hashed_message"].native == bytes.fromhex(sealed_head)
    assert request["message_imprint"]["hash_algorithm"]["algorithm"].native == "sha256"
    # ``cert_req`` is what makes the reply self-contained enough to verify
    # offline later: without it the TSA omits its own certificate.
    assert request["cert_req"].native is True
    assert request["nonce"].native == 42


def test_a_reply_that_does_not_echo_the_request_nonce_is_refused(
    freetsa_token: bytes,
    sealed_head: str,
) -> None:
    """A token that answers some other request is not evidence about this head.

    The fixture was requested with ``-no_nonce``, so it echoes nothing - the
    same shape a cached or replayed reply has.
    """
    request = build_timestamp_request(sealed_head, nonce=12345)

    with pytest.raises(SealAnchorError, match="nonce"):
        _require_echoed_nonce(request, freetsa_token)


def test_a_non_http_tsa_url_is_refused_before_any_request(sealed_head: str) -> None:
    """The TSA endpoint is a URL to POST to, never a local file to read."""
    with pytest.raises(SealAnchorError, match="refusing to contact TSA"):
        request_timestamp_token("file:///etc/passwd", build_timestamp_request(sealed_head, nonce=1))
