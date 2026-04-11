"""Provider latency routes — historical percentile charts (ROAD-155).

GET /metrics/provider-latency          — current p50/p95/p99 per provider+model
GET /metrics/provider-latency/history  — raw samples for time-series charting
"""

from __future__ import annotations

import time
from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse

from bernstein.core.provider_latency import get_tracker

router = APIRouter()


def _get_metrics_dir(request: Request) -> Any:
    sdd_dir = getattr(request.app.state, "sdd_dir", None)
    if sdd_dir is not None:
        return sdd_dir / "metrics"
    return None


@router.get("/metrics/provider-latency")
def provider_latency_current(request: Request) -> JSONResponse:
    """Return current p50/p95/p99 latency percentiles for all tracked providers.

    Each entry in the response includes a ``baseline_p99_ms`` derived from the
    past 7 days of data. When ``p99_ms`` exceeds ``baseline_p99_ms × 2``, the
    entry carries ``"degraded": true``.
    """
    metrics_dir = _get_metrics_dir(request)
    tracker = get_tracker(metrics_dir)
    entries = tracker.all_percentiles()

    data = []
    for p in entries:
        d = p.to_dict()
        degraded = (
            p.baseline_p99_ms > 0
            and p.sample_count >= 10
            and p.p99_ms >= p.baseline_p99_ms * 2.0
        )
        d["degraded"] = degraded
        data.append(d)

    return JSONResponse(
        {
            "timestamp": time.time(),
            "providers": data,
            "count": len(data),
        }
    )


@router.get("/metrics/provider-latency/history")
def provider_latency_history(
    request: Request,
    provider: str | None = Query(default=None, description="Filter by provider name"),
    model: str | None = Query(default=None, description="Filter by model identifier"),
    hours: int = Query(default=24, ge=1, le=168, description="Hours of history to return (1–168)"),
) -> JSONResponse:
    """Return raw latency samples for time-series charting.

    Each sample has: ``timestamp``, ``provider``, ``model``, ``latency_ms``.
    Samples are ordered chronologically. Use ``hours`` to control the lookback
    window (default 24h, max 7 days).
    """
    metrics_dir = _get_metrics_dir(request)
    tracker = get_tracker(metrics_dir)
    samples = tracker.get_history(provider=provider, model=model, hours=hours)

    return JSONResponse(
        {
            "timestamp": time.time(),
            "provider_filter": provider,
            "model_filter": model,
            "hours": hours,
            "samples": samples,
            "count": len(samples),
        }
    )
