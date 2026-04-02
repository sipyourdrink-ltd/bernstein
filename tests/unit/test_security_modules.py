"""Tests for security modules: IP allowlist, JWT tokens, sensitivity labels."""

from __future__ import annotations

import time
from pathlib import Path

from bernstein.core.ip_allowlist import check_ip_allowed
from bernstein.core.jwt_tokens import JWTManager, JWTPayload
from bernstein.core.models import Task


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
