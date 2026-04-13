"""WEB-014: Tests for webhook signature verification."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from bernstein.core.webhook_signatures import sign_hmac_sha256
from bernstein.core.webhook_verify import (
    WebhookSignatureVerifier,
    verify_webhook_request,
)
from fastapi import HTTPException


class TestVerifyWebhookRequest:
    """Test the convenience verification function."""

    def test_valid_signature(self) -> None:
        payload = b'{"event": "push"}'
        secret = "test-secret"
        sig = sign_hmac_sha256(secret, payload)
        assert verify_webhook_request(payload, sig, secret) is True

    def test_invalid_signature(self) -> None:
        payload = b'{"event": "push"}'
        assert verify_webhook_request(payload, "sha256=invalid", "test-secret") is False

    def test_empty_secret(self) -> None:
        payload = b'{"event": "push"}'
        assert verify_webhook_request(payload, "sha256=abc", "") is False

    def test_mismatched_secret(self) -> None:
        payload = b'{"event": "push"}'
        sig = sign_hmac_sha256("secret-a", payload)
        assert verify_webhook_request(payload, sig, "secret-b") is False


class TestWebhookSignatureVerifier:
    """Test the FastAPI dependency class."""

    @pytest.mark.anyio()
    async def test_no_secret_skips_verification(self) -> None:
        """When no secret is configured, verification should pass."""
        verifier = WebhookSignatureVerifier(secret="")
        request = MagicMock()
        request.app.state = MagicMock(spec=[])  # No webhook_secret attr
        # Should not raise
        with patch.dict("os.environ", {}, clear=True):
            await verifier(request)

    @pytest.mark.anyio()
    async def test_missing_header_raises_401(self) -> None:
        """Missing signature header should raise 401."""
        verifier = WebhookSignatureVerifier(secret="my-secret")
        request = MagicMock()
        request.headers = {}
        with pytest.raises(HTTPException) as exc_info:
            await verifier(request)
        assert exc_info.value.status_code == 401

    @pytest.mark.anyio()
    async def test_invalid_signature_raises_403(self) -> None:
        """Invalid signature should raise 403."""
        verifier = WebhookSignatureVerifier(secret="my-secret")
        request = MagicMock()
        request.headers = {"X-Signature-256": "sha256=badhex0000000000000000000000000000000000000000000000000000000000"}
        request.body = AsyncMock(return_value=b'{"data": "test"}')
        with pytest.raises(HTTPException) as exc_info:
            await verifier(request)
        assert exc_info.value.status_code == 403

    @pytest.mark.anyio()
    async def test_valid_signature_passes(self) -> None:
        """Valid signature should not raise."""
        payload = b'{"data": "test"}'
        secret = "my-secret"
        sig = sign_hmac_sha256(secret, payload)

        verifier = WebhookSignatureVerifier(secret=secret)
        request = MagicMock()
        request.headers = {"X-Signature-256": sig}
        request.body = AsyncMock(return_value=payload)
        # Should not raise
        await verifier(request)

    @pytest.mark.anyio()
    async def test_secret_from_env(self) -> None:
        """Verifier should read secret from env when not passed directly."""
        payload = b'{"data": "env-test"}'
        env_secret = "env-webhook-secret"
        sig = sign_hmac_sha256(env_secret, payload)

        verifier = WebhookSignatureVerifier()
        request = MagicMock()
        request.app.state = MagicMock(spec=[])
        request.headers = {"X-Signature-256": sig}
        request.body = AsyncMock(return_value=payload)

        with patch.dict("os.environ", {"BERNSTEIN_WEBHOOK_SECRET": env_secret}):
            await verifier(request)  # Should not raise

    @pytest.mark.anyio()
    async def test_custom_header(self) -> None:
        """Custom header name should be used for signature lookup."""
        payload = b'{"data": "custom"}'
        secret = "custom-secret"
        sig = sign_hmac_sha256(secret, payload)

        verifier = WebhookSignatureVerifier(secret=secret, header="X-Hub-Signature")
        request = MagicMock()
        request.headers = {"X-Hub-Signature": sig}
        request.body = AsyncMock(return_value=payload)
        await verifier(request)  # Should not raise
