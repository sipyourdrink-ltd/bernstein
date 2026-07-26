"""FastAPI routes for telemetry-grounded autofix webhooks.

The router exposes one endpoint per built-in adapter under
``/webhooks/telemetry/<source>/``. Every endpoint follows the same
pipeline:

1. Read the raw body (so HMAC verification can run before parsing).
2. Verify the upstream signature against ``secret_env`` from
   :class:`TelemetrySourceConfig`. Empty secret disables the check
   (test-only mode).
3. Parse the payload, hand it to the source adapter, and drop the
   normalised :class:`TelemetryEvent` into
   :func:`dispatch_telemetry_event`.
4. Return a JSON response that mirrors the dispatch outcome so
   upstream platforms can render a clear delivery status.

The receiver state (settings, retriever, dispatch hook, audit) lives
on ``request.app.state.telemetry_grounded`` so tests can swap the
dispatcher without touching the router code.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from bernstein.core.autofix.telemetry_grounded import (
    GroundedDispatchHook,
    GroundingRetriever,
    TelemetryDispatchRecord,
    TelemetrySettings,
    TelemetrySource,
    TelemetrySourceId,
    build_default_sources,
    dispatch_telemetry_event,
    parse_json_payload,
    verify_webhook_signature,
)
from bernstein.core.routes._unconfigured import UNCONFIGURED_STATUS

if TYPE_CHECKING:
    from bernstein.core.autofix.telemetry_grounded import AuditEmitter

logger = logging.getLogger(__name__)
router = APIRouter()


@dataclass
class TelemetryReceiverState:
    """Container the router pulls from ``request.app.state``.

    Attributes:
        settings: Effective telemetry settings.
        sources: Source-id-to-adapter mapping.
        retriever: Grounding retriever shared across endpoints.
        dispatch_hook: Spawn callable; production wires this to the
            autofix daemon, tests inject a recorder.
        audit: Optional audit log. Best-effort emit.
    """

    settings: TelemetrySettings
    sources: dict[TelemetrySourceId, TelemetrySource]
    retriever: GroundingRetriever
    dispatch_hook: GroundedDispatchHook
    audit: AuditEmitter | None = None


def configure_receiver(
    *,
    app_state: object,
    settings: TelemetrySettings,
    retriever: GroundingRetriever,
    dispatch_hook: GroundedDispatchHook,
    audit: AuditEmitter | None = None,
) -> TelemetryReceiverState:
    """Install a :class:`TelemetryReceiverState` on ``app.state``.

    The function lets the server bootstrap (or a test) configure the
    receiver in one call. The returned state is the same object the
    router will reach for. ``TelemetrySettings`` and
    ``TelemetrySourceConfig`` are frozen, so runtime reconfiguration
    (e.g. flipping ``enabled`` on a source) requires building a new
    settings instance and calling :func:`configure_receiver` again to
    replace ``app.state.telemetry_grounded`` wholesale.

    Args:
        app_state: ``request.app.state`` or a ``SimpleNamespace`` from
            tests.
        settings: Effective settings.
        retriever: Grounding retriever.
        dispatch_hook: Spawn callable.
        audit: Optional audit emitter.

    Returns:
        The installed :class:`TelemetryReceiverState`.
    """
    state = TelemetryReceiverState(
        settings=settings,
        sources=build_default_sources(settings),
        retriever=retriever,
        dispatch_hook=dispatch_hook,
        audit=audit,
    )
    app_state.telemetry_grounded = state
    return state


def _record_to_response(record: TelemetryDispatchRecord) -> dict[str, object]:
    """Convert a dispatch record into the JSON response body."""
    return {
        "outcome": record.outcome,
        "source": record.source,
        "fingerprint": record.fingerprint,
        "retriever_id": record.retriever_id,
        "cost_usd": round(record.cost_usd, 6),
        "commit_sha": record.commit_sha,
        "log_lines": record.log_lines,
        "reason": record.reason,
    }


def _status_for_outcome(outcome: str) -> int:
    """Map dispatch outcome to a sensible HTTP status code."""
    if outcome == "dispatched":
        return 202
    if outcome == "errored":
        return 500
    # skipped / stubbed / cost_capped - accept but tell the upstream
    # we did not act on the event.
    return 202


async def _handle_source(
    request: Request,
    source: TelemetrySourceId,
) -> JSONResponse:
    """Shared body for every telemetry webhook route."""
    state: TelemetryReceiverState | None = getattr(
        request.app.state,
        "telemetry_grounded",
        None,
    )
    if state is None:
        return JSONResponse(
            status_code=UNCONFIGURED_STATUS,
            content={
                "detail": (
                    "telemetry-grounded autofix receiver is not configured; bootstrap must call configure_receiver()."
                ),
            },
        )

    raw_body = await request.body()
    cfg = state.settings.for_source(source)

    if cfg is not None and cfg.secret_env:
        secret = os.environ.get(cfg.secret_env, "")
        if not secret:
            return JSONResponse(
                status_code=UNCONFIGURED_STATUS,
                content={
                    "detail": (
                        f"telemetry source {source!r} requires secret_env {cfg.secret_env!r}; the env var is not set."
                    ),
                },
            )
        signature_header = _extract_signature_header(request)
        if not verify_webhook_signature(
            body=raw_body,
            signature_header=signature_header,
            secret=secret,
        ):
            return JSONResponse(
                status_code=401,
                content={"detail": f"invalid webhook signature for source {source!r}"},
            )

    try:
        payload = parse_json_payload(raw_body)
    except ValueError:
        # Do not echo the parser exception text back to the caller: it can
        # leak internal structure. Log server-side, return a generic 400.
        logger.warning("telemetry_webhooks: malformed payload for source %s", source)
        return JSONResponse(status_code=400, content={"detail": "malformed JSON payload"})

    adapter = state.sources.get(source)
    if adapter is None:
        return JSONResponse(
            status_code=404,
            content={"detail": f"no adapter registered for source {source!r}"},
        )

    try:
        event = adapter.parse(payload)
    # bot-ack: pre-existing-1723 (third-party adapter may raise anything)
    except Exception:
        # Full traceback is logged server-side; the response carries only a
        # generic message so adapter internals are never exposed to callers.
        logger.exception("telemetry_webhooks: adapter raised for %s", source)
        return JSONResponse(
            status_code=400,
            content={"detail": f"adapter {source!r} could not parse payload"},
        )

    record = dispatch_telemetry_event(
        event,
        settings=state.settings,
        retriever=state.retriever,
        dispatch_hook=state.dispatch_hook,
        audit=state.audit,
    )
    return JSONResponse(
        status_code=_status_for_outcome(record.outcome),
        content=_record_to_response(record),
    )


def _extract_signature_header(request: Request) -> str:
    """Return the first telemetry-style signature header on the request.

    Different upstreams use different header names. The receiver
    accepts the union; the first non-empty value wins.
    """
    headers_lower = {k.lower(): v for k, v in request.headers.items()}
    for header_name in (
        "sentry-hook-signature",  # Sentry-protocol integrations
        "x-hub-signature-256",  # GitHub workflow_run webhook
        "x-datadog-signature",  # Datadog
        "x-bernstein-telemetry-signature",  # custom / loki / jsonl
    ):
        value = headers_lower.get(header_name, "")
        if value:
            return value
    return ""


@router.post("/webhooks/telemetry/sentry/")
async def telemetry_sentry(request: Request) -> JSONResponse:
    """Receive a Sentry-protocol issue-alert webhook."""
    return await _handle_source(request, "sentry")


@router.post("/webhooks/telemetry/gha_failure/")
async def telemetry_gha_failure(request: Request) -> JSONResponse:
    """Receive a GitHub Actions ``workflow_run`` failure webhook."""
    return await _handle_source(request, "gha_failure")


@router.post("/webhooks/telemetry/datadog/")
async def telemetry_datadog(request: Request) -> JSONResponse:
    """Receive a Datadog Logs webhook (stubbed in MVP)."""
    return await _handle_source(request, "datadog")


@router.post("/webhooks/telemetry/loki/")
async def telemetry_loki(request: Request) -> JSONResponse:
    """Receive a Loki / Alertmanager webhook (stubbed in MVP)."""
    return await _handle_source(request, "loki")


@router.post("/webhooks/telemetry/custom_jsonl/")
async def telemetry_custom_jsonl(request: Request) -> JSONResponse:
    """Receive a custom JSONL tail webhook (stubbed in MVP)."""
    return await _handle_source(request, "custom_jsonl")


__all__ = [
    "TelemetryReceiverState",
    "configure_receiver",
    "router",
]
