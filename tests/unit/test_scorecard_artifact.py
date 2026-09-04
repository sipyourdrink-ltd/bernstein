"""Unit tests for the signed scorecard artifact (issue #5403).

Mirrors the signing and binding conventions of :mod:`bernstein.core.replay.run_receipt`.
Each test pins one of the 8 acceptance criteria from issue #5403.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TYPE_CHECKING

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.core.lineage.spine import LineageSpine
from bernstein.core.replay.journal import EventJournal
from bernstein.core.replay.run_receipt import (
    RUN_RECEIPT_PAYLOAD_TYPE,
    build_run_receipt,
    verify_run_receipt,
)
from bernstein.core.replay.scorecard import (
    SCORECARD_PAYLOAD_TYPE,
    ScorecardDocument,
    _project_document_body,
    build_scorecard,
    verify_scorecard,
)
from bernstein.core.security.lineage_kms import FileBasedKMSAdapter

if TYPE_CHECKING:
    from bernstein.core.security.lineage_kms import KMSAdapter

_RUN_ID = "scorecard-fixture"
_HMAC_KEY = b"x" * 32
_SIGN_SEED = b"i" * 32
_OTHER_SEED = b"o" * 32


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _seed_run(sdd_dir: Path, run_id: str = _RUN_ID) -> None:
    """Populate a hermetic run: 3 journal events + 2 spine entries."""
    journal = EventJournal(run_id=run_id, sdd_dir=sdd_dir)
    journal.record("run_started", run_id=run_id)
    journal.record("task_claimed", task_id="T-1", role="backend")
    journal.record("run_completed", run_id=run_id, ticks=7)
    spine = LineageSpine(sdd_dir / "lineage", run_id=run_id, hmac_key=_HMAC_KEY)
    spine.record(
        artifact_path="src/app.py",
        content=b"print('hi')\n",
        actor="backend",
        step_id="T-1",
        model="m1",
        timestamp=1111,
    )
    spine.record(
        artifact_path="tests/test_app.py",
        content=b"assert True\n",
        actor="qa",
        step_id="T-2",
        model="m1",
        timestamp=2222,
    )


def _write_key(path: Path, seed: bytes) -> Ed25519PrivateKey:
    key = Ed25519PrivateKey.from_private_bytes(seed)
    path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        ),
    )
    return key


def _kms(tmp_path: Path, seed: bytes = _SIGN_SEED) -> KMSAdapter:
    key_path = tmp_path / f"sign-{seed[:1].hex()}.pem"
    _write_key(key_path, seed)
    return FileBasedKMSAdapter(key_path, kid="test-scorecard-key")


def _public_pem(seed: bytes) -> bytes:
    key = Ed25519PrivateKey.from_private_bytes(seed)
    return key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _reserialize(doc: dict[str, object]) -> bytes:
    """Re-encode a (possibly mutated) scorecard dict."""
    return json.dumps(doc).encode("utf-8") + b"\n"


# ---------------------------------------------------------------------------
# Criterion 1: round-trip — build then re-load; reloaded document equals original
# ---------------------------------------------------------------------------


def test_scorecard_round_trip(tmp_path: Path) -> None:
    """build_scorecard then re-parse the written file produces byte-identical artifact.

    Verifies the artifact bytes are well-formed JSON, the document body
    re-derives identically, and no hidden non-determinism crept into the build.
    """
    sdd = tmp_path / ".sdd"
    _seed_run(sdd)

    document = ScorecardDocument(
        run_id=_RUN_ID,
        document_version="1.0.0",
        scorecard={"metric": "coverage", "value": 0.87},
        generated_at="2026-01-01T00:00:00Z",
    )
    artifact = build_scorecard(_RUN_ID, sdd, _kms(tmp_path), document)

    assert artifact.artifact_path is not None
    raw = artifact.artifact_path.read_bytes()
    reloaded = json.loads(raw.decode("utf-8"))

    assert reloaded["run_id"] == _RUN_ID
    assert reloaded["document"]["run_id"] == _RUN_ID
    assert reloaded["document"]["scorecard"] == {"metric": "coverage", "value": 0.87}
    assert reloaded["document"]["generated_at"] == "2026-01-01T00:00:00Z"
    assert reloaded["signing"]["payload_type"] == SCORECARD_PAYLOAD_TYPE

    # The reloaded bytes must match what build returned (byte-determinism).
    assert raw == artifact.artifact_bytes


# ---------------------------------------------------------------------------
# Criterion 2: verifies offline — no .sdd/ access during verify
# ---------------------------------------------------------------------------


def test_scorecard_verifies_offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """verify_scorecard succeeds from its bytes alone; no .sdd/ accessed.

    Verification runs from an unrelated empty cwd so any hidden dependence
    on the live run directory would surface as a failure.
    """
    sdd = tmp_path / "proj" / ".sdd"
    _seed_run(sdd)

    document = ScorecardDocument(
        run_id=_RUN_ID,
        document_version="1.0.0",
        scorecard={"metric": "test-pass-rate", "value": 1.0},
    )
    artifact = build_scorecard(_RUN_ID, sdd, _kms(tmp_path), document)
    assert artifact.artifact_path is not None
    artifact_bytes = artifact.artifact_path.read_bytes()

    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)

    result = verify_scorecard(artifact_bytes)
    assert result.ok
    assert result.status == "ok"
    assert result.run_id == _RUN_ID
    assert result.journal_events == 3


# ---------------------------------------------------------------------------
# Criterion 3: journal tamper — divergent_step names the broken row
# ---------------------------------------------------------------------------


def test_journal_tamper_names_divergent_step(tmp_path: Path) -> None:
    """Mutating one embedded journal row reports tampered at its 0-based index.

    The verifier recomputes the chain from the embedded rows and stops at
    the first divergence; the returned divergent_step MUST equal the index of
    the mutated row, and the error message MUST name the step.
    """
    sdd = tmp_path / ".sdd"
    _seed_run(sdd)

    document = ScorecardDocument(
        run_id=_RUN_ID,
        document_version="1.0.0",
        scorecard={"metric": "lints", "value": True},
    )
    artifact = build_scorecard(_RUN_ID, sdd, _kms(tmp_path), document, write=False)

    doc = json.loads(artifact.artifact_bytes)
    doc["journal"]["events"][1]["task_id"] = "T-FORGED"
    result = verify_scorecard(_reserialize(doc))

    assert not result.ok
    assert result.status == "tampered"
    assert result.divergent_step == 1
    assert any("step" in err.lower() or "journal" in err.lower() for err in result.errors)


# ---------------------------------------------------------------------------
# Criterion 4: document body tamper — document digest mismatch named
# ---------------------------------------------------------------------------


def test_document_body_tamper_fails(tmp_path: Path) -> None:
    """Mutating the embedded document body fails verification with digest mismatch.

    The verifier recomputes the document digest from the projected body (with
    wall-clock fields excluded) and compares it to the bound subject value;
    any mutation of the document body is detected.
    """
    sdd = tmp_path / ".sdd"
    _seed_run(sdd)

    document = ScorecardDocument(
        run_id=_RUN_ID,
        document_version="1.0.0",
        scorecard={"metric": "coverage", "value": 0.99},
    )
    artifact = build_scorecard(_RUN_ID, sdd, _kms(tmp_path), document, write=False)

    doc = json.loads(artifact.artifact_bytes)
    doc["document"]["scorecard"]["value"] = 0.10  # mutated
    result = verify_scorecard(_reserialize(doc))

    assert not result.ok
    assert result.status == "tampered"
    assert any("digest" in err.lower() for err in result.errors)


# ---------------------------------------------------------------------------
# Criterion 5: deterministic build — two independent builds are byte-identical
# ---------------------------------------------------------------------------


def test_two_independent_builds_byte_identical(tmp_path: Path) -> None:
    """Two calls to build_scorecard with identical inputs produce byte-equal artifact bytes.

    Pins RFC 8032 deterministic Ed25519 + canonical JSON. If the signature
    ever uses non-deterministic Ed25519 or JSON key-order varies, this test fails.
    """
    sdd = tmp_path / ".sdd"
    _seed_run(sdd)

    document = ScorecardDocument(
        run_id=_RUN_ID,
        document_version="1.0.0",
        scorecard={"metric": "determinism", "checks": ["a", "b"]},
        generated_at="2026-06-15T12:00:00Z",
    )
    first = build_scorecard(_RUN_ID, sdd, _kms(tmp_path), document, write=False)
    second = build_scorecard(_RUN_ID, sdd, _kms(tmp_path), document, write=False)

    assert first.artifact_bytes == second.artifact_bytes
    # Also verify both still verify cleanly.
    assert verify_scorecard(first.artifact_bytes).ok
    assert verify_scorecard(second.artifact_bytes).ok


# ---------------------------------------------------------------------------
# Criterion 6: wall-clock fields excluded from hashed binding subject
# ---------------------------------------------------------------------------


def test_wall_clock_fields_excluded_from_hashed_subject(tmp_path: Path) -> None:
    """generated_at is present in artifact.document but absent from the hashed binding subject.

    The scorecard is built with a wall-clock field; verification must still pass
    when the same scorecard is re-built (wall-clock would differ) only if the
    field is not part of the signed subject. We test this by:
    (a) verifying the field IS present in artifact.document,
    (b) mutating the field in the embedded document and checking that verification
        still passes — proving the signature was computed over the projected body.
    """
    sdd = tmp_path / ".sdd"
    _seed_run(sdd)

    document = ScorecardDocument(
        run_id=_RUN_ID,
        document_version="1.0.0",
        scorecard={"metric": "wallclock", "value": 1},
        generated_at="2026-01-01T00:00:00Z",
    )
    artifact = build_scorecard(_RUN_ID, sdd, _kms(tmp_path), document, write=False)
    doc = json.loads(artifact.artifact_bytes)

    # (a) generated_at is in the document body.
    assert "generated_at" in doc["document"]
    assert doc["document"]["generated_at"] == "2026-01-01T00:00:00Z"

    # (b) Mutating the wall-clock field does NOT break verification.
    doc["document"]["generated_at"] = "2099-12-31T23:59:59Z"
    result = verify_scorecard(_reserialize(doc))
    assert result.ok, f"wall-clock mutation should not affect verification: {result.errors}"

    # Also directly confirm _project_document_body strips the field.
    projected = _project_document_body(doc["document"])
    assert "generated_at" not in projected


# ---------------------------------------------------------------------------
# Criterion 7: payload type distinct from run-receipt; signatures do not replay
# ---------------------------------------------------------------------------


def test_payload_type_distinct_from_run_receipt() -> None:
    """SCORECARD_PAYLOAD_TYPE != RUN_RECEIPT_PAYLOAD_TYPE."""
    assert SCORECARD_PAYLOAD_TYPE != RUN_RECEIPT_PAYLOAD_TYPE


def test_scorecard_signature_cannot_be_replayed_as_run_receipt(tmp_path: Path) -> None:
    """A scorecard artifact's signature fails verification when presented as a run-receipt.

    Build a valid scorecard, then verify its bytes (reinterpreted as a run-receipt
    payload type) with verify_run_receipt; it must be rejected.
    """
    sdd = tmp_path / ".sdd"
    _seed_run(sdd)

    document = ScorecardDocument(
        run_id=_RUN_ID,
        document_version="1.0.0",
        scorecard={"metric": "replay", "value": 0},
    )
    scorecard_artifact = build_scorecard(_RUN_ID, sdd, _kms(tmp_path), document, write=False)
    doc = json.loads(scorecard_artifact.artifact_bytes)

    # Swap the payload_type in the signing envelope to RUN_RECEIPT_PAYLOAD_TYPE.
    doc["signing"]["payload_type"] = RUN_RECEIPT_PAYLOAD_TYPE
    result = verify_run_receipt(_reserialize(doc))

    assert not result.ok, "scorecard signature replayed as run-receipt should be rejected"
    assert result.status in {"malformed", "tampered"}


def test_run_receipt_signature_cannot_be_replayed_as_scorecard(tmp_path: Path) -> None:
    """A run-receipt artifact's signature fails verification when presented as a scorecard.

    Build a valid run-receipt, then verify its bytes (reinterpreted as a scorecard
    payload type) with verify_scorecard; it must be rejected.
    """
    sdd = tmp_path / ".sdd"
    _seed_run(sdd)

    receipt = build_run_receipt(_RUN_ID, sdd, _kms(tmp_path), write=False)
    doc = json.loads(receipt.receipt_bytes)

    # Swap the payload_type in the signing envelope to SCORECARD_PAYLOAD_TYPE.
    doc["signing"]["payload_type"] = SCORECARD_PAYLOAD_TYPE
    result = verify_scorecard(_reserialize(doc))

    assert not result.ok, "run-receipt signature replayed as scorecard should be rejected"
    assert result.status in {"malformed", "tampered"}


# ---------------------------------------------------------------------------
# Criterion 8: no-regression gate — existing run-receipt roundtrip still green
# ---------------------------------------------------------------------------


def test_existing_run_receipt_roundtrip_still_green() -> None:
    """The existing run-receipt integration suite must still pass (no-regression gate).

    This test delegates to the real integration suite to catch any accidental
    coupling between scorecard and run-receipt that breaks the existing test.
    """
    import subprocess

    repo_root = Path(__file__).resolve().parents[2]

    result = subprocess.run(
        [
            "uv",
            "run",
            "pytest",
            "tests/integration/test_replay_receipt_roundtrip.py",
            "-x",
            "-q",
        ],
        capture_output=True,
        text=True,
        cwd=repo_root,
    )
    assert result.returncode == 0, f"run-receipt roundtrip regression:\n{result.stdout}\n{result.stderr}"
