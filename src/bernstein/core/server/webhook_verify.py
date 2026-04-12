"""WEB-014: Webhook signature verification middleware/dependency.

FastAPI dependency that verifies inbound HMAC-SHA256 signatures on
webhook requests.  Uses the shared helpers from webhook_signatures.py.
"""

from __future__ import annotations

import logging

from fastapi import HTTPException, Request

from bernstein.core.webhook_signatures import verify_hmac_sha256

logger = logging.getLogger(__name__)

# Default header name for the signature
DEFAULT_SIGNATURE_HEADER = "X-Signature-256"

# Default prefix on the signature value
DEFAULT_SIGNATURE_PREFIX = "sha256="


class WebhookSignatureVerifier:
    """Callable dependency that verifies HMAC-SHA256 webhook signatures.

    Usage as a FastAPI dependency::

        verifier = WebhookSignatureVerifier(secret="my-secret")

        @router.post("/webhook/inbound")
        async def handle(request: Request, _=Depends(verifier)):
            ...
    """

    def __init__(
        self,
        *,
        secret: str = "",
        header: str = DEFAULT_SIGNATURE_HEADER,
        prefix: str = DEFAULT_SIGNATURE_PREFIX,
        secret_env_var: str = "BERNSTEIN_WEBHOOK_SECRET",
    ) -> None:
        self._secret = secret
        self._header = header
        self._prefix = prefix
        self._secret_env_var = secret_env_var

    def _resolve_secret(self, request: Request) -> str:
        """Resolve the webhook secret from constructor, app state, or env."""
        if self._secret:
            return self._secret
        # Try app state
        state_secret = getattr(request.app.state, "webhook_secret", None)
        if isinstance(state_secret, str) and state_secret:
            return state_secret
        # Try environment
        import os

        return os.environ.get(self._secret_env_var, "")

    async def __call__(self, request: Request) -> None:
        """Verify the webhook signature or raise 401/403."""
        secret = self._resolve_secret(request)
        if not secret:
            # No secret configured — skip verification (dev mode)
            logger.debug("Webhook verification skipped: no secret configured")
            return

        signature = request.headers.get(self._header, "")
        if not signature:
            raise HTTPException(
                status_code=401,
                detail=f"Missing {self._header} header",
            )

        body = await request.body()
        if not verify_hmac_sha256(body, signature, secret, prefix=self._prefix):
            raise HTTPException(
                status_code=403,
                detail="Invalid webhook signature",
            )


def verify_webhook_request(
    payload: bytes,
    signature: str,
    secret: str,
    *,
    prefix: str = DEFAULT_SIGNATURE_PREFIX,
) -> bool:
    """Convenience function for verifying a webhook payload.

    Args:
        payload: Raw request body bytes.
        signature: Signature header value.
        secret: Shared webhook secret.
        prefix: Signature prefix (default ``sha256=``).

    Returns:
        True if the signature is valid.
    """
    return verify_hmac_sha256(payload, signature, secret, prefix=prefix)
