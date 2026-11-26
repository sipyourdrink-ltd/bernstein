"""Asymmetric EdDSA (Ed25519) signing and verification for agent identity JWTs (#8193).

Covers the 6 deliverables and 5 preserved security invariants of issue #8193:
1. ``create_jwt`` / ``verify_jwt`` support ``EdDSA`` (Ed25519) with private-key
   signing, public-key-only verification, and ``kid`` in the JOSE header.
2. ``AgentIdentityStore`` holds an out-of-workdir Ed25519 signing key under the
   same ``0600`` custody rules as the install audit key, loadable through
   ``KMSAdapter`` (file, env, HSM subclass), refusing keys inside ``.sdd/`` or
   with mode wider than ``0600``.
3. ``create_identity`` signs new tokens with ``EdDSA`` and records
   ``algorithm="EdDSA"``; ``_authenticate_jwt`` dispatches strictly from the
   MAC-authenticated credential record's ``algorithm``, never trusting the
   token header ``alg``.
4. ``/.well-known/agent.json/keys`` publishes the agent-token Ed25519 public
   key under its own distinct ``kid`` matching the token header ``kid``.
5. Migration window: existing unexpired ``HS256`` credentials keep verifying
   until expiry while no new ``HS256`` token is issued; ``BERNSTEIN_AUTH_JWT_SECRET``
   logs a deprecation warning during the migration window.
6. Preserved invariants: authority comes only from the MAC-authenticated
   identity record, only SHA-256 token hashes are stored, verification is
   offline, and revocation/suspension/expiry semantics are unchanged.
"""

# pyright: reportPrivateUsage=false, reportUnknownMemberType=false, reportUnknownVariableType=false, reportUnknownArgumentType=false

from __future__ import annotations

import base64
import hashlib
import json
import logging
import os
import socket
import sys
import time
from typing import TYPE_CHECKING, Any

import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient

from bernstein.core.identity.agent_jwt import (
    AgentCredential,
    AgentIdentity,
    AgentIdentityStore,
    IdentityKeyError,
    _reset_env_kms_adapter_cache_for_tests,
    load_or_create_agent_jwt_public_jwk,
    permissions_for_role,
)
from bernstein.core.interop.a2a_conformance import check_agent_card_v1_conformance, resolve_jwk
from bernstein.core.persistence.lineage_signer import LineageSignerError
from bernstein.core.routes.well_known import _DEFAULT_KID, _reset_signing_keypair_for_tests
from bernstein.core.security.agent_card_signer import ed25519_public_jwk, generate_ed25519_keypair
from bernstein.core.security.audit import AuditKeyPermissionError, load_or_create_audit_key
from bernstein.core.security.auth import (
    AuthService,
    AuthStore,
    AuthUser,
    SSOConfig,
    create_jwt,
    decode_jwt_unverified,
    verify_jwt,
)
from bernstein.core.security.auth_middleware import SSOAuthMiddleware
from bernstein.core.security.key_custody import (
    EnvBasedKMSAdapter,
    FileBasedKMSAdapter,
    HSMKMSAdapter,
    public_key_jwk_for,
)
from bernstein.core.security.key_derivation import DOMAIN_AGENT_IDENTITY_JWT, derive_store_key
from tests.unit._identity_record_helpers import seal

if TYPE_CHECKING:
    from pathlib import Path


def _make_app(jsonl_path: Path, *, auth_token: str | None = None) -> FastAPI:
    from bernstein.core.server import create_app

    return create_app(jsonl_path=jsonl_path, auth_token=auth_token)


def _b64url_encode(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64url_decode(data: str) -> bytes:
    return base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))


def _jwt_header(token: str) -> dict[str, Any]:
    return json.loads(_b64url_decode(token.split(".")[0]))


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _write_sealed_hs256_identity(
    auth_dir: Path,
    identity_id: str,
    hs256_secret: str,
    *,
    role: str = "backend",
    task_ids: list[str] | None = None,
    expiry_seconds: int = 3600,
) -> str:
    """Create a MAC-authenticated legacy HS256 identity record on disk for migration tests."""
    scoped_tasks = list(task_ids or ["task-legacy-1"])
    perms = permissions_for_role(role)
    raw_token = create_jwt(
        claims={
            "sub": identity_id,
            "sid": identity_id,
            "role": role,
            "scopes": sorted(perms),
            "tenant_id": "default",
            "task_ids": scoped_tasks,
            "allowed_files": [],
        },
        secret=hs256_secret,
        algorithm="HS256",
        expiry_seconds=expiry_seconds,
    )
    claims = decode_jwt_unverified(raw_token)
    assert claims is not None
    now = time.time()
    identity = AgentIdentity(
        id=identity_id,
        role=role,
        session_id=identity_id,
        permissions=perms,
        created_at=now,
        task_ids=scoped_tasks,
        allowed_files=[],
        credential=AgentCredential(
            token_hash=_token_hash(raw_token),
            created_at=now,
            expires_at=float(claims["exp"]),
            token_type="jwt",
            algorithm="HS256",
            jti=str(claims["jti"]),
            tenant_id="default",
            task_ids=scoped_tasks,
            allowed_files=[],
        ),
    )
    identities_dir = auth_dir / "agent_identities"
    identities_dir.mkdir(parents=True, exist_ok=True)
    path = identities_dir / f"{identity_id}.json"
    path.write_text(json.dumps(seal(identity.to_dict()), indent=2), encoding="utf-8")
    return raw_token


# ---------------------------------------------------------------------------
# 1. EdDSA in create_jwt and verify_jwt (public-key-only verification + kid)
# ---------------------------------------------------------------------------


class TestEdDSAJWTPrimitives:
    def test_eddsa_create_and_verify_with_public_key_only(self, tmp_path: Path) -> None:
        """EdDSA tokens verify with public key material alone (PEM, object, raw, JWK)."""
        private_pem, public_pem = generate_ed25519_keypair()
        key_file = tmp_path / "ed25519.key"
        key_file.write_bytes(private_pem)
        key_file.chmod(0o600)
        adapter = FileBasedKMSAdapter(key_file, kid="agent-jwt-test-kid")

        token = create_jwt(
            {"sub": "worker-1", "role": "backend"},
            adapter,
            algorithm="EdDSA",
            expiry_seconds=600,
            kid="agent-jwt-test-kid",
        )

        header = _jwt_header(token)
        assert header == {"alg": "EdDSA", "typ": "JWT", "kid": "agent-jwt-test-kid"}
        assert len(_b64url_decode(token.split(".")[2])) == 64

        # Verify using SPKI PEM bytes and str (no private key present).
        claims_pem_bytes = verify_jwt(token, public_pem, algorithm="EdDSA", kid="agent-jwt-test-kid")
        assert claims_pem_bytes is not None
        assert claims_pem_bytes["sub"] == "worker-1"

        claims_pem_str = verify_jwt(token, public_pem.decode("ascii"), algorithm="EdDSA")
        assert claims_pem_str is not None
        assert claims_pem_str["role"] == "backend"

        # Verify using Ed25519PublicKey object and raw 32-byte public key.
        pub_obj = serialization.load_pem_public_key(public_pem)
        raw_pub = pub_obj.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        assert verify_jwt(token, pub_obj, algorithm="EdDSA") is not None  # type: ignore[arg-type]
        assert verify_jwt(token, raw_pub, algorithm="EdDSA") is not None

        # Verify using an RFC 8037 OKP JWK dict directly.
        jwk = ed25519_public_jwk(public_pem, kid="agent-jwt-test-kid")
        assert verify_jwt(token, jwk, algorithm="EdDSA", kid="agent-jwt-test-kid") is not None
        assert verify_jwt(token, jwk, algorithm="EdDSA", kid="wrong-kid") is None

    def test_eddsa_signing_rejects_public_keys_and_verifier_rejects_private_keys(self) -> None:
        """Signing requires private key material; verification accepts public key material only."""
        private_pem, public_pem = generate_ed25519_keypair()
        private_key = Ed25519PrivateKey.generate()
        public_key = private_key.public_key()

        with pytest.raises((TypeError, ValueError)):
            create_jwt({"sub": "x"}, public_key, algorithm="EdDSA")  # type: ignore[arg-type]
        with pytest.raises((TypeError, ValueError)):
            create_jwt({"sub": "x"}, public_pem, algorithm="EdDSA")
        with pytest.raises((TypeError, ValueError)):
            create_jwt({"sub": "x"}, "shared-hmac-secret-32-bytes-long", algorithm="EdDSA")

        token = create_jwt({"sub": "worker-2"}, private_pem, algorithm="EdDSA")
        # Verifier must refuse private keys and private JWKs (with 'd').
        assert verify_jwt(token, private_key, algorithm="EdDSA") is None  # type: ignore[arg-type]
        assert verify_jwt(token, private_pem, algorithm="EdDSA") is None
        jwk_with_private_d = {**ed25519_public_jwk(public_pem, kid="k1"), "d": _b64url_encode(b"\x01" * 32)}
        assert verify_jwt(token, jwk_with_private_d, algorithm="EdDSA") is None

        # Wrong public key must fail signature verification.
        _other_priv, other_pub = generate_ed25519_keypair()
        assert verify_jwt(token, other_pub, algorithm="EdDSA") is None

    @pytest.mark.auth_enabled
    def test_non_ascii_jwt_payload_returns_none_and_http_401(self, tmp_path: Path) -> None:
        """Non-ASCII JWT segments return None (HTTP 401), never UnicodeEncodeError (HTTP 500)."""
        _priv, public_pem = generate_ed25519_keypair()
        hs256_hdr = _b64url_encode(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode("ascii"))
        eddsa_hdr = _b64url_encode(json.dumps({"alg": "EdDSA", "typ": "JWT"}, separators=(",", ":")).encode("ascii"))
        sig_64 = _b64url_encode(b"\x00" * 64)

        non_ascii_hs256 = f"{hs256_hdr}.caf\u00e9.{sig_64}"
        non_ascii_eddsa = f"{eddsa_hdr}.caf\u00e9.{sig_64}"

        assert verify_jwt(non_ascii_hs256, "shared-secret", algorithm="HS256") is None  # NOSONAR - test fixture
        assert verify_jwt(non_ascii_eddsa, public_pem, algorithm="EdDSA") is None

        # End-to-end through AuthMiddleware -> SSO AuthService.validate_token -> verify_jwt.
        sso_cfg = SSOConfig(jwt_secret="sso-non-ascii-secret", enabled=True)  # NOSONAR - test fixture
        sso_store = AuthStore(tmp_path / "sso")
        sso_store.save_user(AuthUser(id="u1", email="u1@example.com", display_name="User 1"))
        sso_service = AuthService(sso_cfg, sso_store)

        app = FastAPI()
        app.add_middleware(SSOAuthMiddleware, auth_service=sso_service)

        @app.get("/protected")
        async def _protected() -> JSONResponse:
            return JSONResponse({"ok": True})

        client = TestClient(app, raise_server_exceptions=False)
        raw_auth = f"Bearer {non_ascii_hs256}".encode("latin-1")
        resp = client.get("/protected", headers=[(b"authorization", raw_auth)])  # type: ignore[arg-type]
        assert resp.status_code == 401
        assert sso_service.validate_token(non_ascii_hs256) is None


# ---------------------------------------------------------------------------
# 2 & 3. Credential-bound EdDSA minting, header mismatch, and alg confusion
# ---------------------------------------------------------------------------


class TestCredentialBoundAlgorithmDispatch:
    def test_create_identity_signs_eddsa_even_when_legacy_env_secret_is_set(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """No new HS256 token is ever issued, even if BERNSTEIN_AUTH_JWT_SECRET is set."""
        monkeypatch.setenv("BERNSTEIN_AUTH_JWT_SECRET", "legacy-shared-secret")  # NOSONAR - test fixture
        store = AgentIdentityStore(tmp_path / ".sdd" / "auth")

        identity, token = store.create_identity("worker-eddsa-1", "backend", task_ids=["t-1"])

        assert identity.credential is not None
        assert identity.credential.algorithm == "EdDSA"
        header = _jwt_header(token)
        assert header["alg"] == "EdDSA"
        assert header["kid"] == store.signing_kid

        # Must NOT verify under the legacy HS256 secret; MUST verify with public key only.
        assert verify_jwt(token, "legacy-shared-secret", algorithm="HS256") is None
        claims = verify_jwt(token, store.public_key_jwk(), algorithm="EdDSA", kid=store.signing_kid)
        assert claims is not None
        assert claims["sub"] == "worker-eddsa-1"
        assert claims["task_ids"] == ["t-1"]

    def test_token_header_naming_different_alg_than_record_is_refused(self, tmp_path: Path) -> None:
        """_authenticate_jwt picks the verifier from credential.algorithm, never header.alg."""
        auth_dir = tmp_path / ".sdd" / "auth"
        store = AgentIdentityStore(auth_dir)
        _identity, eddsa_token = store.create_identity("worker-mismatch", "backend", task_ids=["t-1"])

        # Rewrite only the JOSE header of a real token to advertise another alg.
        _hdr_b64, payload_b64, sig_b64 = eddsa_token.split(".")
        forged_hdr = _b64url_encode(json.dumps({"alg": "HS256", "typ": "JWT"}, separators=(",", ":")).encode())
        assert store.authenticate(f"{forged_hdr}.{payload_b64}.{sig_b64}") is None

        none_hdr = _b64url_encode(json.dumps({"alg": "none", "typ": "JWT"}, separators=(",", ":")).encode())
        assert store.authenticate(f"{none_hdr}.{payload_b64}.") is None

        # Reverse direction: a legacy HS256 credential record refuses an EdDSA token
        # even when signed by this install's genuine Ed25519 signing key.
        hs256_secret = derive_store_key(load_or_create_audit_key(), DOMAIN_AGENT_IDENTITY_JWT).hex()
        _write_sealed_hs256_identity(auth_dir, "legacy-worker", hs256_secret, task_ids=["t-legacy"])
        reloaded = AgentIdentityStore(auth_dir)
        eddsa_against_hs256 = create_jwt(
            {
                "sub": "legacy-worker",
                "sid": "legacy-worker",
                "role": "backend",
                "scopes": sorted(permissions_for_role("backend")),
                "tenant_id": "default",
                "task_ids": ["t-legacy"],
                "allowed_files": [],
            },
            reloaded._signer,
            algorithm="EdDSA",
            kid=reloaded.signing_kid,
        )
        assert reloaded.authenticate(eddsa_against_hs256) is None

    @pytest.mark.auth_enabled
    def test_hs256_token_presented_against_eddsa_credential_is_refused(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Algorithm confusion: HS256 tokens signed with secrets or public-key bytes are refused."""
        monkeypatch.setenv("BERNSTEIN_AUTH_JWT_SECRET", "operator-legacy-secret")  # NOSONAR - test fixture
        app = _make_app(
            jsonl_path=tmp_path / ".sdd" / "runtime" / "tasks.jsonl",
            auth_token="operator-static-token",  # NOSONAR - test fixture
        )
        store: AgentIdentityStore = app.state.identity_store
        identity, genuine_token = store.create_identity("worker-confuse", "backend", task_ids=["t-1"])
        genuine_claims = decode_jwt_unverified(genuine_token)
        assert genuine_claims is not None
        assert identity.credential is not None
        assert identity.credential.algorithm == "EdDSA"

        # Build matching claims for the target EdDSA identity.
        forged_claims = {
            "sub": "worker-confuse",
            "sid": "worker-confuse",
            "role": "backend",
            "scopes": sorted(identity.permissions),
            "tenant_id": "default",
            "task_ids": ["t-1"],
            "allowed_files": [],
        }
        hkdf_secret = derive_store_key(load_or_create_audit_key(), DOMAIN_AGENT_IDENTITY_JWT).hex()
        pub_pem_str = store.public_key_pem.decode("ascii")
        pub_jwk_json = json.dumps(store.public_key_jwk(), sort_keys=True, separators=(",", ":"))

        client = TestClient(app)
        for candidate_secret in ("operator-legacy-secret", hkdf_secret, pub_pem_str, pub_jwk_json):
            confused_hs256 = create_jwt(forged_claims, candidate_secret, algorithm="HS256", expiry_seconds=3600)
            assert store.authenticate(confused_hs256) is None
            resp = client.get("/tasks", headers={"Authorization": f"Bearer {confused_hs256}"})
            assert resp.status_code == 401

        # Genuine EdDSA token still authenticates cleanly.
        ok_resp = client.get("/tasks", headers={"Authorization": f"Bearer {genuine_token}"})
        assert ok_resp.status_code == 200


# ---------------------------------------------------------------------------
# 4. JWKS publication with distinct kid & offline third-party verification
# ---------------------------------------------------------------------------


class TestJWKSPublicationAndOfflineVerification:
    def test_jwks_publishes_agent_token_key_with_distinct_kid_for_offline_verification(self, tmp_path: Path) -> None:
        """Third-party verifier checks agent JWT offline using only /.well-known/agent.json/keys."""
        _reset_signing_keypair_for_tests(tmp_path / "card-keys")
        app = _make_app(jsonl_path=tmp_path / ".sdd" / "runtime" / "tasks.jsonl")
        client = TestClient(app)

        store: AgentIdentityStore = app.state.identity_store
        _identity, agent_token = store.create_identity("worker-jwks-1", "backend", task_ids=["task-42"])

        card_resp = client.get("/.well-known/agent.json")
        jwks_resp = client.get("/.well-known/agent.json/keys")
        assert card_resp.status_code == 200
        assert jwks_resp.status_code == 200

        card = json.loads(card_resp.content)
        jwks = jwks_resp.json()

        # Existing A2A v1.0 card conformance must stay green.
        report = check_agent_card_v1_conformance(card, jwks=jwks)
        assert report.ok, [c.detail for c in report.checks if not c.passed]

        header = _jwt_header(agent_token)
        token_kid = header["kid"]
        card_kid = card["signatures"][0]["kid"]

        # Distinct kid from both the historical alias and the agent-card signing key kid.
        assert token_kid != _DEFAULT_KID
        assert token_kid != card_kid

        # Resolve JWK strictly by header kid and verify offline with no network/private key.
        matched_jwk = resolve_jwk(jwks, token_kid)
        assert matched_jwk is not None
        assert matched_jwk["kty"] == "OKP"
        assert matched_jwk["crv"] == "Ed25519"
        assert matched_jwk["alg"] == "EdDSA"
        assert matched_jwk["use"] == "sig"

        verified_claims = verify_jwt(agent_token, matched_jwk, algorithm="EdDSA", kid=token_kid)
        assert verified_claims is not None
        assert verified_claims["sub"] == "worker-jwks-1"
        assert verified_claims["task_ids"] == ["task-42"]


# ---------------------------------------------------------------------------
# 5. Migration window: unexpired vs expired HS256 + deprecation warning
# ---------------------------------------------------------------------------


class TestHS256MigrationWindow:
    def test_unexpired_hs256_verifies_with_env_secret_and_logs_deprecation_warning(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Existing unexpired HS256 credentials verify during migration and log deprecation."""
        secret_val = "migration-window-hs256-secret"  # NOSONAR - test fixture
        monkeypatch.setenv("BERNSTEIN_AUTH_JWT_SECRET", secret_val)
        auth_dir = tmp_path / ".sdd" / "auth"
        legacy_token = _write_sealed_hs256_identity(auth_dir, "legacy-hs256-live", secret_val, expiry_seconds=600)

        with caplog.at_level(logging.WARNING, logger="bernstein.core.identity.agent_jwt"):
            store = AgentIdentityStore(auth_dir)
            caplog.clear()
            authed = store.authenticate(legacy_token)

        assert authed is not None
        assert authed.id == "legacy-hs256-live"
        warnings = [r.getMessage() for r in caplog.records if r.levelno >= logging.WARNING]
        assert any("BERNSTEIN_AUTH_JWT_SECRET" in msg and "deprecated" in msg.lower() for msg in warnings)
        assert all(secret_val not in msg for msg in warnings)

    def test_expired_hs256_credential_is_refused_after_migration_window(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Once an existing HS256 credential expires, authentication refuses it."""
        secret_val = "migration-window-hs256-secret"  # NOSONAR - test fixture
        monkeypatch.setenv("BERNSTEIN_AUTH_JWT_SECRET", secret_val)
        auth_dir = tmp_path / ".sdd" / "auth"
        legacy_token = _write_sealed_hs256_identity(auth_dir, "legacy-hs256-exp", secret_val, expiry_seconds=5)
        store = AgentIdentityStore(auth_dir)
        assert store.authenticate(legacy_token) is not None

        future = float(time.time() + 3600)
        monkeypatch.setattr("bernstein.core.auth.time.time", lambda: future)
        monkeypatch.setattr("bernstein.core.identity.agent_jwt.time.time", lambda: future)

        assert store.authenticate(legacy_token) is None


# ---------------------------------------------------------------------------
# 6 & 7. Key custody (0600 mode, outside .sdd/, KMSAdapter) + invariants
# ---------------------------------------------------------------------------


class TestSigningKeyCustodyAndInvariants:
    @pytest.mark.skipif(sys.platform == "win32", reason="POSIX file permission bits")
    def test_private_key_wider_than_0600_is_refused(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Private key outside .sdd/ with mode 0644 is refused on load."""
        private_pem, _ = generate_ed25519_keypair()
        loose_key = tmp_path / "outside-keys" / "agent_jwt.ed25519"
        loose_key.parent.mkdir(parents=True, exist_ok=True)
        loose_key.write_bytes(private_pem)
        loose_key.chmod(0o644)

        auth_dir = tmp_path / "project" / ".sdd" / "auth"
        monkeypatch.setenv("BERNSTEIN_AGENT_JWT_SIGNING_KEY_PATH", str(loose_key))
        with pytest.raises(AuditKeyPermissionError):
            AgentIdentityStore(auth_dir)

        monkeypatch.delenv("BERNSTEIN_AGENT_JWT_SIGNING_KEY_PATH", raising=False)
        adapter = FileBasedKMSAdapter(loose_key)
        with pytest.raises(AuditKeyPermissionError):
            AgentIdentityStore(auth_dir, signer=adapter)

    def test_signing_key_inside_sdd_is_refused_and_nothing_under_sdd_signs(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Nothing under .sdd/ may hold or act as the agent-token signing key."""
        workdir = tmp_path / "repo"
        sdd_dir = workdir / ".sdd"
        auth_dir = sdd_dir / "auth"
        in_tree_key = sdd_dir / "keys" / "bad.ed25519"
        in_tree_key.parent.mkdir(parents=True, exist_ok=True)
        private_pem, _ = generate_ed25519_keypair()
        in_tree_key.write_bytes(private_pem)
        in_tree_key.chmod(0o600)

        monkeypatch.setenv("BERNSTEIN_AGENT_JWT_SIGNING_KEY_PATH", str(in_tree_key))
        with pytest.raises(IdentityKeyError):
            AgentIdentityStore(auth_dir)

        monkeypatch.delenv("BERNSTEIN_AGENT_JWT_SIGNING_KEY_PATH", raising=False)
        with pytest.raises(IdentityKeyError):
            AgentIdentityStore(auth_dir, signer=FileBasedKMSAdapter(in_tree_key))

        # Remove the attacker-planted test file so we inspect only what the store itself writes.
        in_tree_key.unlink()

        # Default store keeps the private key outside workdir/.sdd and at mode 0600.
        store = AgentIdentityStore(auth_dir)
        _identity, token = store.create_identity("worker-sdd-check", "backend", task_ids=["t-1"])

        for sdd_file in sdd_dir.rglob("*"):
            if sdd_file.is_file():
                content = sdd_file.read_bytes()
                assert b"PRIVATE KEY" not in content
                # Raw JWT token is never persisted anywhere in .sdd/auth/; only SHA-256 hash is.
                assert token.encode("utf-8") not in content

    def test_kms_adapter_boundary_supports_env_and_hsm_subclass(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """AgentIdentityStore loads signing keys cleanly through EnvBasedKMSAdapter and HSMKMSAdapter."""
        private_pem, _ = generate_ed25519_keypair()
        monkeypatch.setenv("TEST_AGENT_JWT_PEM", private_pem.decode("ascii"))
        env_adapter = EnvBasedKMSAdapter("TEST_AGENT_JWT_PEM", kid="env-kms-kid-1")

        store_env = AgentIdentityStore(tmp_path / "env-auth", signer=env_adapter)
        _id1, tok1 = store_env.create_identity("worker-env", "backend", task_ids=["t-env"])
        assert _jwt_header(tok1)["kid"] == "env-kms-kid-1"
        assert store_env.authenticate(tok1) is not None

        # Custom HSMKMSAdapter subclass simulating PKCS#11 / Cloud KMS signing without disk PEM.
        hsm_key = Ed25519PrivateKey.generate()

        class _InMemoryHSM(HSMKMSAdapter):
            def sign(self, payload: bytes) -> bytes:
                return hsm_key.sign(payload)

            def public_key_jwk(self) -> dict[str, str]:
                return public_key_jwk_for(hsm_key.public_key(), kid=self.kid or "hsm-slot-0")

        hsm_signer = _InMemoryHSM("pkcs11:object=agent-jwt;type=private", kid="hsm-agent-kid-1")
        store_hsm = AgentIdentityStore(tmp_path / "hsm-auth", signer=hsm_signer)
        _id2, tok2 = store_hsm.create_identity("worker-hsm", "backend", task_ids=["t-hsm"])
        assert _jwt_header(tok2)["kid"] == "hsm-agent-kid-1"
        assert store_hsm.authenticate(tok2) is not None

    def test_env_kms_adapter_supports_multiple_stores_and_scrubs_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Env KMS adapter caches per process after scrubbing the env var so multiple stores work."""
        private_pem_1, _ = generate_ed25519_keypair()
        env_var = "TEST_MULTI_STORE_AGENT_JWT_PEM"
        monkeypatch.setenv("BERNSTEIN_AGENT_JWT_KMS_ADAPTER", "env")
        monkeypatch.setenv("BERNSTEIN_AGENT_JWT_KMS_ENV_VAR", env_var)
        monkeypatch.setenv("BERNSTEIN_AGENT_JWT_KMS_KID", "env-multi-kid-1")
        monkeypatch.setenv(env_var, private_pem_1.decode("ascii"))

        auth_dir = tmp_path / "shared-auth"
        store1 = AgentIdentityStore(auth_dir)
        # First load scrubs the raw private key from os.environ so child subprocesses cannot inherit it.
        assert env_var not in os.environ

        # Second store in the same process (e.g. orchestrator + spawner) and JWKS lookup must succeed.
        store2 = AgentIdentityStore(auth_dir)
        published_jwk = load_or_create_agent_jwt_public_jwk()

        assert store1.signing_kid == "env-multi-kid-1"
        assert store2.signing_kid == "env-multi-kid-1"
        assert store1.public_key_jwk() == store2.public_key_jwk() == published_jwk

        _id1, tok1 = store1.create_identity("worker-multi-env", "backend", task_ids=["t-multi"])
        authed = store2.authenticate(tok1)
        assert authed is not None
        assert authed.id == "worker-multi-env"

        # Setting a new PEM in the env var loads the rotated key and scrubs it again.
        private_pem_2, _ = generate_ed25519_keypair()
        monkeypatch.setenv(env_var, private_pem_2.decode("ascii"))
        store3 = AgentIdentityStore(tmp_path / "rotated-auth")
        assert env_var not in os.environ
        assert store3.public_key_jwk()["x"] != store1.public_key_jwk()["x"]

        # Without a cached adapter or env var, construction still fails closed.
        _reset_env_kms_adapter_cache_for_tests()
        with pytest.raises(LineageSignerError):
            AgentIdentityStore(tmp_path / "missing-env-auth")

    def test_invariants_preserved_record_mac_authority_offline_and_lifecycle(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Record MAC stays sole authority; only SHA-256 stored; offline; lifecycle intact."""
        store = AgentIdentityStore(tmp_path / ".sdd" / "auth")
        identity, token = store.create_identity(
            "worker-inv",
            "backend",
            task_ids=["t-allowed"],
            allowed_files=["src/**"],
        )

        # Only SHA-256 hash stored on credential.
        record_path = tmp_path / ".sdd" / "auth" / "agent_identities" / "worker-inv.json"
        persisted = record_path.read_text(encoding="utf-8")
        assert token not in persisted
        assert _token_hash(token) in persisted

        # Even a token signed by the genuine Ed25519 install key cannot widen claims past the record.
        widened_token = create_jwt(
            {
                "sub": "worker-inv",
                "sid": "worker-inv",
                "role": "manager",
                "scopes": sorted(permissions_for_role("manager")),
                "tenant_id": "default",
                "task_ids": [],
                "allowed_files": [],
            },
            store._signer,
            algorithm="EdDSA",
            kid=store.signing_kid,
        )
        assert store.authenticate(widened_token) is None

        # Verification stays 100% offline (block all socket creation).
        def _deny_network(*_args: object, **_kwargs: object) -> Any:
            msg = "network call attempted during offline JWT verification"
            raise AssertionError(msg)

        monkeypatch.setattr(socket, "socket", _deny_network)
        monkeypatch.setattr(socket, "create_connection", _deny_network)

        authed = store.authenticate(token)
        assert authed is not None
        assert authed.permissions == identity.permissions

        # Suspension, reactivation, and revocation behave identically.
        assert store.suspend("worker-inv", reason="pause")
        assert store.authenticate(token) is None
        assert store.reactivate("worker-inv")
        assert store.authenticate(token) is not None
        assert store.revoke("worker-inv", reason="done")
        assert store.authenticate(token) is None
