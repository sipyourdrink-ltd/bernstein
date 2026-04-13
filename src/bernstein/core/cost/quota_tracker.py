"""API quota utilization tracker.

Tracks per-provider request and token usage against known limits,
emits alerts at configurable thresholds, and provides a throttle
signal for the orchestrator.  Usage records are persisted to
``.sdd/metrics/quota.jsonl`` so that TUI dashboards and the
``GET /status`` endpoint can report quota headroom.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from datetime import datetime
    from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Alert thresholds (utilization percentage)
# ---------------------------------------------------------------------------

_ALERT_INFO_PCT: float = 70.0
_ALERT_WARNING_PCT: float = 85.0
_ALERT_CRITICAL_PCT: float = 95.0
_THROTTLE_PCT: float = 90.0


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QuotaStatus:
    """Current API quota utilization for a single provider.

    Attributes:
        provider: Provider identifier (e.g. ``"anthropic"``, ``"openai"``).
        requests_used: Number of requests consumed in current window.
        requests_limit: Maximum requests allowed in current window.
        tokens_used: Total tokens consumed in current window.
        tokens_limit: Maximum tokens allowed in current window.
        utilization_pct: Highest of request-% and token-% utilization.
        resets_at: When the current quota window resets, if known.
        tier: Provider tier (``"free"``, ``"pro"``, ``"max"``, ``"enterprise"``).
    """

    provider: str
    requests_used: int
    requests_limit: int
    tokens_used: int
    tokens_limit: int
    utilization_pct: float
    resets_at: datetime | None
    tier: str


@dataclass(frozen=True)
class QuotaAlert:
    """An alert fired when a provider approaches its quota limit.

    Attributes:
        provider: Provider that triggered the alert.
        message: Human-readable description of the alert.
        severity: One of ``"info"``, ``"warning"``, ``"critical"``.
        utilization_pct: Current utilization when the alert was generated.
    """

    provider: str
    message: str
    severity: str
    utilization_pct: float


# ---------------------------------------------------------------------------
# Internal mutable state per provider
# ---------------------------------------------------------------------------


@dataclass
class _ProviderAccum:
    """Mutable accumulator for a single provider's quota window."""

    requests_used: int = 0
    tokens_used: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    requests_limit: int = 0
    tokens_limit: int = 0
    resets_at: datetime | None = None
    tier: str = "free"


# ---------------------------------------------------------------------------
# QuotaTracker
# ---------------------------------------------------------------------------


class QuotaTracker:
    """Track API quota usage across providers.

    Thread-safe: all mutations are protected by a :class:`threading.Lock`.

    Args:
        sdd_root: Path to the ``.sdd`` directory.  Pass ``None`` to
            disable persistence (useful in tests).
    """

    def __init__(self, sdd_root: Path | None = None) -> None:
        self._lock = threading.Lock()
        self._providers: dict[str, _ProviderAccum] = {}
        self._sdd_root = sdd_root

    # -- configuration -------------------------------------------------------

    def configure_provider(
        self,
        provider: str,
        *,
        requests_limit: int = 0,
        tokens_limit: int = 0,
        resets_at: datetime | None = None,
        tier: str = "free",
    ) -> None:
        """Set or update limits for *provider*.

        Args:
            provider: Provider identifier.
            requests_limit: Max requests in the current window.
            tokens_limit: Max tokens in the current window.
            resets_at: When the window resets.
            tier: Provider tier.
        """
        with self._lock:
            acc = self._providers.setdefault(provider, _ProviderAccum())
            acc.requests_limit = requests_limit
            acc.tokens_limit = tokens_limit
            acc.resets_at = resets_at
            acc.tier = tier

    # -- recording -----------------------------------------------------------

    def record_request(
        self,
        provider: str,
        tokens_in: int,
        tokens_out: int,
        cache_read: int = 0,
        cache_write: int = 0,
    ) -> None:
        """Record an API request for *provider*.

        Args:
            provider: Provider identifier.
            tokens_in: Input tokens consumed.
            tokens_out: Output tokens consumed.
            cache_read: Tokens served from cache.
            cache_write: Tokens written to cache.
        """
        with self._lock:
            acc = self._providers.setdefault(provider, _ProviderAccum())
            acc.requests_used += 1
            acc.tokens_used += tokens_in + tokens_out
            acc.cache_read_tokens += cache_read
            acc.cache_write_tokens += cache_write

        self._persist(provider, tokens_in, tokens_out, cache_read, cache_write)

    # -- queries -------------------------------------------------------------

    def get_status(self, provider: str) -> QuotaStatus:
        """Get current quota utilization for *provider*.

        Args:
            provider: Provider identifier.

        Returns:
            A frozen :class:`QuotaStatus` snapshot.
        """
        with self._lock:
            acc = self._providers.get(provider, _ProviderAccum())
            return self._status_from_accum(provider, acc)

    def get_all_statuses(self) -> list[QuotaStatus]:
        """Get quota status for all tracked providers.

        Returns:
            List of :class:`QuotaStatus`, one per configured provider.
        """
        with self._lock:
            return [self._status_from_accum(p, a) for p, a in sorted(self._providers.items())]

    def check_alerts(self) -> list[QuotaAlert]:
        """Check if any provider is approaching its limits.

        Alerts are generated at 70% (info), 85% (warning), and
        95% (critical) utilization.

        Returns:
            List of :class:`QuotaAlert` for providers that exceed thresholds.
        """
        alerts: list[QuotaAlert] = []
        with self._lock:
            for provider, acc in sorted(self._providers.items()):
                pct = self._utilization_pct(acc)
                if pct >= _ALERT_CRITICAL_PCT:
                    alerts.append(
                        QuotaAlert(
                            provider=provider,
                            message=f"{provider} quota at {pct:.0f}% — critical",
                            severity="critical",
                            utilization_pct=pct,
                        )
                    )
                elif pct >= _ALERT_WARNING_PCT:
                    alerts.append(
                        QuotaAlert(
                            provider=provider,
                            message=f"{provider} quota at {pct:.0f}% — warning",
                            severity="warning",
                            utilization_pct=pct,
                        )
                    )
                elif pct >= _ALERT_INFO_PCT:
                    alerts.append(
                        QuotaAlert(
                            provider=provider,
                            message=f"{provider} quota at {pct:.0f}% — info",
                            severity="info",
                            utilization_pct=pct,
                        )
                    )
        return alerts

    def should_throttle(self, provider: str) -> bool:
        """Return ``True`` if *provider* is at >90% utilization.

        Args:
            provider: Provider identifier.

        Returns:
            Whether requests to this provider should be throttled.
        """
        with self._lock:
            acc = self._providers.get(provider)
            if acc is None:
                return False
            return self._utilization_pct(acc) > _THROTTLE_PCT

    def render_status_line(self) -> str:
        """One-line status string for TUI display.

        Example::

            Anthropic: 42% | OpenAI: 18% | cache: 72%

        Returns:
            Formatted status line.
        """
        parts: list[str] = []
        total_cache_read = 0
        total_tokens = 0
        with self._lock:
            for provider, acc in sorted(self._providers.items()):
                pct = self._utilization_pct(acc)
                parts.append(f"{provider}: {pct:.0f}%")
                total_cache_read += acc.cache_read_tokens
                total_tokens += acc.tokens_used
        if total_tokens > 0:
            cache_pct = (total_cache_read / total_tokens) * 100.0
            parts.append(f"cache: {cache_pct:.0f}%")
        return " | ".join(parts) if parts else "no providers tracked"

    # -- internals -----------------------------------------------------------

    @staticmethod
    def _utilization_pct(acc: _ProviderAccum) -> float:
        """Compute utilization percentage as the max of request and token %.

        Args:
            acc: Provider accumulator.

        Returns:
            Utilization percentage (0.0-100.0+).
        """
        req_pct = (acc.requests_used / acc.requests_limit * 100.0) if acc.requests_limit else 0.0
        tok_pct = (acc.tokens_used / acc.tokens_limit * 100.0) if acc.tokens_limit else 0.0
        return max(req_pct, tok_pct)

    @staticmethod
    def _status_from_accum(provider: str, acc: _ProviderAccum) -> QuotaStatus:
        """Build a frozen :class:`QuotaStatus` from a mutable accumulator.

        Args:
            provider: Provider identifier.
            acc: Provider accumulator.

        Returns:
            Frozen status snapshot.
        """
        req_pct = (acc.requests_used / acc.requests_limit * 100.0) if acc.requests_limit else 0.0
        tok_pct = (acc.tokens_used / acc.tokens_limit * 100.0) if acc.tokens_limit else 0.0
        return QuotaStatus(
            provider=provider,
            requests_used=acc.requests_used,
            requests_limit=acc.requests_limit,
            tokens_used=acc.tokens_used,
            tokens_limit=acc.tokens_limit,
            utilization_pct=max(req_pct, tok_pct),
            resets_at=acc.resets_at,
            tier=acc.tier,
        )

    def _persist(
        self,
        provider: str,
        tokens_in: int,
        tokens_out: int,
        cache_read: int,
        cache_write: int,
    ) -> None:
        """Append a usage record to ``.sdd/metrics/quota.jsonl``.

        Args:
            provider: Provider identifier.
            tokens_in: Input tokens.
            tokens_out: Output tokens.
            cache_read: Cache-read tokens.
            cache_write: Cache-write tokens.
        """
        if self._sdd_root is None:
            return
        metrics_dir = self._sdd_root / "metrics"
        try:
            metrics_dir.mkdir(parents=True, exist_ok=True)
            record = {
                "ts": time.time(),
                "provider": provider,
                "tokens_in": tokens_in,
                "tokens_out": tokens_out,
                "cache_read": cache_read,
                "cache_write": cache_write,
            }
            with (metrics_dir / "quota.jsonl").open("a") as fh:
                fh.write(json.dumps(record) + "\n")
        except OSError:
            logger.debug("Failed to persist quota record", exc_info=True)
