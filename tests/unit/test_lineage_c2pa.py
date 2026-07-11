"""Unit tests for C2PA content-credential projection (#2303).

The credential is a *projection* of the lineage spine: its assertions
are populated from spine entries, it is signed with the install-identity
Ed25519 key, and it is byte-identical across replays. Stripping the
spine must make the manifest unproducible, not merely unsigned.
"""

from __future__ import annotations

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.core.lineage.c2pa import (
    C2PA_CLAIM_GENERATOR,
    C2PA_SPEC_VERSION,
    LABEL_ACTIONS,
    LABEL_HARD_BINDING,
    LABEL_SOFT_BINDING,
    ManifestError,
    ManifestIdentity,
    SoftBinding,
    canonical_manifest_bytes,
    manifest_from_dict,
    manifest_to_dict,
    project_manifest,
    sign_manifest,
    verify_manifest,
)
from bernstein.core.lineage.spine import LineageSpine

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

_ARTIFACT_PATH = "out/report.md"
_ARTIFACT_CONTENT = b"# hello world\n"


def _identity() -> ManifestIdentity:
    return ManifestIdentity(
        install_rev="abc1234567890def",
        keyid="0" * 64,
        run_id="run-1",
    )


def _make_spine(tmp_path, run_id: str = "run-1") -> LineageSpine:
    spine = LineageSpine(tmp_path / ".sdd" / "lineage", run_id=run_id, hmac_key=b"k" * 32)
    spine.record(
        artifact_path=_ARTIFACT_PATH,
        content=_ARTIFACT_CONTENT,
        actor="agent-a",
        step_id="step-1",
        model="anthropic:claude",
        timestamp=1000,
    )
    return spine


def _entries_for(spine: LineageSpine, artifact_path: str) -> list:
    return [e for e in spine.iter_entries() if e.artifact_path == artifact_path]


def _signing_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(b"s" * 32)


# ---------------------------------------------------------------------------
# AC1: assertions populated from spine entries
# ---------------------------------------------------------------------------


def test_manifest_assertions_projected_from_spine(tmp_path) -> None:
    """AC1: the manifest's assertions are populated from lineage entries."""
    spine = _make_spine(tmp_path)
    entries = _entries_for(spine, _ARTIFACT_PATH)

    manifest = project_manifest(
        artifact_path=_ARTIFACT_PATH,
        entries=entries,
        identity=_identity(),
    )

    assert manifest.spec_version == C2PA_SPEC_VERSION
    assert manifest.claim_generator == C2PA_CLAIM_GENERATOR
    labels = [a["label"] for a in manifest.assertions]
    assert LABEL_ACTIONS in labels
    assert LABEL_HARD_BINDING in labels

    # The hard binding carries the entry's content hash verbatim.
    hard = next(a for a in manifest.assertions if a["label"] == LABEL_HARD_BINDING)
    assert hard["data"]["hash"] == entries[0].content_hash

    # The AI actions assertion references the producing model + actor.
    actions = next(a for a in manifest.assertions if a["label"] == LABEL_ACTIONS)
    action = actions["data"]["actions"][0]
    assert action["softwareAgent"] == "anthropic:claude"
    assert action["digitalSourceType"].endswith("trainedAlgorithmicMedia")


def test_manifest_carries_lineage_entry_hash(tmp_path) -> None:
    """The manifest pins the spine entry hash so it links back to the chain."""
    spine = _make_spine(tmp_path)
    entries = _entries_for(spine, _ARTIFACT_PATH)
    manifest = project_manifest(
        artifact_path=_ARTIFACT_PATH,
        entries=entries,
        identity=_identity(),
    )
    assert manifest.lineage_entry_hash == entries[-1].entry_hash


# ---------------------------------------------------------------------------
# AC2: determinism - two projections are byte-identical
# ---------------------------------------------------------------------------


def test_two_projections_byte_identical(tmp_path) -> None:
    """AC2: two replays produce byte-identical manifests, assertion order included."""
    spine_a = _make_spine(tmp_path / "a")
    spine_b = _make_spine(tmp_path / "b")

    m_a = project_manifest(
        artifact_path=_ARTIFACT_PATH,
        entries=_entries_for(spine_a, _ARTIFACT_PATH),
        identity=_identity(),
    )
    m_b = project_manifest(
        artifact_path=_ARTIFACT_PATH,
        entries=_entries_for(spine_b, _ARTIFACT_PATH),
        identity=_identity(),
    )

    assert canonical_manifest_bytes(m_a) == canonical_manifest_bytes(m_b)
    assert [a["label"] for a in m_a.assertions] == [a["label"] for a in m_b.assertions]


def test_signature_deterministic_across_replays(tmp_path) -> None:
    """AC2: Ed25519 is deterministic - signing twice yields identical bytes."""
    spine = _make_spine(tmp_path)
    entries = _entries_for(spine, _ARTIFACT_PATH)
    key = _signing_key()

    s1 = sign_manifest(
        project_manifest(artifact_path=_ARTIFACT_PATH, entries=entries, identity=_identity()),
        signing_key=key,
    )
    s2 = sign_manifest(
        project_manifest(artifact_path=_ARTIFACT_PATH, entries=entries, identity=_identity()),
        signing_key=key,
    )
    assert s1.signature_b64 == s2.signature_b64
    assert canonical_manifest_bytes(s1) == canonical_manifest_bytes(s2)


# ---------------------------------------------------------------------------
# AC3: verify checks hard binding + signature
# ---------------------------------------------------------------------------


def test_verify_ok_for_signed_manifest_and_matching_content(tmp_path) -> None:
    """AC3: verify confirms hash binding matches artifact and signature chains."""
    spine = _make_spine(tmp_path)
    entries = _entries_for(spine, _ARTIFACT_PATH)
    key = _signing_key()
    signed = sign_manifest(
        project_manifest(artifact_path=_ARTIFACT_PATH, entries=entries, identity=_identity()),
        signing_key=key,
    )

    result = verify_manifest(signed, _ARTIFACT_CONTENT, key.public_key())
    assert result.ok, result.errors


def test_verify_fails_on_content_mismatch(tmp_path) -> None:
    """AC3: a different artifact fails the hard-binding check."""
    spine = _make_spine(tmp_path)
    entries = _entries_for(spine, _ARTIFACT_PATH)
    key = _signing_key()
    signed = sign_manifest(
        project_manifest(artifact_path=_ARTIFACT_PATH, entries=entries, identity=_identity()),
        signing_key=key,
    )

    result = verify_manifest(signed, b"tampered", key.public_key())
    assert not result.ok
    assert any("hard binding" in e for e in result.errors)


def test_verify_fails_on_wrong_key(tmp_path) -> None:
    """AC3/AC5: a signature from a different key does not verify."""
    spine = _make_spine(tmp_path)
    entries = _entries_for(spine, _ARTIFACT_PATH)
    signed = sign_manifest(
        project_manifest(artifact_path=_ARTIFACT_PATH, entries=entries, identity=_identity()),
        signing_key=_signing_key(),
    )
    other = Ed25519PrivateKey.from_private_bytes(b"x" * 32)
    result = verify_manifest(signed, _ARTIFACT_CONTENT, other.public_key())
    assert not result.ok
    assert any("signature" in e for e in result.errors)


def test_verify_fails_on_tampered_assertion(tmp_path) -> None:
    """AC3: swapping an assertion after signing breaks the signature."""
    spine = _make_spine(tmp_path)
    entries = _entries_for(spine, _ARTIFACT_PATH)
    key = _signing_key()
    signed = sign_manifest(
        project_manifest(artifact_path=_ARTIFACT_PATH, entries=entries, identity=_identity()),
        signing_key=key,
    )
    tampered = manifest_from_dict(manifest_to_dict(signed))
    tampered.assertions[0]["data"]["hash"] = "sha256:deadbeef"
    result = verify_manifest(tampered, _ARTIFACT_CONTENT, key.public_key())
    assert not result.ok


# ---------------------------------------------------------------------------
# AC4: stripping the spine makes the manifest unproducible
# ---------------------------------------------------------------------------


def test_empty_entries_raises_not_unsigned(tmp_path) -> None:
    """AC4: no spine entries -> the manifest cannot be produced at all."""
    with pytest.raises(ManifestError):
        project_manifest(artifact_path=_ARTIFACT_PATH, entries=[], identity=_identity())


def test_entries_for_other_artifact_do_not_produce_manifest(tmp_path) -> None:
    """AC4: the projection binds to one artifact - a foreign entry set fails."""
    spine = _make_spine(tmp_path)
    entries = _entries_for(spine, _ARTIFACT_PATH)
    with pytest.raises(ManifestError):
        # Entries exist, but none for the requested artifact path.
        project_manifest(artifact_path="out/other.md", entries=entries, identity=_identity())


# ---------------------------------------------------------------------------
# AC5: manifest signature + install identity share one attestation root
# ---------------------------------------------------------------------------


def test_manifest_embeds_install_identity(tmp_path) -> None:
    """AC5: the signed manifest carries the install identity tokens + keyid."""
    spine = _make_spine(tmp_path)
    entries = _entries_for(spine, _ARTIFACT_PATH)
    key = _signing_key()
    from bernstein.core.security.audit_dsse import keyid_from_public_key

    keyid = keyid_from_public_key(key.public_key())
    identity = ManifestIdentity(install_rev="rev", keyid=keyid, run_id="run-1")
    signed = sign_manifest(
        project_manifest(artifact_path=_ARTIFACT_PATH, entries=entries, identity=identity),
        signing_key=key,
    )
    assert signed.identity.keyid == keyid
    assert signed.identity.install_rev == "rev"
    # The same keyid that signs the manifest identifies the install.
    result = verify_manifest(signed, _ARTIFACT_CONTENT, key.public_key())
    assert result.ok


# ---------------------------------------------------------------------------
# Soft-binding: pluggable watermark/fingerprint layer
# ---------------------------------------------------------------------------


def test_soft_binding_layer_projected_when_supplied(tmp_path) -> None:
    """A supplied soft-binding layer surfaces as a soft-binding assertion."""
    spine = _make_spine(tmp_path)
    entries = _entries_for(spine, _ARTIFACT_PATH)
    soft = SoftBinding(alg="example.watermark", blocks=[{"scope": {}, "value": "wm-123"}])
    manifest = project_manifest(
        artifact_path=_ARTIFACT_PATH,
        entries=entries,
        identity=_identity(),
        soft_binding=soft,
    )
    labels = [a["label"] for a in manifest.assertions]
    assert LABEL_SOFT_BINDING in labels
    sb = next(a for a in manifest.assertions if a["label"] == LABEL_SOFT_BINDING)
    assert sb["data"]["alg"] == "example.watermark"


def test_soft_binding_absent_by_default(tmp_path) -> None:
    """No soft binding is emitted unless a layer is explicitly supplied."""
    spine = _make_spine(tmp_path)
    entries = _entries_for(spine, _ARTIFACT_PATH)
    manifest = project_manifest(
        artifact_path=_ARTIFACT_PATH,
        entries=entries,
        identity=_identity(),
    )
    labels = [a["label"] for a in manifest.assertions]
    assert LABEL_SOFT_BINDING not in labels


# ---------------------------------------------------------------------------
# Round-trip serialisation
# ---------------------------------------------------------------------------


def test_round_trip_dict(tmp_path) -> None:
    spine = _make_spine(tmp_path)
    entries = _entries_for(spine, _ARTIFACT_PATH)
    signed = sign_manifest(
        project_manifest(artifact_path=_ARTIFACT_PATH, entries=entries, identity=_identity()),
        signing_key=_signing_key(),
    )
    restored = manifest_from_dict(manifest_to_dict(signed))
    assert canonical_manifest_bytes(restored) == canonical_manifest_bytes(signed)
    assert restored.signature_b64 == signed.signature_b64
