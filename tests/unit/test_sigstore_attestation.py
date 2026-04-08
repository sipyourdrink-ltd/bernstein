"""Tests for Sigstore/Rekor cryptographic attestation module.

These tests exercise the attestation module without requiring network access
or the sigstore package — they mock out the sigstore path and directly test
the Ed25519 fallback and the data model / persistence layer.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from bernstein.core.sigstore_attestation import (
    AttestationConfig,
    AttestationPayload,
    AttestationRecord,
    attest_task_completion,
    list_attestations,
    load_attestation_record,
    verify_local_attestation,
)

# ---------------------------------------------------------------------------
# AttestationPayload tests
# ---------------------------------------------------------------------------


class TestAttestationPayload:
    def test_canonical_json_is_deterministic(self) -> None:
        p = AttestationPayload(
            task_id="abc",
            agent_id="claude",
            diff_sha256="d" * 64,
            event_hmac="e" * 64,
            timestamp="2026-04-08T00:00:00Z",
        )
        assert p.canonical_json() == p.canonical_json()
        data = json.loads(p.canonical_json())
        assert data["task_id"] == "abc"
        assert data["agent_id"] == "claude"

    def test_digest_is_sha256_hex(self) -> None:
        p = AttestationPayload(
            task_id="abc",
            agent_id="claude",
            diff_sha256="d" * 64,
            event_hmac="e" * 64,
            timestamp="2026-04-08T00:00:00Z",
        )
        digest = p.digest()
        assert len(digest) == 64
        assert all(c in "0123456789abcdef" for c in digest)

    def test_different_payloads_have_different_digests(self) -> None:
        base = AttestationPayload(
            task_id="abc",
            agent_id="claude",
            diff_sha256="d" * 64,
            event_hmac="e" * 64,
            timestamp="2026-04-08T00:00:00Z",
        )
        other = AttestationPayload(
            task_id="xyz",
            agent_id="claude",
            diff_sha256="d" * 64,
            event_hmac="e" * 64,
            timestamp="2026-04-08T00:00:00Z",
        )
        assert base.digest() != other.digest()


# ---------------------------------------------------------------------------
# AttestationRecord serialization
# ---------------------------------------------------------------------------


class TestAttestationRecord:
    def test_to_dict_round_trip(self) -> None:
        payload = AttestationPayload(
            task_id="t1",
            agent_id="a1",
            diff_sha256="d" * 64,
            event_hmac="e" * 64,
            timestamp="2026-04-08T00:00:00Z",
        )
        record = AttestationRecord(
            payload=payload,
            rekor_log_id="abc123",
            rekor_log_index=42,
            bundle_path="/tmp/bundle.json",
            signed_at="2026-04-08T00:00:01Z",
            fallback_used=False,
        )
        d = record.to_dict()
        assert d["rekor_log_id"] == "abc123"
        assert d["rekor_log_index"] == 42
        assert d["fallback_used"] is False
        assert d["payload"]["task_id"] == "t1"

    def test_default_values(self) -> None:
        payload = AttestationPayload(
            task_id="t1",
            agent_id="a1",
            diff_sha256="d" * 64,
            event_hmac="e" * 64,
            timestamp="2026-04-08T00:00:00Z",
        )
        record = AttestationRecord(payload=payload)
        assert record.rekor_log_id == ""
        assert record.rekor_log_index == -1
        assert record.fallback_used is False
        assert record.error == ""


# ---------------------------------------------------------------------------
# Ed25519 fallback attestation (sigstore unavailable)
# ---------------------------------------------------------------------------


class TestFallbackAttestation:
    @pytest.fixture
    def attest_dir(self, tmp_path: Path) -> Path:
        return tmp_path / "attestations"

    def test_fallback_creates_bundle(self, attest_dir: Path) -> None:
        """When sigstore is unavailable, creates a local Ed25519 bundle."""
        with patch(
            "bernstein.core.sigstore_attestation._sigstore_available",
            return_value=False,
        ):
            record = attest_task_completion(
                task_id="task-001",
                agent_id="agent-qa",
                diff_sha256="a" * 64,
                event_hmac="b" * 64,
                attestation_dir=attest_dir,
            )

        assert record.fallback_used is True
        assert record.rekor_log_id == ""
        assert record.rekor_log_index == -1
        assert record.bundle_path.endswith(".json")
        assert Path(record.bundle_path).exists()

    def test_fallback_bundle_schema(self, attest_dir: Path) -> None:
        """Ed25519 bundle has the correct schema and fields."""
        with patch(
            "bernstein.core.sigstore_attestation._sigstore_available",
            return_value=False,
        ):
            record = attest_task_completion(
                task_id="task-002",
                agent_id="agent-qa",
                diff_sha256="c" * 64,
                event_hmac="d" * 64,
                attestation_dir=attest_dir,
            )

        bundle = json.loads(Path(record.bundle_path).read_text())
        assert bundle["schema"] == "bernstein-local-attestation/v1"
        assert "signature_hex" in bundle
        assert "payload_digest" in bundle
        assert bundle["payload"]["task_id"] == "task-002"

    def test_fallback_generates_ed25519_key(self, attest_dir: Path) -> None:
        """Ed25519 signing key and public key are generated on first run."""
        with patch(
            "bernstein.core.sigstore_attestation._sigstore_available",
            return_value=False,
        ):
            attest_task_completion(
                task_id="task-003",
                agent_id="a",
                diff_sha256="d" * 64,
                event_hmac="e" * 64,
                attestation_dir=attest_dir,
            )

        assert (attest_dir / "ed25519-signing-key.pem").exists()
        assert (attest_dir / "ed25519-public-key.pem").exists()

    def test_fallback_reuses_existing_key(self, attest_dir: Path) -> None:
        """Successive attestations use the same Ed25519 key."""
        with patch(
            "bernstein.core.sigstore_attestation._sigstore_available",
            return_value=False,
        ):
            attest_task_completion(
                task_id="task-004",
                agent_id="a",
                diff_sha256="d" * 64,
                event_hmac="e" * 64,
                attestation_dir=attest_dir,
            )
            pub1 = (attest_dir / "ed25519-public-key.pem").read_bytes()

            attest_task_completion(
                task_id="task-005",
                agent_id="a",
                diff_sha256="f" * 64,
                event_hmac="g" * 64,
                attestation_dir=attest_dir,
            )
            pub2 = (attest_dir / "ed25519-public-key.pem").read_bytes()

        assert pub1 == pub2

    def test_verify_valid_attestation(self, attest_dir: Path) -> None:
        """verify_local_attestation returns True for an intact bundle."""
        with patch(
            "bernstein.core.sigstore_attestation._sigstore_available",
            return_value=False,
        ):
            record = attest_task_completion(
                task_id="task-006",
                agent_id="a",
                diff_sha256="d" * 64,
                event_hmac="e" * 64,
                attestation_dir=attest_dir,
            )

        assert verify_local_attestation(Path(record.bundle_path), attest_dir) is True

    def test_verify_tampered_attestation(self, attest_dir: Path) -> None:
        """verify_local_attestation returns False if signature is tampered."""
        with patch(
            "bernstein.core.sigstore_attestation._sigstore_available",
            return_value=False,
        ):
            record = attest_task_completion(
                task_id="task-007",
                agent_id="a",
                diff_sha256="d" * 64,
                event_hmac="e" * 64,
                attestation_dir=attest_dir,
            )

        # Tamper with the payload — use a known-safe path derived from attest_dir
        safe_bundle = attest_dir / Path(record.bundle_path).name
        assert safe_bundle.exists(), f"Bundle not found at {safe_bundle}"
        bundle = json.loads(safe_bundle.read_text())
        bundle["payload"]["task_id"] = "TAMPERED"
        safe_bundle.write_text(json.dumps(bundle))

        assert verify_local_attestation(safe_bundle, attest_dir) is False

    def test_verify_non_local_bundle_raises(self, attest_dir: Path) -> None:
        """verify_local_attestation raises ValueError for non-local bundles."""
        bad = attest_dir / "bad.json"
        bad.parent.mkdir(parents=True, exist_ok=True)
        bad.write_text(json.dumps({"schema": "some-other-format"}))
        with pytest.raises(ValueError, match="Not a local Ed25519"):
            verify_local_attestation(bad, attest_dir)


# ---------------------------------------------------------------------------
# Index and listing
# ---------------------------------------------------------------------------


class TestAttestationIndex:
    def test_index_written_after_attestation(self, tmp_path: Path) -> None:
        """Attestations are appended to attestations.jsonl index."""
        attest_dir = tmp_path / "attestations"
        with patch(
            "bernstein.core.sigstore_attestation._sigstore_available",
            return_value=False,
        ):
            attest_task_completion(
                task_id="task-idx-1",
                agent_id="a",
                diff_sha256="d" * 64,
                event_hmac="e" * 64,
                attestation_dir=attest_dir,
            )
            attest_task_completion(
                task_id="task-idx-2",
                agent_id="b",
                diff_sha256="f" * 64,
                event_hmac="g" * 64,
                attestation_dir=attest_dir,
            )

        records = list_attestations(attest_dir)
        assert len(records) == 2
        # Newest first
        assert records[0]["payload"]["task_id"] == "task-idx-2"
        assert records[1]["payload"]["task_id"] == "task-idx-1"

    def test_list_empty_dir(self, tmp_path: Path) -> None:
        assert list_attestations(tmp_path / "nonexistent") == []

    def test_load_attestation_record(self, tmp_path: Path) -> None:
        """load_attestation_record parses a bundle file."""
        attest_dir = tmp_path / "attestations"
        with patch(
            "bernstein.core.sigstore_attestation._sigstore_available",
            return_value=False,
        ):
            record = attest_task_completion(
                task_id="task-load",
                agent_id="a",
                diff_sha256="d" * 64,
                event_hmac="e" * 64,
                attestation_dir=attest_dir,
            )

        data = load_attestation_record(Path(record.bundle_path))
        assert data["schema"] == "bernstein-local-attestation/v1"
        assert data["payload"]["task_id"] == "task-load"


# ---------------------------------------------------------------------------
# AttestationConfig
# ---------------------------------------------------------------------------


class TestAttestationConfig:
    def test_require_rekor_raises_on_failure(self, tmp_path: Path) -> None:
        """When require_rekor=True and sigstore fails, raise RuntimeError."""
        config = AttestationConfig(
            attestation_dir=tmp_path / "attestations",
            require_rekor=True,
        )
        with (
            patch(
                "bernstein.core.sigstore_attestation._sigstore_available",
                return_value=True,
            ),
            patch(
                "bernstein.core.sigstore_attestation._attest_with_sigstore",
                side_effect=RuntimeError("network error"),
            ),
            pytest.raises(RuntimeError, match="Rekor attestation required"),
        ):
            attest_task_completion(
                task_id="task-require",
                agent_id="a",
                diff_sha256="d" * 64,
                event_hmac="e" * 64,
                config=config,
            )

    def test_config_or_dir_required(self) -> None:
        """Must provide either config or attestation_dir."""
        with pytest.raises(ValueError, match="Provide either config"):
            attest_task_completion(
                task_id="t",
                agent_id="a",
                diff_sha256="d" * 64,
                event_hmac="e" * 64,
            )
