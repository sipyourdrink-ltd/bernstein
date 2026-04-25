"""HTTP webhook listener for ``pull_request_review_comment`` events.

The listener exposes a single FastAPI route, ``POST /webhook``, that
verifies the ``X-Hub-Signature-256`` HMAC and forwards normalised
comments to the bundler.  It does not own its public URL — the caller
is expected to launch a tunnel via the v1.8.15 :mod:`bernstein.tunnels`
wrapper and point GitHub at the resulting public address.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from fastapi import FastAPI

logger = logging.getLogger(__name__)

#: HTTP header GitHub uses to carry the HMAC-SHA256 signature.
SIGNATURE_HEADER = "X-Hub-Signature-256"

#: HTTP header GitHub uses to label the event type.
EVENT_HEADER = "X-GitHub-Event"

#: Event name we respond to.  Other event types are ignored with HTTP 202.
TARGET_EVENT = "pull_request_review_comment"


def verify_signature(*, secret: bytes, body: bytes, signature: str | None) -> bool:
    """Validate a GitHub HMAC-SHA256 signature in constant time.

    Args:
        secret: The webhook secret bytes (must not be empty).
        body: Raw request body as received on the wire.
        signature: Value of the ``X-Hub-Signature-256`` header, e.g.
            ``"sha256=abcd..."``.  ``None`` or non-``sha256=`` values
            return ``False``.

    Returns:
        ``True`` if the signature matches, ``False`` otherwise.  An empty
        ``secret`` always returns ``False`` — a misconfigured listener
        must not silently accept unsigned traffic.
    """
    if not secret or not signature:
        return False
    if not signature.startswith("sha256="):
        return False
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    received = signature.split("=", 1)[1].strip()
    return hmac.compare_digest(expected, received)


class WebhookListener:
    """FastAPI app that ingests review-comment webhooks.

    The listener intentionally does not own its event loop — callers
    embed it in their own ``uvicorn`` runner so the daemon command can
    bind/unbind the port alongside other services.

    Args:
        secret: Webhook secret bytes used to verify ``X-Hub-Signature-256``.
            Must be non-empty; an empty secret is treated as a hard
            misconfiguration and raises :class:`ValueError`.
        on_comment: Callback invoked for every successfully verified
            comment.  Should be cheap — heavy work belongs in the bundler.

    Raises:
        ValueError: If ``secret`` is empty.
    """

    def __init__(
        self,
        *,
        secret: bytes,
        on_comment: Callable[[Mapping[str, Any]], None],
    ) -> None:
        """Build the FastAPI app and register the ``POST /webhook`` route."""
        if not secret:
            raise ValueError("WebhookListener secret must be non-empty")
        self._secret = secret
        self._on_comment = on_comment
        self._app = self._build_app()

    @property
    def app(self) -> FastAPI:
        """Return the FastAPI app instance for embedding in a runner."""
        return self._app

    def _build_app(self) -> FastAPI:
        """Construct the FastAPI app with the ``/webhook`` route registered.

        The endpoint takes the raw request via ``starlette.requests.Request``.
        FastAPI introspects parameter annotations at registration time, so we
        register the route via ``app.add_api_route`` with explicit ``methods=``
        rather than the decorator form — this lets us pre-resolve the Request
        annotation from the function's closure, sidestepping the
        ``from __future__ import annotations`` lazy-string issue that otherwise
        causes FastAPI to misclassify ``request`` as a query parameter.
        """
        # FastAPI is imported lazily so importing this module never pulls
        # the framework into processes that only need ``verify_signature``.
        from fastapi import FastAPI
        from fastapi.responses import JSONResponse
        from starlette.requests import Request as _Request

        app = FastAPI(title="bernstein-review-responder")
        secret = self._secret
        on_comment = self._on_comment

        async def webhook(request: _Request) -> JSONResponse:
            body = await request.body()
            signature = request.headers.get(SIGNATURE_HEADER)
            if not verify_signature(secret=secret, body=body, signature=signature):
                logger.warning(
                    "Rejected webhook: signature mismatch (event=%s)",
                    request.headers.get(EVENT_HEADER, "unknown"),
                )
                return JSONResponse({"error": "invalid signature"}, status_code=401)

            event = request.headers.get(EVENT_HEADER, "")
            if event != TARGET_EVENT:
                # Acknowledge but ignore — keeps GitHub from retrying.
                return JSONResponse({"status": "ignored", "event": event}, status_code=202)

            try:
                payload = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError) as exc:
                logger.warning("Rejected webhook: payload not JSON (%s)", type(exc).__name__)
                return JSONResponse({"error": "invalid payload"}, status_code=400)

            try:
                on_comment(payload)
            except Exception:  # pragma: no cover - logged, not re-raised
                logger.exception("on_comment callback raised — comment dropped")
                return JSONResponse({"status": "error"}, status_code=500)
            return JSONResponse({"status": "queued"}, status_code=202)

        # Force-resolve the annotation against the local _Request class so
        # FastAPI sees the real type (not a deferred string).
        webhook.__annotations__["request"] = _Request
        app.add_api_route("/webhook", webhook, methods=["POST"])
        return app
