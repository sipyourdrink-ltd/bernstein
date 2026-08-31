"""Committed trust-record test vectors are exercised by CI (issues #4760-#4764).

``tests/fixtures/trust-record-vectors/`` carries a single-execution Trust
Record, a delegated parent+child pair, and a run-level aggregate over that
pair (issue #4763), all produced by the real ``TrustRecordEmitter`` over
real ``EventJournal``-recorded runs (never hand-written JSON -- see
``_build_trust_record_vectors.py`` in that directory). These tests
re-verify their signatures and full field surface from the committed bytes
alone: no network, and no separately-known key file required (``cnf.jwk``
carries the public key needed to verify). The committed public key PEM is
pinned alongside purely as a second, independent check that the two
agree -- not as something a verifier needs.
"""

from __future__ import annotations

import base64
import hashlib
import json
from pathlib import Path
from typing import Any

from bernstein.core.security.agent_card_signer import canonicalize_jcs

_REPO_ROOT = Path(__file__).resolve().parents[2]
_VECTORS = _REPO_ROOT / "tests" / "fixtures" / "trust-record-vectors"
_SOLO = _VECTORS / "single-execution-trust-record.json"
_PARENT = _VECTORS / "delegated-parent-trust-record.json"
_CHILD = _VECTORS / "delegated-child-trust-record.json"
_GRANDCHILD = _VECTORS / "delegated-grandchild-trust-record.json"
_AGGREGATE = _VECTORS / "aggregate-trust-record.json"
_PUBKEY = _VECTORS / "trust-record-vectors-key.pem"

#: Every top-level field that is *always* signed, mirroring
#: ``trust_record._BASE_SIGNED_FIELDS``. ``delegation``/``references`` are
#: deliberately excluded here: they only enter the signed body when the
#: record actually carries them -- see ``_canonical_body_bytes`` below.
_BASE_SIGNED_FIELDS: tuple[str, ...] = (
    "eat_profile",
    "iat",
    "subject",
    "model",
    "runtime",
    "policy",
    "data_class",
    "tool_transcript",
    "build_provenance",
    "appraisal",
    "cnf",
)
_REQUIRED_TOP_LEVEL_FIELDS: tuple[str, ...] = (
    "eat_profile",
    "iat",
    "subject",
    "model",
    "runtime",
    "policy",
    "data_class",
    "build_provenance",
    "appraisal",
    "cnf",
    "signature",
)


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def _canonical_body_bytes(doc: dict[str, Any]) -> bytes:
    """Rebuild the exact bytes the emitter signed from a parsed record.

    ``delegation``/``references`` are only added when *doc* actually
    carries them: the signed pre-image is the record as serialized (see
    ``trust_record``'s "Signing pre-image, corrected" docstring note), not
    a form padded out with an explicit ``null`` for an absent member.
    """
    body = {field: doc[field] for field in _BASE_SIGNED_FIELDS}
    if "delegation" in doc:
        body["delegation"] = doc["delegation"]
    if "references" in doc:
        body["references"] = doc["references"]
    return canonicalize_jcs(body)


def _verify_offline(doc: dict[str, Any], public_key_pem: bytes) -> bool:
    """Re-verify a parsed record's bare Ed25519 ``signature`` string, offline.

    Per the schema, ``signature`` is "a signature ... by the cnf key over
    the canonical JSON form of the record with only this field absent" --
    a bare base64url Ed25519 signature, no JOSE/JWS framing to unwrap.
    """
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.serialization import load_pem_public_key

    public_key = load_pem_public_key(public_key_pem)
    sig_b64 = doc["signature"]
    padded = sig_b64 + "=" * (-len(sig_b64) % 4)
    raw_sig = base64.urlsafe_b64decode(padded)
    try:
        public_key.verify(raw_sig, _canonical_body_bytes(doc))
    except InvalidSignature:
        return False
    return True


def _public_key_pem_from_cnf_jwk(doc: dict[str, Any]) -> bytes:
    """Recover the SPKI PEM public key from a record's ``cnf.jwk``."""
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    jwk = doc["cnf"]["jwk"]
    assert jwk["kty"] == "OKP"
    assert jwk["crv"] == "Ed25519"
    x_b64 = jwk["x"]
    padded = x_b64 + "=" * (4 - len(x_b64) % 4)
    raw_public_key = base64.urlsafe_b64decode(padded)
    return Ed25519PublicKey.from_public_bytes(raw_public_key).public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def test_single_execution_vector_has_all_required_top_level_fields() -> None:
    doc = _load(_SOLO)
    for field in _REQUIRED_TOP_LEVEL_FIELDS:
        assert field in doc, f"missing required top-level field: {field}"


def test_single_execution_vector_verifies_offline_via_its_cnf_jwk() -> None:
    doc = _load(_SOLO)
    public_key_pem = _public_key_pem_from_cnf_jwk(doc)
    assert _verify_offline(doc, public_key_pem) is True


def test_single_execution_vector_has_no_delegation_member() -> None:
    doc = _load(_SOLO)
    assert "delegation" not in doc


def test_single_execution_vector_subject_is_execution_scoped_spiffe() -> None:
    doc = _load(_SOLO)
    assert doc["subject"] == "spiffe://bernstein.run/run/trust-record-vector-run/exec/trust-record-vector-solo"


def test_single_execution_vector_has_a_produced_artifact_reference_and_no_evidence_rel() -> None:
    doc = _load(_SOLO)
    rels = {r["rel"] for r in doc["references"]}
    assert "produced-artifact" in rels
    assert "evidence" not in rels


def test_single_execution_vector_tool_transcript_call_count_matches_recorded_tool_calls() -> None:
    doc = _load(_SOLO)
    # The generator records exactly one tool_call event for the solo run.
    assert doc["tool_transcript"]["call_count"] == 1


def test_delegated_parent_vector_verifies_offline() -> None:
    doc = _load(_PARENT)
    public_key_pem = _public_key_pem_from_cnf_jwk(doc)
    assert _verify_offline(doc, public_key_pem) is True


def test_delegated_child_vector_verifies_offline() -> None:
    doc = _load(_CHILD)
    public_key_pem = _public_key_pem_from_cnf_jwk(doc)
    assert _verify_offline(doc, public_key_pem) is True


def test_delegated_child_vector_parent_record_hash_matches_the_committed_parent_bytes() -> None:
    """Documents the exact canonicalization ``delegation.parent_record_hash`` covers.

    The hash is ``sha256:`` + the hex SHA-256 of the JCS (RFC 8785)
    canonicalisation of the complete signed parent record.
    """
    child = _load(_CHILD)
    parent_doc = _load(_PARENT)
    from bernstein.core.security.agent_card_signer import canonicalize_jcs

    expected = f"sha256:{hashlib.sha256(canonicalize_jcs(parent_doc)).hexdigest()}"
    assert child["delegation"]["parent_record_hash"] == expected


def test_delegated_child_vector_carries_the_delegation_credential_id() -> None:
    child = _load(_CHILD)
    assert child["delegation"]["credential_id"] == "trust-record-vector-delegation-credential:scope=narrow"


def test_delegated_parent_and_child_have_different_policy_bundle_hashes() -> None:
    """The child's resolved gate config is a narrowed subset of the parent's;
    a delegated hop is not assumed to inherit its parent's policy bundle verbatim."""
    parent = _load(_PARENT)
    child = _load(_CHILD)
    assert parent["policy"]["bundle_hash"] != child["policy"]["bundle_hash"]


def test_committed_public_key_matches_the_key_recovered_from_cnf_jwk() -> None:
    """Belt-and-suspenders: the pinned PEM and cnf.jwk must agree.

    Not something a real verifier needs (cnf.jwk alone is enough) --
    this just guards the fixture-generation script against pinning the
    wrong key file.
    """
    doc = _load(_SOLO)
    from_cnf = _public_key_pem_from_cnf_jwk(doc)
    pinned = _PUBKEY.read_bytes()
    assert from_cnf == pinned


def test_aggregate_vector_verifies_offline() -> None:
    doc = _load(_AGGREGATE)
    public_key_pem = _public_key_pem_from_cnf_jwk(doc)
    assert _verify_offline(doc, public_key_pem) is True


def test_aggregate_vector_has_no_delegation_member() -> None:
    """A rollup is not a hop acting under delegated authority."""
    doc = _load(_AGGREGATE)
    assert "delegation" not in doc


def test_aggregate_vector_subject_is_run_scoped_not_execution_scoped() -> None:
    doc = _load(_AGGREGATE)
    assert doc["subject"] == "spiffe://bernstein.run/run/trust-record-vector-run"
    assert "/exec/" not in doc["subject"]


def test_aggregate_vector_has_one_member_execution_reference_per_member_no_other_rel() -> None:
    doc = _load(_AGGREGATE)
    rels = [r["rel"] for r in doc["references"]]
    assert rels == ["member-execution", "member-execution", "member-execution"]


def test_aggregate_vector_member_references_resolve_to_the_parent_and_child_vectors_by_hash() -> None:
    """The generator gave the aggregate ``[parent_output, child_output, grandchild_output]`` in
    that order -- each reference's ``digest`` must recompute to the
    corresponding committed member vector's JCS canonical hash.

    The digest has to be in ``digest``: that is the field §3.1.2 defines as
    binding the reference to specific bytes, and the field a verifier that
    content-binds references reads. An entry carrying it as ``id`` and no
    ``digest`` still validates, because ``digest`` is optional and ``rel`` is
    open -- so this is the only place the difference is caught.
    """
    aggregate = _load(_AGGREGATE)
    from bernstein.core.security.agent_card_signer import canonicalize_jcs

    parent_doc = _load(_PARENT)
    child_doc = _load(_CHILD)
    grandchild_doc = _load(_GRANDCHILD)

    parent_digest = f"sha256:{hashlib.sha256(canonicalize_jcs(parent_doc)).hexdigest()}"
    child_digest = f"sha256:{hashlib.sha256(canonicalize_jcs(child_doc)).hexdigest()}"
    grandchild_digest = f"sha256:{hashlib.sha256(canonicalize_jcs(grandchild_doc)).hexdigest()}"

    assert [r["digest"] for r in aggregate["references"]] == [parent_digest, child_digest, grandchild_digest]
    assert [r["id"] for r in aggregate["references"]] == [
        _load(_PARENT)["subject"],
        _load(_CHILD)["subject"],
        _load(_GRANDCHILD)["subject"],
    ]
    for entry in aggregate["references"]:
        assert entry["resolver"]


def test_regenerating_the_vectors_is_byte_identical_to_the_committed_files() -> None:
    """Running the generator twice (in-process) must reproduce the exact
    committed bytes -- the acceptance criterion for issues #4760-#4762."""
    import importlib.util
    import tempfile

    spec = importlib.util.spec_from_file_location(
        "_build_trust_record_vectors_under_test",
        _VECTORS / "_build_trust_record_vectors.py",
    )
    assert spec is not None and spec.loader is not None
    build_module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(build_module)  # module-level constants only; main() is not auto-invoked

    with tempfile.TemporaryDirectory() as tmp:
        tmp_out = Path(tmp)
        # A real module attribute, not a plain dict entry: main()'s global
        # lookup of OUT_DIR resolves against this same module's __dict__,
        # so overwriting the attribute redirects it away from the committed
        # fixtures rather than overwriting them in place.
        build_module.OUT_DIR = tmp_out
        build_module.main()
        first = {p.name: p.read_bytes() for p in tmp_out.iterdir()}
        for p in tmp_out.iterdir():
            p.unlink()
        build_module.main()
        second = {p.name: p.read_bytes() for p in tmp_out.iterdir()}

        assert first == second
        for name in (
            "single-execution-trust-record.json",
            "delegated-parent-trust-record.json",
            "delegated-child-trust-record.json",
            "delegated-grandchild-trust-record.json",
            "aggregate-trust-record.json",
        ):
            committed = (_VECTORS / name).read_bytes()
            assert first[name] == committed, f"{name} has drifted from the committed vector -- re-mint required"


def test_chain_depth_at_least_two_hops() -> None:
    """The committed delegation chain must be at least two hops deep when
    walking delegation.parent_record_hash from the deepest record back
    to a root. The grandchild adds a second hop over the original
    parent->child pair (issue #4782).
    """
    grandchild = _load(_GRANDCHILD)
    child = _load(_CHILD)
    parent = _load(_PARENT)

    from bernstein.core.security.agent_card_signer import canonicalize_jcs

    # Compute each record's content hash from its JCS canonical form
    child_hash = f"sha256:{hashlib.sha256(canonicalize_jcs(child)).hexdigest()}"
    parent_hash = f"sha256:{hashlib.sha256(canonicalize_jcs(parent)).hexdigest()}"

    # Walk the chain: grandchild -> child -> parent
    hop_count = 0

    # Hop 1: grandchild's parent_record_hash must point to child's content hash
    grandchild_parent_hash = grandchild["delegation"]["parent_record_hash"]
    assert grandchild_parent_hash == child_hash, (
        f"Grandchild's parent_record_hash {grandchild_parent_hash[-20:]}... "
        f"does not match child's content hash {child_hash[-20:]}..."
    )
    hop_count += 1

    # Hop 2: child's parent_record_hash must point to parent's content hash
    child_parent_hash = child["delegation"]["parent_record_hash"]
    assert child_parent_hash == parent_hash, (
        f"Child's parent_record_hash {child_parent_hash[-20:]}... "
        f"does not match parent's content hash {parent_hash[-20:]}..."
    )
    hop_count += 1

    # Parent is the root of the chain (no delegation field)
    assert "delegation" not in parent, "Parent should not have delegation field"

    assert hop_count >= 2, f"Expected at least 2 hops in delegation chain, got {hop_count}"


def test_data_class_narrowing_exists() -> None:
    """At least one parent/child pair must have a strictly narrower data_class
    on the child (e.g., 'internal' -> 'restricted'). The test loads all
    delegated records and checks each pair for this narrowing pattern.

    Semantics: 'public' is broadest, 'internal' is less broad, 'restricted'
    is narrowest -- a narrow child data_class is a subset of the parent's
    authority. The failure message names both the broader parent data_class
    and the narrower child data_class actually present in the vectors.
    """
    parent = _load(_PARENT)
    child = _load(_CHILD)
    grandchild = _load(_GRANDCHILD)

    pairs = [
        ("parent-child", parent, child),
        ("child-grandchild", child, grandchild),
    ]
    rank = {"public": 3, "internal": 2, "restricted": 1}

    assert any(rank[child_rec["data_class"]] < rank[parent_rec["data_class"]] for _, parent_rec, child_rec in pairs), (
        "No parent/child pair has a strictly narrower child data_class "
        "(ranking: public > internal > restricted). Observed classes: "
        f"parent-child {parent['data_class']} -> {child['data_class']}, "
        f"child-grandchild {child['data_class']} -> {grandchild['data_class']}"
    )
