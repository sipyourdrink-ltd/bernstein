"""Provider quota probes."""

from __future__ import annotations

import logging
import time
from dataclasses import asdict, dataclass

from bernstein.adapters.registry import get_adapter

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class QuotaSnapshot:
    """Best-effort provider quota snapshot for status and routing."""

    provider: str
    model: str
    source: str
    observed_at: float
    available: bool = True
    requests_per_minute: int | None = None
    requests_per_day: int | None = None
    tokens_per_minute: int | None = None
    tokens_per_day: int | None = None
    reset_at: float | None = None
    detail: str | None = None

    def to_dict(self) -> dict[str, object]:
        """Serialize the snapshot for JSON status output."""
        return asdict(self)


def probe_provider_quota(provider: str, model: str) -> QuotaSnapshot | None:
    """Return a best-effort quota snapshot for *provider* and *model*."""
    try:
        adapter = get_adapter(provider)
    except Exception as exc:
        logger.debug("Quota probe skipped for provider %s: %s", provider, exc)
        return None

    try:
        tier_info = adapter.detect_tier()
    except Exception as exc:
        logger.debug("Quota probe failed for provider %s: %s", provider, exc)
        return None

    if tier_info is None:
        return None

    rate_limit = tier_info.rate_limit
    return QuotaSnapshot(
        provider=provider,
        model=model,
        source="adapter.detect_tier",
        observed_at=time.time(),
        available=tier_info.is_active,
        requests_per_minute=rate_limit.requests_per_minute if rate_limit else None,
        requests_per_day=rate_limit.requests_per_day if rate_limit else None,
        tokens_per_minute=rate_limit.tokens_per_minute if rate_limit else None,
        tokens_per_day=rate_limit.tokens_per_day if rate_limit else None,
        detail=tier_info.tier.value,
    )
