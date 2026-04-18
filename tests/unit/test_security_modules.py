"""Tests for security modules: IP allowlist, JWT tokens, sensitivity labels."""

from __future__ import annotations

import time
from pathlib import Path

import pytest
from bernstein.core.ip_allowlist import check_ip_allowed
from bernstein.core.jwt_tokens import JWTManager
from bernstein.core.models import Task
from bernstein.core.seed import NetworkConfig, SeedConfig
from httpx import ASGITransport, AsyncClient

from bernstein.core.server import create_app


class TestIPAllowlist:
    """Test IP allowlist functionality."""

    def test_check_ip_allowed_localhost(self) -> None:
        """Test localhost is always allowed."""
        assert check_ip_allowed("127.0.0.1", ["10.0.0.0/8"]) is True
        assert check_ip_allowed("::1", ["10.0.0.0/8"]) is True

    def test_check_ip_allowed_in_range(self) -> None:
        """Test IP in allowed range."""
        assert check_ip_allowed("10.0.0.5", ["10.0.0.0/8"]) is True
        assert check_ip_allowed("192.168.1.100", ["192.168.1.0/24"]) is True

    def test_check_ip_allowed_not_in_range(self) -> None:
        """Test IP not in allowed range."""
        assert check_ip_allowed("8.8.8.8", ["10.0.0.0/8"]) is False

    def test_check_ip_allowed_empty_list(self) -> None:
        """Test with empty allowlist."""
        assert check_ip_allowed("10.0.0.5", []) is False

    @pytest.mark.anyio
    async def test_middleware_allows_seed_allowlisted_ip(self, tmp_path: Path) -> None:
        """Test seed-configured CIDRs are enforced by the canonical middleware."""
        app = create_app(jsonl_path=tmp_path / "tasks.jsonl")
        app.state.seed_config = SeedConfig(goal="Test", network=NetworkConfig(allowed_ips=("10.0.0.0/8",)))
        transport = ASGITransport(app=app, client=("10.2.3.4", 123))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/tasks")
        assert response.status_code == 200

    @pytest.mark.anyio
    async def test_middleware_blocks_disallowed_ip(self, tmp_path: Path) -> None:
        """Test non-allowlisted IPs are rejected on protected routes."""
        app = create_app(jsonl_path=tmp_path / "tasks.jsonl")
        app.state.seed_config = SeedConfig(goal="Test", network=NetworkConfig(allowed_ips=("10.0.0.0/8",)))
        transport = ASGITransport(app=app, client=("203.0.113.9", 123))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/tasks")
        assert response.status_code == 403

    @pytest.mark.anyio
    async def test_middleware_trusts_forwarded_headers_only_from_loopback(self, tmp_path: Path) -> None:
        """Test X-Forwarded-For is honored only for trusted local proxies."""
        app = create_app(jsonl_path=tmp_path / "tasks.jsonl")
        app.state.seed_config = SeedConfig(goal="Test", network=NetworkConfig(allowed_ips=("10.0.0.0/8",)))

        trusted_transport = ASGITransport(app=app, client=("127.0.0.1", 123))
        async with AsyncClient(transport=trusted_transport, base_url="http://test") as client:
            response = await client.get("/tasks", headers={"X-Forwarded-For": "10.2.3.4"})
        assert response.status_code == 200

        untrusted_transport = ASGITransport(app=app, client=("203.0.113.9", 123))
        async with AsyncClient(transport=untrusted_transport, base_url="http://test") as client:
            response = await client.get("/tasks", headers={"X-Forwarded-For": "10.2.3.4"})
        assert response.status_code == 403

    @pytest.mark.anyio
    async def test_middleware_allows_public_path(self, tmp_path: Path) -> None:
        """Test public endpoints bypass the allowlist."""
        app = create_app(jsonl_path=tmp_path / "tasks.jsonl")
        app.state.seed_config = SeedConfig(goal="Test", network=NetworkConfig(allowed_ips=("10.0.0.0/8",)))
        transport = ASGITransport(app=app, client=("203.0.113.9", 123))
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            response = await client.get("/health")
        assert response.status_code == 200


class TestJWTManager:
    """Test JWT token management."""

    def test_create_and_verify_token(self) -> None:
        """Test creating and verifying a token."""
        manager = JWTManager(secret="test-secret", expiry_hours=1)

        token = manager.create_token(
            session_id="session-123",
            user_id="user-456",
            scopes=["read", "write"],
        )

        payload = manager.verify_token(token)

        assert payload is not None
        assert payload.session_id == "session-123"
        assert payload.user_id == "user-456"
        assert "read" in payload.scopes

    def test_token_expiry(self) -> None:
        """Test token expiry."""
        # Create token that expires immediately
        manager = JWTManager(secret="test-secret", expiry_hours=0)

        token = manager.create_token(session_id="session-123")

        # Wait a tiny bit
        time.sleep(0.1)

        # Token should be expired
        payload = manager.verify_token(token)
        assert payload is None

    def test_invalid_token(self) -> None:
        """Test invalid token verification."""
        manager = JWTManager(secret="test-secret")

        # Tampered token
        payload = manager.verify_token("invalid.token.here")
        assert payload is None

    def test_token_with_different_secrets(self) -> None:
        """Test token signed with different secret."""
        manager1 = JWTManager(secret="secret1")
        manager2 = JWTManager(secret="secret2")

        token = manager1.create_token(session_id="session-123")

        # Token signed with secret1 should not verify with secret2
        payload = manager2.verify_token(token)
        assert payload is None

    def test_alg_none_is_rejected(self) -> None:
        """audit-053: alg=none MUST be rejected even if signature is empty/absent."""
        import base64
        import json as _json

        manager = JWTManager(secret="test-secret")

        header = {"alg": "none", "typ": "JWT"}
        payload = {
            "session_id": "s",
            "user_id": None,
            "iat": time.time(),
            "exp": time.time() + 3600,
            "scopes": [],
        }

        def _b64(data: bytes) -> str:
            return base64.urlsafe_b64encode(data).rstrip(b"=").decode("utf-8")

        header_b64 = _b64(_json.dumps(header).encode())
        payload_b64 = _b64(_json.dumps(payload).encode())
        # Intentionally empty signature — classic "alg: none" bypass.
        forged = f"{header_b64}.{payload_b64}."

        assert manager.verify_token(forged) is None

    def test_alg_none_uppercase_is_rejected(self) -> None:
        """audit-053: 'None', 'NONE' etc. must also be rejected (case-insensitive)."""
        import base64
        import json as _json

        manager = JWTManager(secret="test-secret")

        def _b64(data: bytes) -> str:
            return base64.urlsafe_b64encode(data).rstrip(b"=").decode("utf-8")

        for alg in ("None", "NONE", "nOnE"):
            header_b64 = _b64(_json.dumps({"alg": alg, "typ": "JWT"}).encode())
            payload_b64 = _b64(
                _json.dumps(
                    {
                        "session_id": "s",
                        "user_id": None,
                        "iat": time.time(),
                        "exp": time.time() + 3600,
                        "scopes": [],
                    }
                ).encode()
            )
            forged = f"{header_b64}.{payload_b64}."
            assert manager.verify_token(forged) is None, f"alg={alg!r} was accepted"

    def test_mismatched_alg_is_rejected(self) -> None:
        """audit-053: a token advertising HS512 must fail under an HS256 verifier."""
        import base64
        import hashlib as _hashlib
        import hmac as _hmac
        import json as _json

        secret = "test-secret"
        manager = JWTManager(secret=secret, algorithm="HS256")

        def _b64(data: bytes) -> str:
            return base64.urlsafe_b64encode(data).rstrip(b"=").decode("utf-8")

        # Build a self-consistent HS512 token (valid HMAC under SHA-512)…
        header_b64 = _b64(_json.dumps({"alg": "HS512", "typ": "JWT"}).encode())
        payload_b64 = _b64(
            _json.dumps(
                {
                    "session_id": "s",
                    "user_id": None,
                    "iat": time.time(),
                    "exp": time.time() + 3600,
                    "scopes": [],
                }
            ).encode()
        )
        signing_input = f"{header_b64}.{payload_b64}"
        sig = _hmac.new(secret.encode(), signing_input.encode(), _hashlib.sha512).digest()
        token = f"{signing_input}.{_b64(sig)}"

        # …the verifier is configured for HS256, so this MUST be rejected
        # on the header check, before any HMAC compare.
        assert manager.verify_token(token) is None

    def test_missing_alg_is_rejected(self) -> None:
        """audit-053: a header without an alg claim must be rejected."""
        import base64
        import json as _json

        manager = JWTManager(secret="test-secret")

        def _b64(data: bytes) -> str:
            return base64.urlsafe_b64encode(data).rstrip(b"=").decode("utf-8")

        header_b64 = _b64(_json.dumps({"typ": "JWT"}).encode())  # no 'alg'
        payload_b64 = _b64(
            _json.dumps(
                {
                    "session_id": "s",
                    "user_id": None,
                    "iat": time.time(),
                    "exp": time.time() + 3600,
                    "scopes": [],
                }
            ).encode()
        )
        assert manager.verify_token(f"{header_b64}.{payload_b64}.") is None


class TestSensitivityLabels:
    """Test sensitivity labels on tasks."""

    def test_task_default_sensitivity(self) -> None:
        """Test task default sensitivity is internal."""
        task = Task(
            id="task-1",
            title="Test task",
            description="Test",
            role="backend",
        )

        assert task.sensitivity == "internal"

    def test_task_custom_sensitivity(self) -> None:
        """Test task with custom sensitivity."""
        task = Task(
            id="task-1",
            title="Test task",
            description="Test",
            role="backend",
            sensitivity="confidential",
        )

        assert task.sensitivity == "confidential"

    def test_task_sensitivity_values(self) -> None:
        """Test all sensitivity values are valid."""
        for sensitivity in ["public", "internal", "confidential"]:
            task = Task(
                id="task-1",
                title="Test",
                description="Test",
                role="backend",
                sensitivity=sensitivity,  # type: ignore
            )
            assert task.sensitivity == sensitivity
