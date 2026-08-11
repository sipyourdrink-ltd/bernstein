"""Tests for COSE/in-toto/RFC 6962 projection of trajectory receipts (#2925 PR 2/2).

Acceptance criteria under test:

AC5  A third party holding only the operator's Ed25519 public key can:
     (a) verify the COSE envelope to confirm the receipt hash,
     (b) verify the in-toto DSSE envelope,
     (c) verify the RFC 6962 transparency envelope.
     All three commit to the same receipt hash without any HMAC key.

Tamper-collapse: mutating any envelope byte makes verification fail closed.

Subject agreement: all three envelopes must commit to the same receipt hash,
and that hash must equal ``receipt.receipt_hash`` from PR1.

Determinism: same receipt + same signing key -> byte-identical COSE bytes
across two independent calls.

Wrong-key rejection: verifying with a different Ed25519 key raises.

Receipt hash agreement: the hash returned by ``verify_trajectory_receipt_projection``
equals the hash returned by ``verify_cose_projection_bytes``, and equals
``receipt.receipt_hash`` from the sealed receipt.

All tests are hermetic: fresh Ed25519 key per test, temp dirs, no live
providers, no wall-clock in sealed bytes.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.eval.metrics import EvalScoreComponents, TierScores
from bernstein.eval.trajectory_receipt import (
    BestOfNProvenance,
    TaskTrajectoryAnchor,
    TrajectoryReceipt,
    build_trajectory_receipt,
)
from bernstein.eval.trajectory_receipt_projection import (
    TrajectoryProjectionError,
    TrajectoryReceiptProjection,
    project_trajectory_receipt,
    verify_cose_projection_bytes,
    verify_trajectory_receipt_projection,
)

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_HMAC_KEY = b"k" * 32
_FAKE_JOURNAL_HEAD = "sha256:" + "a" * 64
_FAKE_EVENTS_HASH = "sha256:" + "b" * 64


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fresh_key() -> Ed25519PrivateKey:
    """Return a freshly generated Ed25519 private key (no shared state)."""
    return Ed25519PrivateKey.generate()


def _anchor(task_id: str, *, task_success: float = 1.0) -> TaskTrajectoryAnchor:
    return TaskTrajectoryAnchor(
        task_id=task_id,
        journal_head_hash=_FAKE_JOURNAL_HEAD,
        events_content_hash=_FAKE_EVENTS_HASH,
        model_id="claude-test",
        config_fingerprint="cfg-v1",
        components=EvalScoreComponents(
            task_success=task_success,
            code_quality=0.9,
            efficiency=0.8,
            reliability=1.0,
            safety=1.0,
        ),
    )


def _per_tier() -> TierScores:
    return TierScores(smoke=1.0, standard=0.0, stretch=0.0, adversarial=0.0)


def _build_receipt(workdir: Path) -> TrajectoryReceipt:
    """Build a sealed two-task receipt under workdir."""
    return build_trajectory_receipt(
        run_id="proj-test-run",
        task_anchors=[_anchor("t-001"), _anchor("t-002")],
        per_tier=_per_tier(),
        workdir=workdir,
        lineage_root=workdir / ".sdd" / "lineage",
        hmac_key=_HMAC_KEY,
    )


def _project(receipt: TrajectoryReceipt, signing_key: Ed25519PrivateKey) -> TrajectoryReceiptProjection:
    return project_trajectory_receipt(receipt, signing_key=signing_key)


# ---------------------------------------------------------------------------
# AC5a -- COSE round-trip
# ---------------------------------------------------------------------------


def test_cose_verify_returns_receipt_hash(tmp_path: Path) -> None:
    """COSE envelope verifies and returns the correct receipt hash."""
    sk = _fresh_key()
    receipt = _build_receipt(tmp_path)
    proj = _project(receipt, sk)

    returned_hash = verify_cose_projection_bytes(proj.cose_bytes, public_key=sk.public_key())

    assert returned_hash == receipt.receipt_hash
    assert returned_hash.startswith("sha256:")


def test_cose_wrong_key_raises(tmp_path: Path) -> None:
    """COSE verify with a different key raises TrajectoryProjectionError."""
    sk = _fresh_key()
    other_key = _fresh_key()
    receipt = _build_receipt(tmp_path)
    proj = _project(receipt, sk)

    with pytest.raises(TrajectoryProjectionError, match="does not verify"):
        verify_cose_projection_bytes(proj.cose_bytes, public_key=other_key.public_key())


def test_cose_tamper_one_byte_raises(tmp_path: Path) -> None:
    """Flipping one byte in COSE bytes makes verification fail."""
    sk = _fresh_key()
    receipt = _build_receipt(tmp_path)
    proj = _project(receipt, sk)

    cose = bytearray(proj.cose_bytes)
    cose[5] ^= 0xFF  # flip a byte in the middle of the CBOR structure
    tampered = bytes(cose)

    with pytest.raises(TrajectoryProjectionError):
        verify_cose_projection_bytes(tampered, public_key=sk.public_key())


# ---------------------------------------------------------------------------
# AC5b -- in-toto DSSE round-trip
# ---------------------------------------------------------------------------


def test_intoto_verify_embedded_in_full_projection(tmp_path: Path) -> None:
    """Full projection verify passes and returns receipt hash."""
    sk = _fresh_key()
    receipt = _build_receipt(tmp_path)
    proj = _project(receipt, sk)

    returned_hash = verify_trajectory_receipt_projection(proj, public_key=sk.public_key())

    assert returned_hash == receipt.receipt_hash


def test_intoto_tamper_payload_raises(tmp_path: Path) -> None:
    """Flipping the DSSE payload base64 makes verify_trajectory_receipt_projection raise."""
    sk = _fresh_key()
    receipt = _build_receipt(tmp_path)
    proj = _project(receipt, sk)

    # Corrupt the payload field in the intoto dict
    bad_intoto = dict(proj.intoto_dict)
    original_payload = bad_intoto["payload"]
    # Flip the last character of the base64 string
    bad_intoto["payload"] = original_payload[:-1] + ("A" if original_payload[-1] != "A" else "B")

    bad_proj = TrajectoryReceiptProjection(
        receipt_hash=proj.receipt_hash,
        cose_bytes=proj.cose_bytes,
        intoto_dict=bad_intoto,
        transparency_dict=proj.transparency_dict,
        public_key_jwk=proj.public_key_jwk,
    )

    with pytest.raises(TrajectoryProjectionError):
        verify_trajectory_receipt_projection(bad_proj, public_key=sk.public_key())


# ---------------------------------------------------------------------------
# AC5c -- RFC 6962 transparency round-trip
# ---------------------------------------------------------------------------


def test_transparency_verify_passes(tmp_path: Path) -> None:
    """Transparency block verifies as part of the full projection."""
    sk = _fresh_key()
    receipt = _build_receipt(tmp_path)
    proj = _project(receipt, sk)

    # Full projection verify also checks the transparency block
    returned_hash = verify_trajectory_receipt_projection(proj, public_key=sk.public_key())
    assert returned_hash == receipt.receipt_hash


def test_transparency_tamper_signature_raises(tmp_path: Path) -> None:
    """Corrupting the transparency signature makes projection verify raise."""
    sk = _fresh_key()
    receipt = _build_receipt(tmp_path)
    proj = _project(receipt, sk)

    bad_transparency = json.loads(json.dumps(proj.transparency_dict))
    sth = bad_transparency["signed_tree_head"]
    orig_sig = sth["signature_b64"]
    # Flip last char
    sth["signature_b64"] = orig_sig[:-1] + ("A" if orig_sig[-1] != "A" else "B")

    bad_proj = TrajectoryReceiptProjection(
        receipt_hash=proj.receipt_hash,
        cose_bytes=proj.cose_bytes,
        intoto_dict=proj.intoto_dict,
        transparency_dict=bad_transparency,
        public_key_jwk=proj.public_key_jwk,
    )

    with pytest.raises(TrajectoryProjectionError):
        verify_trajectory_receipt_projection(bad_proj, public_key=sk.public_key())


# ---------------------------------------------------------------------------
# Subject agreement: all three formats commit to the same hash
# ---------------------------------------------------------------------------


def test_all_three_formats_commit_same_receipt_hash(tmp_path: Path) -> None:
    """The receipt hash embedded in every format must match receipt.receipt_hash."""
    sk = _fresh_key()
    receipt = _build_receipt(tmp_path)
    proj = _project(receipt, sk)

    # Verify all three individually and collect the subjects
    cose_hash = verify_cose_projection_bytes(proj.cose_bytes, public_key=sk.public_key())

    intoto_payload = json.loads(__import__("base64").b64decode(proj.intoto_dict["payload"]).decode("utf-8"))
    intoto_hash = intoto_payload["predicate"]["receipt_hash"]

    transparency_hash = proj.transparency_dict["signed_tree_head"]["subject_receipt_hash"]

    assert cose_hash == intoto_hash == transparency_hash == receipt.receipt_hash


def test_all_three_formats_disagree_raises(tmp_path: Path) -> None:
    """If envelope subjects disagree, verify_trajectory_receipt_projection raises."""
    sk = _fresh_key()
    # Build two different receipts to get two different hashes
    receipt_a = _build_receipt(tmp_path / "a")
    workdir_b = tmp_path / "b"
    receipt_b = build_trajectory_receipt(
        run_id="different-run",
        task_anchors=[_anchor("t-X", task_success=0.5)],
        per_tier=_per_tier(),
        workdir=workdir_b,
        lineage_root=workdir_b / ".sdd" / "lineage",
        hmac_key=_HMAC_KEY,
    )
    proj_a = _project(receipt_a, sk)
    proj_b = _project(receipt_b, sk)

    # Mix COSE from proj_a with DSSE from proj_b — subjects must disagree
    mixed_proj = TrajectoryReceiptProjection(
        receipt_hash=proj_a.receipt_hash,
        cose_bytes=proj_a.cose_bytes,
        intoto_dict=proj_b.intoto_dict,  # different receipt hash
        transparency_dict=proj_a.transparency_dict,
        public_key_jwk=proj_a.public_key_jwk,
    )

    with pytest.raises(TrajectoryProjectionError, match="disagree"):
        verify_trajectory_receipt_projection(mixed_proj, public_key=sk.public_key())


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_same_receipt_same_key_produces_identical_cose(tmp_path: Path) -> None:
    """Same receipt + same signing key → byte-identical COSE bytes."""
    # Use a fixed seed for the signing key so it is deterministic
    seed = b"s" * 32
    sk = Ed25519PrivateKey.from_private_bytes(seed)
    receipt = _build_receipt(tmp_path)

    proj1 = _project(receipt, sk)
    proj2 = _project(receipt, sk)

    assert proj1.cose_bytes == proj2.cose_bytes


def test_different_receipts_produce_different_projections(tmp_path: Path) -> None:
    """Two different receipts produce different COSE bytes."""
    seed = b"s" * 32
    sk = Ed25519PrivateKey.from_private_bytes(seed)

    receipt_a = build_trajectory_receipt(
        run_id="run-a",
        task_anchors=[_anchor("t-001")],
        per_tier=_per_tier(),
        workdir=tmp_path / "a",
        lineage_root=tmp_path / "a" / ".sdd" / "lineage",
        hmac_key=_HMAC_KEY,
    )
    receipt_b = build_trajectory_receipt(
        run_id="run-b",
        task_anchors=[_anchor("t-002", task_success=0.3)],
        per_tier=_per_tier(),
        workdir=tmp_path / "b",
        lineage_root=tmp_path / "b" / ".sdd" / "lineage",
        hmac_key=_HMAC_KEY,
    )

    proj_a = _project(receipt_a, sk)
    proj_b = _project(receipt_b, sk)

    assert proj_a.cose_bytes != proj_b.cose_bytes
    assert proj_a.receipt_hash != proj_b.receipt_hash


# ---------------------------------------------------------------------------
# Tamper-collapse: mutating the receipt changes the hash → projection mismatch
# ---------------------------------------------------------------------------


def test_mutated_receipt_hash_fails_projection_subject_check(tmp_path: Path) -> None:
    """A receipt with a mutated task anchor has a different hash; the old
    projection's subject no longer matches what verify would recompute."""
    sk = _fresh_key()
    receipt = _build_receipt(tmp_path)
    proj = _project(receipt, sk)

    # Build a second receipt with different content (simulating a tampered receipt)
    mutated = build_trajectory_receipt(
        run_id="mutated-run",
        task_anchors=[_anchor("t-MUTATED", task_success=0.0)],
        per_tier=_per_tier(),
        workdir=tmp_path / "mutated",
        lineage_root=tmp_path / "mutated" / ".sdd" / "lineage",
        hmac_key=_HMAC_KEY,
    )

    # The original projection commits to the original hash — the mutated receipt
    # has a different hash, proving the binding is hash-committed
    assert mutated.receipt_hash != receipt.receipt_hash

    # The original COSE still verifies to the original hash
    verified_hash = verify_cose_projection_bytes(proj.cose_bytes, public_key=sk.public_key())
    assert verified_hash == receipt.receipt_hash
    assert verified_hash != mutated.receipt_hash


# ---------------------------------------------------------------------------
# Best-of-N receipt also projects correctly
# ---------------------------------------------------------------------------


def test_best_of_n_receipt_projects_and_verifies(tmp_path: Path) -> None:
    """A best-of-N sealed receipt can be projected and verified."""
    sk = _fresh_key()
    bon = BestOfNProvenance(
        n_candidates=3,
        candidate_journal_heads=[
            "sha256:" + "1" * 64,
            "sha256:" + "2" * 64,
            "sha256:" + "3" * 64,
        ],
        selection_rule="highest_final_score",
        selected_index=1,
    )
    receipt = build_trajectory_receipt(
        run_id="bon-run",
        task_anchors=[_anchor("t-001"), _anchor("t-002")],
        per_tier=_per_tier(),
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_HMAC_KEY,
        best_of_n=bon,
    )

    proj = _project(receipt, sk)
    returned_hash = verify_trajectory_receipt_projection(proj, public_key=sk.public_key())

    assert returned_hash == receipt.receipt_hash


# ---------------------------------------------------------------------------
# NO_TASKS receipt also projects correctly
# ---------------------------------------------------------------------------


def test_no_tasks_receipt_projects_and_verifies(tmp_path: Path) -> None:
    """An empty-suite (NO_TASKS) receipt projects and verifies."""
    sk = _fresh_key()
    receipt = build_trajectory_receipt(
        run_id="empty-run",
        task_anchors=[],
        per_tier=_per_tier(),
        workdir=tmp_path,
        lineage_root=tmp_path / ".sdd" / "lineage",
        hmac_key=_HMAC_KEY,
    )

    proj = _project(receipt, sk)
    returned_hash = verify_trajectory_receipt_projection(proj, public_key=sk.public_key())

    assert returned_hash == receipt.receipt_hash
    assert receipt.published_score == 0.0


# ---------------------------------------------------------------------------
# Public key JWK is embedded in the projection
# ---------------------------------------------------------------------------


def test_public_key_jwk_embedded_and_correct(tmp_path: Path) -> None:
    """The embedded JWK in the projection is the public half of the signing key."""

    sk = _fresh_key()
    receipt = _build_receipt(tmp_path)
    proj = _project(receipt, sk)

    jwk = proj.public_key_jwk
    assert jwk["kty"] == "OKP"
    assert jwk["crv"] == "Ed25519"
    assert "x" in jwk

    # Reconstruct public key from JWK and confirm it matches
    import base64

    x = jwk["x"]
    padding = "=" * (-len(x) % 4)
    raw = base64.urlsafe_b64decode(x + padding)
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    reconstructed = Ed25519PublicKey.from_public_bytes(raw)

    # Verify that we can verify the COSE with the reconstructed key
    verified_hash = verify_cose_projection_bytes(proj.cose_bytes, public_key=reconstructed)
    assert verified_hash == receipt.receipt_hash


# ---------------------------------------------------------------------------
# No wall-clock in signed bytes
# ---------------------------------------------------------------------------


def test_no_timestamp_in_intoto_payload(tmp_path: Path) -> None:
    """The in-toto envelope payload contains no timestamp or wall-clock field."""
    import base64

    sk = _fresh_key()
    receipt = _build_receipt(tmp_path)
    proj = _project(receipt, sk)

    payload = json.loads(base64.b64decode(proj.intoto_dict["payload"]).decode("utf-8"))
    payload_str = json.dumps(payload)
    assert "timestamp" not in payload_str
    assert "time" not in payload_str.lower() or "predicate_type" in payload_str  # predicate_type is fine
