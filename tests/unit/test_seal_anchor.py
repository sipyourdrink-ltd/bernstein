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

import base64
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.core.security.audit_receipt import _merkle_root_and_path
from bernstein.core.security.rfc3161_verifier import load_trusted_tsa_certs
from bernstein.core.security.seal_anchor import (
    ANCHOR_FILENAME,
    ANCHOR_KIND_RFC3161,
    ANCHOR_KIND_TRANSPARENCY_LOG,
    AnchorStatus,
    SealAnchor,
    SealAnchorError,
    _require_echoed_nonce,
    build_rfc3161_anchor,
    build_timestamp_request,
    build_transparency_log_anchor,
    load_anchor,
    request_timestamp_token,
    transparency_log_leaf,
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


# ---------------------------------------------------------------------------
# Transparency-log anchors (#6208 slice 1)
# ---------------------------------------------------------------------------

#: Fixture Ed25519 seed for the log that signs the tree head.
_LOG_SEED = bytes.fromhex("11" * 32)
_OTHER_LOG_SEED = bytes.fromhex("22" * 32)


def _log_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(_LOG_SEED)


def _public_hex(key: Ed25519PrivateKey) -> str:
    return key.public_key().public_bytes_raw().hex()


def _five_leaf_transparency_anchor(
    *,
    head: str | None = None,
    log_key: Ed25519PrivateKey | None = None,
) -> tuple[str, SealAnchor]:
    """Build a 5-leaf tree whose leaf 3 is the sealed head, and sign the STH."""
    sealed_head = head or hashlib.sha256(b"sealed-head-fixture").hexdigest()
    key = log_key or _log_key()
    leaves = [
        transparency_log_leaf(hashlib.sha256(f"padding-{index}".encode()).hexdigest())
        if index != 3
        else transparency_log_leaf(sealed_head)
        for index in range(5)
    ]
    root, path = _merkle_root_and_path(leaves, 3)
    audit_path = [{"hash": sibling, "left": is_left} for sibling, is_left in path]
    sth = {"root_hash": root, "tree_size": 5}
    signature = key.sign(json.dumps(sth, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    signed_tree_head = {
        **sth,
        "signature_b64": base64.b64encode(signature).decode("ascii"),
    }
    anchor = build_transparency_log_anchor(
        run_id="run-log",
        head_sha256=sealed_head,
        leaf_hash=leaves[3],
        tree_size=5,
        audit_path=audit_path,
        signed_tree_head=signed_tree_head,
        log_public_key=_public_hex(key),
    )
    return sealed_head, anchor


def test_inclusion_proof_recomputes_the_signed_tree_head() -> None:
    """A 5-leaf tree whose leaf 3 is the sealed head verifies offline at tree_size=5."""
    sealed_head, anchor = _five_leaf_transparency_anchor()

    result = verify_anchor(anchor, sealed_head=sealed_head, trusted_tsa_certs=[])

    assert result.status is AnchorStatus.VERIFIED, result.errors
    assert result.errors == []
    assert result.tree_size == 5
    assert result.gen_time is None


def test_tampered_sealed_head_fails_inclusion() -> None:
    """Changing the stored head without rebuilding the proof is a loud failure."""
    sealed_head, anchor = _five_leaf_transparency_anchor()
    other_head = hashlib.sha256(b"different-sealed-head").hexdigest()
    tampered = replace(anchor, head_sha256=other_head)

    result = verify_anchor(tampered, sealed_head=other_head, trusted_tsa_certs=[])

    assert result.status is AnchorStatus.INVALID
    assert result.tree_size is None
    assert any("leaf" in err or "root" in err for err in result.errors)
    assert sealed_head != other_head


def test_tree_head_signed_by_another_log_key_is_refused() -> None:
    """An inclusion proof is not evidence if a different log key is attached."""
    sealed_head, anchor = _five_leaf_transparency_anchor()
    other_key = Ed25519PrivateKey.from_private_bytes(_OTHER_LOG_SEED)
    tampered = replace(anchor, log_public_key=_public_hex(other_key))

    result = verify_anchor(tampered, sealed_head=sealed_head, trusted_tsa_certs=[])

    assert result.status is AnchorStatus.INVALID
    assert any("signature" in err for err in result.errors)


def test_existing_rfc3161_anchor_files_still_load(
    freetsa_token: bytes,
    sealed_head: str,
    tmp_path: Path,
) -> None:
    """v3.19.2 records (RFC 3161 fields only) still load after the log fields landed."""
    record = {
        "schema_version": "1.0.0",
        "run_id": "run-legacy",
        "head_sha256": sealed_head,
        "anchor_kind": "rfc3161",
        "rfc3161_token_b64": base64.b64encode(freetsa_token).decode("ascii"),
        "rfc3161_tsa_url": "https://freetsa.org/tsr",
    }
    assert "leaf_hash" not in record
    assert "signed_tree_head" not in record
    path = tmp_path / "legacy_seal_anchor.json"
    path.write_text(json.dumps(record), encoding="utf-8")

    loaded = load_anchor(path)

    assert loaded.anchor_kind == ANCHOR_KIND_RFC3161
    assert loaded.head_sha256 == sealed_head
    assert loaded.token_b64 == record["rfc3161_token_b64"]
    assert loaded.leaf_hash is None
    assert loaded.tree_size is None
    assert loaded.audit_path is None
    assert loaded.signed_tree_head is None
    assert loaded.log_public_key is None


def test_transparency_log_anchor_round_trips_through_disk(tmp_path: Path) -> None:
    """A log anchor reloads as the same record, without TSA fields."""
    _head, anchor = _five_leaf_transparency_anchor()
    path = tmp_path / ANCHOR_FILENAME
    write_anchor(path, anchor)

    assert load_anchor(path) == anchor
    stored = json.loads(path.read_text(encoding="utf-8"))
    assert stored["anchor_kind"] == ANCHOR_KIND_TRANSPARENCY_LOG
    assert "rfc3161_token_b64" not in stored
    assert "created_at" not in stored
    assert "timestamp" not in stored


def test_transparency_log_leaf_is_the_bare_sealed_head_digest() -> None:
    """The leaf is domain-separated over the raw head bytes, not a run statement."""
    from bernstein.core.persistence.merkle import _leaf_digest

    head = hashlib.sha256(b"bare-head").hexdigest()
    assert transparency_log_leaf(head) == _leaf_digest(bytes.fromhex(head))
    assert transparency_log_leaf(head) != head
