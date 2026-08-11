"""Tests for Sigstore/Rekor cryptographic attestation module.

These tests exercise the attestation module without requiring network access
or the sigstore package - they mock out the sigstore path and directly test
the Ed25519 fallback and the data model / persistence layer.
"""

from __future__ import annotations

import json
import os
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

        # Tamper with the payload - use a known-safe path derived from attest_dir
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
# Containment of the bundle-supplied public_key_file
# ---------------------------------------------------------------------------


class TestVerifyPublicKeyContainment:
    """``public_key_file`` is attacker-controlled and must stay inside the dir.

    The bundle is untrusted input: whoever hands us a bundle picks the file
    the verifier loads its public key from.  If that file can be steered
    outside ``attestation_dir``, an attacker signs any payload they like with
    a key they own and the bundle still verifies.  Each rejection case below
    forges a bundle end-to-end with an attacker keypair, so the containment
    check is the only thing standing between the forgery and a ``True``
    verdict.
    """

    @pytest.fixture
    def attest_dir(self, tmp_path: Path) -> Path:
        return tmp_path / "attestations"

    def _forged_bundle(self, attest_dir: Path, key_dir: Path) -> tuple[Path, Path]:
        """Write a genuine bundle, then re-sign it with an attacker key.

        The attacker public key lands in ``key_dir``; the returned bundle has
        a tampered payload signed by the matching private key.  Returns the
        bundle path and the attacker public-key path.
        """
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

        with patch(
            "bernstein.core.sigstore_attestation._sigstore_available",
            return_value=False,
        ):
            record = attest_task_completion(
                task_id="task-traversal",
                agent_id="a",
                diff_sha256="d" * 64,
                event_hmac="e" * 64,
                attestation_dir=attest_dir,
            )

        evil_private = Ed25519PrivateKey.generate()
        key_dir.mkdir(parents=True, exist_ok=True)
        evil_pub = key_dir / "ed25519-public-key.pem"
        evil_pub.write_bytes(
            evil_private.public_key().public_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PublicFormat.SubjectPublicKeyInfo,
            )
        )

        bundle_path = Path(record.bundle_path)
        bundle = json.loads(bundle_path.read_text())
        bundle["payload"]["task_id"] = "FORGED"
        payload_bytes = AttestationPayload(**bundle["payload"]).canonical_json().encode()
        bundle["signature_hex"] = evil_private.sign(payload_bytes).hex()
        bundle_path.write_text(json.dumps(bundle))
        return bundle_path, evil_pub

    def test_rejects_sibling_directory_with_shared_name_prefix(self, attest_dir: Path) -> None:
        """``../attestations-evil/key.pem`` is outside the dir despite the prefix.

        A plain string ``startswith`` on the resolved path accepts any sibling
        directory whose name begins with the allowed directory's name.
        """
        evil_dir = attest_dir.parent / f"{attest_dir.name}-evil"
        bundle_path, _ = self._forged_bundle(attest_dir, evil_dir)

        bundle = json.loads(bundle_path.read_text())
        bundle["public_key_file"] = f"../{evil_dir.name}/ed25519-public-key.pem"
        bundle_path.write_text(json.dumps(bundle))

        with pytest.raises(ValueError, match="Path traversal detected"):
            verify_local_attestation(bundle_path, attest_dir)

    def test_rejects_absolute_public_key_file_outside_dir(self, attest_dir: Path) -> None:
        """An absolute path escapes ``attestation_dir / raw`` entirely."""
        evil_dir = attest_dir.parent / "elsewhere"
        bundle_path, evil_pub = self._forged_bundle(attest_dir, evil_dir)

        bundle = json.loads(bundle_path.read_text())
        bundle["public_key_file"] = str(evil_pub)
        bundle_path.write_text(json.dumps(bundle))

        with pytest.raises(ValueError, match="Path traversal detected"):
            verify_local_attestation(bundle_path, attest_dir)

    def test_rejects_absolute_public_key_file_inside_dir(self, attest_dir: Path) -> None:
        """Absolute values are refused even when they land inside the dir.

        Bundles carry a bare filename (``pub_path.name``), so an absolute
        value never comes from the writer -- it only comes from a crafted
        bundle probing the guard.
        """
        bundle_path, evil_pub = self._forged_bundle(attest_dir, attest_dir)

        bundle = json.loads(bundle_path.read_text())
        bundle["public_key_file"] = str(evil_pub)
        bundle_path.write_text(json.dumps(bundle))

        with pytest.raises(ValueError, match="Path traversal detected"):
            verify_local_attestation(bundle_path, attest_dir)

    def test_rejects_a_contained_subdirectory_path(self, attest_dir: Path) -> None:
        """Even a name that stays inside the directory is refused if it descends.

        Containment is decided from the name alone, with no filesystem lookup,
        so it cannot be raced.  That only holds for a single plain component:
        ``sub/key.pem`` would have to resolve ``sub``, and resolving is the
        step an attacker gets to interfere with.  The writer emits a bare
        filename, so nothing legitimate descends.
        """
        bundle_path, _ = self._forged_bundle(attest_dir, attest_dir / "sub")

        bundle = json.loads(bundle_path.read_text())
        bundle["public_key_file"] = "sub/ed25519-public-key.pem"
        bundle_path.write_text(json.dumps(bundle))

        with pytest.raises(ValueError, match="Path traversal detected"):
            verify_local_attestation(bundle_path, attest_dir)

    def test_accepts_plain_filename_inside_dir(self, attest_dir: Path) -> None:
        """The legitimate shape -- a bare filename -- still verifies."""
        with patch(
            "bernstein.core.sigstore_attestation._sigstore_available",
            return_value=False,
        ):
            record = attest_task_completion(
                task_id="task-contained",
                agent_id="a",
                diff_sha256="d" * 64,
                event_hmac="e" * 64,
                attestation_dir=attest_dir,
            )

        bundle_path = Path(record.bundle_path)
        assert json.loads(bundle_path.read_text())["public_key_file"] == "ed25519-public-key.pem"
        assert verify_local_attestation(bundle_path, attest_dir) is True


# ---------------------------------------------------------------------------
# How the contained key file is opened
# ---------------------------------------------------------------------------


class TestVerifyPublicKeyOpen:
    """Containment validates a path; the open must reach the same object.

    Checking a path string and later opening that string are two separate
    lookups, so anything that can write into ``attestation_dir`` could swap a
    symlink in between them.  The open therefore refuses to follow a link at
    the named component and insists on a regular file, which removes the
    window instead of narrowing it.
    """

    @pytest.fixture
    def attest_dir(self, tmp_path: Path) -> Path:
        return tmp_path / "attestations"

    def _bundle_naming(self, attest_dir: Path, key_name: str) -> Path:
        """Write a genuine bundle whose ``public_key_file`` is ``key_name``."""
        with patch(
            "bernstein.core.sigstore_attestation._sigstore_available",
            return_value=False,
        ):
            record = attest_task_completion(
                task_id="task-open",
                agent_id="a",
                diff_sha256="d" * 64,
                event_hmac="e" * 64,
                attestation_dir=attest_dir,
            )

        bundle_path = Path(record.bundle_path)
        bundle = json.loads(bundle_path.read_text())
        bundle["public_key_file"] = key_name
        bundle_path.write_text(json.dumps(bundle))
        return bundle_path

    @pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="O_NOFOLLOW is POSIX-only")
    def test_refuses_symlink_at_the_named_component(self, attest_dir: Path) -> None:
        """A symlink is refused even when it points at the real, contained key.

        The link resolves inside the directory, so containment alone is happy
        with it.  Refusing to follow it is what makes the checked path and the
        opened file the same object.
        """
        bundle_path = self._bundle_naming(attest_dir, "linked-key.pem")
        (attest_dir / "linked-key.pem").symlink_to(attest_dir / "ed25519-public-key.pem")

        with pytest.raises(ValueError, match="symlink"):
            verify_local_attestation(bundle_path, attest_dir)

    def test_refuses_non_regular_file(self, attest_dir: Path) -> None:
        """A directory at the named component is a ValueError, not an OSError.

        Callers already handle ``ValueError`` from this function; letting an
        ``IsADirectoryError`` escape would make a crafted bundle look like a
        local I/O fault.
        """
        bundle_path = self._bundle_naming(attest_dir, "key-dir")
        (attest_dir / "key-dir").mkdir()

        with pytest.raises(ValueError, match="not a regular file"):
            verify_local_attestation(bundle_path, attest_dir)

    @pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="needs a POSIX host to emulate from")
    def test_refuses_symlink_without_dir_fd_or_nofollow(
        self, attest_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The no-``dir_fd`` branch refuses a symlink too.

        Emulates the platform Windows presents -- no ``dir_fd`` support and no
        ``O_NOFOLLOW`` -- because a POSIX host would otherwise never reach that
        branch and it would ship untested.  The check there is best effort: it
        catches a symlink that is present, not one planted in the instant
        before the open, which is the most the platform allows.
        """
        bundle_path = self._bundle_naming(attest_dir, "linked-key.pem")
        (attest_dir / "linked-key.pem").symlink_to(attest_dir / "ed25519-public-key.pem")
        monkeypatch.setattr(os, "supports_dir_fd", set())
        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)

        with pytest.raises(ValueError, match="symlink"):
            verify_local_attestation(bundle_path, attest_dir)

    @pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="needs a POSIX host to emulate from")
    def test_refuses_symlink_with_dir_fd_but_no_nofollow(
        self, attest_dir: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Having ``dir_fd`` is not on its own enough to select the anchored open.

        The anchored branch refuses a symlink only because ``O_NOFOLLOW`` is in
        its flags, and the best-effort check lives in the other branch.  A
        platform with one capability and not the other therefore has to reach
        the best-effort branch, or it gets neither protection.
        """
        bundle_path = self._bundle_naming(attest_dir, "linked-key.pem")
        (attest_dir / "linked-key.pem").symlink_to(attest_dir / "ed25519-public-key.pem")
        # ``supports_dir_fd`` is deliberately left intact: this is the mixed
        # capability case, not the no-capability one covered above.
        monkeypatch.delattr(os, "O_NOFOLLOW", raising=False)

        with pytest.raises(ValueError, match="symlink"):
            verify_local_attestation(bundle_path, attest_dir)

    def test_missing_key_file_still_raises_oserror(self, attest_dir: Path) -> None:
        """A genuinely absent key stays an OSError -- that is a local fault."""
        bundle_path = self._bundle_naming(attest_dir, "no-such-key.pem")

        with pytest.raises(FileNotFoundError):
            verify_local_attestation(bundle_path, attest_dir)


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
