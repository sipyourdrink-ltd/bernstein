"""Unit tests for the conduct fold and signed conduct artifact (#5476 slice 1).

Mirrors :mod:`tests.unit.test_scorecard_artifact` and
:mod:`tests.unit.core.replay.test_scorecard_serialization`: each test pins one
acceptance criterion.

Acceptance criteria under test:

1. Fold of a fixed journal is byte-identical across two independent builds.
2. The artifact verifies offline with ``verify_events``.
3. A period with no rows reports ``UNVERIFIED``, not clean.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bernstein.core.quality.absence_coverage import CompletionCoverageStatus
from bernstein.core.replay.conduct_artifact import (
    CONDUCT_PAYLOAD_TYPE,
    ConductDocument,
    build_conduct,
    verify_conduct,
)
from bernstein.core.replay.conduct_fold import (
    CONDUCT_FOLD_TYPE,
    derive_conduct,
    derive_conduct_from_path,
)
from bernstein.core.replay.journal import EventJournal
from bernstein.core.security.lineage_kms import FileBasedKMSAdapter

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.replay.journal import JournalLoadResult

_RUN_ID = "conduct-fixture"
_PRINCIPAL = "backend-1"
_SIGN_SEED = b"c" * 32
_OTHER_SEED = b"o" * 32

_T0 = 1000.0
_T1 = 1100.0
_T2 = 1200.0
_T3 = 1300.0
_PERIOD_START = 1050.0
_PERIOD_END = 1250.0


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _seed_run(sdd_dir: Path, run_id: str = _RUN_ID) -> None:
    """Populate a hermetic run journal with events for two principals.

    Rows carry a ``ts`` inside or outside the test period and a
    ``principal_id`` naming one of two agents, so the fold's two filters
    (window and principal) are exercised independently.
    """
    journal = EventJournal(run_id=run_id, sdd_dir=sdd_dir)
    journal.record(
        "run_started",
        run_id=run_id,
        principal_id=_PRINCIPAL,
        ts=_T0,
    )
    journal.record(
        "tool_call",
        principal_id=_PRINCIPAL,
        ts=_T1,
    )
    journal.record(
        "task_completed",
        task_id="T-1",
        principal_id=_PRINCIPAL,
        ts=_T2,
    )
    journal.record(
        "task_completed",
        task_id="T-2",
        principal_id="other-agent",
        ts=_T2,
    )
    journal.record(
        "approval_gate",
        principal_id=_PRINCIPAL,
        ts=_T3,
    )


def _row(index: int, event: str, principal: str, ts: float, **extra: object) -> dict[str, object]:
    """One in-memory journal row shaped like a load_events row."""
    row: dict[str, object] = {"event": event, "principal_id": principal, "ts": ts, "index": index}
    row.update(extra)
    return row


def _sample_rows() -> list[dict[str, object]]:
    """A fixed journal row list covering every folded event class."""
    return [
        _row(0, "tool_call", _PRINCIPAL, _T1),
        _row(1, "task_completed", _PRINCIPAL, _T2, task_id="T-1"),
        _row(2, "task_verification_failed", _PRINCIPAL, _T2, task_id="T-2"),
        _row(3, "approval_gate", _PRINCIPAL, _T2),
        _row(4, "approval_honoured", _PRINCIPAL, _T2),
        _row(5, "approval_overridden", _PRINCIPAL, _T2),
        _row(6, "task_delegated", _PRINCIPAL, _T2, to="other-agent"),
        _row(7, "task_completed", "other-agent", _T2, task_id="T-9"),
        _row(8, "tool_call", _PRINCIPAL, _T3),  # outside the window
    ]


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


def _kms(tmp_path: Path, seed: bytes = _SIGN_SEED) -> FileBasedKMSAdapter:
    key_path = tmp_path / f"sign-{seed[:1].hex()}.pem"
    _write_key(key_path, seed)
    return FileBasedKMSAdapter(key_path, kid="test-conduct-key")


def _public_pem(seed: bytes) -> bytes:
    key = Ed25519PrivateKey.from_private_bytes(seed)
    return key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def _document() -> ConductDocument:
    return ConductDocument(
        run_id=_RUN_ID,
        principal_id=_PRINCIPAL,
        period_start=_PERIOD_START,
        period_end=_PERIOD_END,
        document_version="1.0.0",
        conduct={"coverage": "verified", "event_count": 1},
    )


def _reserialize(doc: dict[str, object]) -> bytes:
    """Re-encode a (possibly mutated) artifact dict."""
    return json.dumps(doc).encode("utf-8") + b"\n"


# ---------------------------------------------------------------------------
# Criterion 1: fold of a fixed journal is byte-identical across builds
# ---------------------------------------------------------------------------


class TestFoldDeterminism:
    """Pin the pure-projection contract for the conduct fold."""

    def test_two_independent_folds_byte_identical(self) -> None:
        first = derive_conduct(
            _sample_rows(),
            principal_id=_PRINCIPAL,
            period_start=_PERIOD_START,
            period_end=_PERIOD_END,
            run_id=_RUN_ID,
        )
        second = derive_conduct(
            _sample_rows(),
            principal_id=_PRINCIPAL,
            period_start=_PERIOD_START,
            period_end=_PERIOD_END,
            run_id=_RUN_ID,
        )
        assert first.canonical_bytes() == second.canonical_bytes()

    def test_counts_carry_event_index_ranges(self) -> None:
        fold = derive_conduct(
            _sample_rows(),
            principal_id=_PRINCIPAL,
            period_start=_PERIOD_START,
            period_end=_PERIOD_END,
        )
        assert fold.event_count == 7
        assert fold.tool_calls.count == 1
        assert fold.tool_calls.first_index == 0
        assert fold.tool_calls.last_index == 0
        assert fold.completed.count == 1
        assert fold.completed.first_index == 1
        assert fold.failed.count == 1
        assert fold.failed.first_index == 2
        assert fold.gated.count == 1
        assert fold.gated.first_index == 3
        assert fold.gated_honoured.count == 1
        assert fold.gated_honoured.first_index == 4
        assert fold.gated_overridden.count == 1
        assert fold.gated_overridden.first_index == 5
        assert fold.delegated.count == 1
        assert fold.delegated.first_index == 6
        # Other-agent rows and the out-of-window row are never cited.
        assert fold.completed.last_index == 1
        assert fold.tool_calls.last_index == 0

    def test_out_of_window_rows_excluded(self) -> None:
        fold = derive_conduct(
            _sample_rows(),
            principal_id=_PRINCIPAL,
            period_start=_PERIOD_END,  # empty window
            period_end=_PERIOD_END,
        )
        assert fold.event_count == 0
        assert fold.coverage == CompletionCoverageStatus.UNVERIFIED

    def test_other_principal_rows_excluded(self) -> None:
        fold = derive_conduct(
            [_row(0, "task_completed", "other-agent", _T2, task_id="T-9")],
            principal_id=_PRINCIPAL,
            period_start=_PERIOD_START,
            period_end=_PERIOD_END,
        )
        assert fold.event_count == 0
        assert fold.coverage == CompletionCoverageStatus.UNVERIFIED

    def test_round_trip_through_dict(self) -> None:
        fold = derive_conduct(
            _sample_rows(),
            principal_id=_PRINCIPAL,
            period_start=_PERIOD_START,
            period_end=_PERIOD_END,
            run_id=_RUN_ID,
        )
        assert ConductProjection_from_dict(fold.to_dict()).canonical_bytes() == fold.canonical_bytes()

    def test_zero_count_has_null_range(self) -> None:
        fold = derive_conduct(
            [_row(0, "task_completed", _PRINCIPAL, _T2, task_id="T-1")],
            principal_id=_PRINCIPAL,
            period_start=_PERIOD_START,
            period_end=_PERIOD_END,
        )
        assert fold.gated.count == 0
        assert fold.gated.first_index is None
        assert fold.gated.last_index is None


def ConductProjection_from_dict(raw: dict[str, object]) -> object:
    """Import-time indirection so the class is imported lazily."""
    from bernstein.core.replay.conduct_fold import ConductProjection

    return ConductProjection.from_dict(raw)


# ---------------------------------------------------------------------------
# Criterion 2: artifact verifies offline
# ---------------------------------------------------------------------------


class TestConductArtifact:
    """Pin the signed-artifact contract for the conduct artifact."""

    def test_build_then_verify_offline(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        sdd = tmp_path / ".sdd"
        _seed_run(sdd)
        document = ConductDocument(
            run_id=_RUN_ID,
            principal_id=_PRINCIPAL,
            period_start=_PERIOD_START,
            period_end=_PERIOD_END,
            document_version="1.0.0",
            conduct={"coverage": "verified", "event_count": 1},
        )
        artifact = build_conduct(_RUN_ID, sdd, _kms(tmp_path), document)
        assert artifact.artifact_path is not None
        artifact_bytes = artifact.artifact_path.read_bytes()

        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        result = verify_conduct(artifact_bytes, public_key_pem=_public_pem(_SIGN_SEED))
        assert result.ok
        assert result.status == "ok"
        assert result.run_id == _RUN_ID
        assert result.principal_id == _PRINCIPAL
        assert result.journal_events == 5

    def test_two_independent_builds_byte_identical(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        _seed_run(sdd)
        document = _document()
        first = build_conduct(_RUN_ID, sdd, _kms(tmp_path), document, write=False)
        second = build_conduct(_RUN_ID, sdd, _kms(tmp_path), document, write=False)

        assert first.artifact_bytes == second.artifact_bytes
        assert verify_conduct(first.artifact_bytes, public_key_pem=_public_pem(_SIGN_SEED)).ok
        assert verify_conduct(second.artifact_bytes, public_key_pem=_public_pem(_SIGN_SEED)).ok

    def test_journal_tamper_names_divergent_step(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        _seed_run(sdd)
        artifact = build_conduct(_RUN_ID, sdd, _kms(tmp_path), _document(), write=False)
        doc = json.loads(artifact.artifact_bytes)
        doc["journal"]["events"][1]["principal_id"] = "forged-agent"
        result = verify_conduct(_reserialize(doc), public_key_pem=_public_pem(_SIGN_SEED))

        assert not result.ok
        assert result.status == "tampered"
        assert result.divergent_step == 1
        assert any("step" in err.lower() or "journal" in err.lower() for err in result.errors)

    def test_document_body_tamper_fails(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        _seed_run(sdd)
        artifact = build_conduct(_RUN_ID, sdd, _kms(tmp_path), _document(), write=False)
        doc = json.loads(artifact.artifact_bytes)
        doc["document"]["conduct"]["event_count"] = 99
        result = verify_conduct(_reserialize(doc), public_key_pem=_public_pem(_SIGN_SEED))

        assert not result.ok
        assert result.status == "tampered"
        assert any("digest" in err.lower() for err in result.errors)

    def test_wrong_signing_key_fails(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        _seed_run(sdd)
        artifact = build_conduct(_RUN_ID, sdd, _kms(tmp_path), _document(), write=False)
        result = verify_conduct(artifact.artifact_bytes, public_key_pem=_public_pem(_OTHER_SEED))
        assert not result.ok
        assert result.status == "tampered"

    def test_generated_at_excluded_from_binding(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        _seed_run(sdd)
        document = ConductDocument(
            run_id=_RUN_ID,
            principal_id=_PRINCIPAL,
            period_start=_PERIOD_START,
            period_end=_PERIOD_END,
            document_version="1.0.0",
            conduct={"coverage": "verified", "event_count": 1},
            generated_at="2026-01-01T00:00:00Z",
        )
        artifact = build_conduct(_RUN_ID, sdd, _kms(tmp_path), document, write=False)
        doc = json.loads(artifact.artifact_bytes)
        assert "generated_at" in doc["document"]
        doc["document"]["generated_at"] = "2099-12-31T23:59:59Z"
        result = verify_conduct(_reserialize(doc), public_key_pem=_public_pem(_SIGN_SEED))
        assert result.ok

    def test_payload_type_distinct_from_scorecard(self) -> None:
        from bernstein.core.replay.scorecard_artifact import SCORECARD_PAYLOAD_TYPE

        assert CONDUCT_PAYLOAD_TYPE != SCORECARD_PAYLOAD_TYPE

    def test_scorecard_signature_cannot_be_replayed_as_conduct(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        _seed_run(sdd)
        from bernstein.core.replay.scorecard_artifact import ScorecardDocument, build_scorecard

        scorecard_doc = ScorecardDocument(
            run_id=_RUN_ID,
            document_version="1.0.0",
            scorecard={"metric": "x", "value": 1},
        )
        scorecard = build_scorecard(_RUN_ID, sdd, _kms(tmp_path), scorecard_doc, write=False)
        doc = json.loads(scorecard.artifact_bytes)
        doc["signing"]["payload_type"] = CONDUCT_PAYLOAD_TYPE
        result = verify_conduct(_reserialize(doc), public_key_pem=_public_pem(_SIGN_SEED))
        assert not result.ok
        assert result.status in {"malformed", "tampered"}

    def test_conduct_signature_cannot_be_replayed_as_scorecard(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        _seed_run(sdd)
        from bernstein.core.replay.scorecard_artifact import verify_scorecard

        artifact = build_conduct(_RUN_ID, sdd, _kms(tmp_path), _document(), write=False)
        doc = json.loads(artifact.artifact_bytes)
        from bernstein.core.replay.scorecard_artifact import SCORECARD_PAYLOAD_TYPE

        doc["signing"]["payload_type"] = SCORECARD_PAYLOAD_TYPE
        result = verify_scorecard(_reserialize(doc), public_key_pem=_public_pem(_SIGN_SEED))
        assert not result.ok
        assert result.status in {"malformed", "tampered"}


# ---------------------------------------------------------------------------
# Criterion 3: period with no rows reports UNVERIFIED, not clean
# ---------------------------------------------------------------------------


class TestCoverageClassification:
    """Pin the absence-coverage classification contract."""

    def test_empty_period_is_unverified_not_clean(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        _seed_run(sdd)
        fold = derive_conduct_from_path(
            sdd / "runs" / _RUN_ID / "journal.jsonl",
            principal_id="never-seen-agent",
            period_start=0.0,
            period_end=9999.0,
            run_id=_RUN_ID,
        )
        assert fold.coverage == CompletionCoverageStatus.UNVERIFIED
        assert fold.event_count == 0
        assert fold.tool_calls.count == 0
        assert fold.completed.count == 0

    def test_period_with_rows_is_verified(self, tmp_path: Path) -> None:
        sdd = tmp_path / ".sdd"
        _seed_run(sdd)
        fold = derive_conduct_from_path(
            sdd / "runs" / _RUN_ID / "journal.jsonl",
            principal_id=_PRINCIPAL,
            period_start=0.0,
            period_end=9999.0,
            run_id=_RUN_ID,
        )
        assert fold.coverage == CompletionCoverageStatus.VERIFIED
        assert fold.event_count == 3

    def test_empty_events_list_unverified(self) -> None:
        fold = derive_conduct(
            [],
            principal_id=_PRINCIPAL,
            period_start=_PERIOD_START,
            period_end=_PERIOD_END,
        )
        assert fold.coverage == CompletionCoverageStatus.UNVERIFIED
        assert fold.event_count == 0

    def test_missing_journal_raises(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="journal not found"):
            derive_conduct_from_path(
                tmp_path / "nonexistent" / "journal.jsonl",
                principal_id=_PRINCIPAL,
                period_start=0.0,
                period_end=1.0,
            )


# ---------------------------------------------------------------------------
# Type-checking helper: load_events typing surface
# ---------------------------------------------------------------------------


def _load_result_type_check(result: JournalLoadResult) -> None:
    """Compile-only assertion that load_events returns JournalLoadResult."""
    _ = result.events
    _ = result.discarded_line_indices


def test_conduct_type_url_versioned() -> None:
    assert CONDUCT_FOLD_TYPE == "https://bernstein.run/attestations/conduct/v1"
