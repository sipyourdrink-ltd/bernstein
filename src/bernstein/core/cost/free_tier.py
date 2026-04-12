"""Free Tier Maximizer - automatically detect and use free tier quotas.

Tracks remaining free tier quotas per provider and routes tasks to free
tier providers first before using paid tiers.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# Free tier limits (approximate, as of 2026)
FREE_TIER_LIMITS = {
    "gemini": {
        "requests_per_day": 1500,
        "requests_per_minute": 15,
        "description": "Gemini API generous free tier",
    },
    "codex": {
        "requests_per_day": 50,
        "requests_per_minute": 3,
        "description": "Codex CLI free tier",
    },
    "qwen": {
        "requests_per_day": 1000,
        "requests_per_minute": 10,
        "description": "Qwen API free tier",
    },
}


@dataclass
class FreeTierStatus:
    """Status of a provider's free tier quota."""

    provider: str
    remaining_today: int
    limit_today: int
    remaining_minute: int
    limit_minute: int
    reset_time: float | None = None  # Unix timestamp
    last_updated: float = field(default_factory=time.time)

    @property
    def utilization_pct(self) -> float:
        """Get quota utilization percentage."""
        if self.limit_today == 0:
            return 0.0
        return ((self.limit_today - self.remaining_today) / self.limit_today) * 100

    @property
    def is_available(self) -> bool:
        """Check if free tier is still available."""
        return self.remaining_today > 0 and self.remaining_minute > 0

    def to_dict(self) -> dict[str, Any]:
        """Serialize to dictionary."""
        return {
            "provider": self.provider,
            "remaining_today": self.remaining_today,
            "limit_today": self.limit_today,
            "remaining_minute": self.remaining_minute,
            "limit_minute": self.limit_minute,
            "utilization_pct": round(self.utilization_pct, 1),
            "is_available": self.is_available,
            "last_updated": self.last_updated,
        }


class FreeTierMaximizer:
    """Maximize free tier usage before falling back to paid tiers.

    Tracks free tier quotas and provides routing recommendations.

    Args:
        workdir: Project working directory for state persistence.
    """

    STATE_FILE = "free_tier_state.json"

    def __init__(self, workdir: Path) -> None:
        self._workdir = workdir
        self._state_path = workdir / ".sdd" / "state" / self.STATE_FILE
        self._statuses: dict[str, FreeTierStatus] = {}
        self._load_state()

    def _load_state(self) -> None:
        """Load free tier state from disk."""
        import json

        if not self._state_path.exists():
            for provider, limits in FREE_TIER_LIMITS.items():
                self._statuses[provider] = FreeTierStatus(
                    provider=provider,
                    remaining_today=int(limits["requests_per_day"]),
                    limit_today=int(limits["requests_per_day"]),
                    remaining_minute=int(limits["requests_per_minute"]),
                    limit_minute=int(limits["requests_per_minute"]),
                )
            return

        try:
            data = json.loads(self._state_path.read_text(encoding="utf-8"))
            for provider, status_data in data.get("providers", {}).items():
                self._statuses[provider] = FreeTierStatus(
                    provider=provider,
                    remaining_today=int(status_data.get("remaining_today", 0)),
                    limit_today=int(status_data.get("limit_today", 0)),
                    remaining_minute=int(status_data.get("remaining_minute", 0)),
                    limit_minute=int(status_data.get("limit_minute", 0)),
                    reset_time=status_data.get("reset_time"),
                    last_updated=float(status_data.get("last_updated", time.time())),
                )
            logger.info("Loaded free tier state from %s", self._state_path)
        except Exception as exc:
            logger.warning("Failed to load free tier state: %s", exc)
            # Initialize with defaults on error
            self._load_state()

    def _save_state(self) -> None:
        """Save free tier state to disk."""
        import json

        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "last_saved": time.time(),
            "providers": {provider: status.to_dict() for provider, status in self._statuses.items()},
        }
        self._state_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def record_request(self, provider: str) -> None:
        """Record a free tier request for a provider.

        Args:
            provider: Provider name.
        """
        if provider not in self._statuses:
            return

        status = self._statuses[provider]
        status.remaining_today = max(0, status.remaining_today - 1)
        status.remaining_minute = max(0, status.remaining_minute - 1)
        status.last_updated = time.time()

        self._save_state()

        if status.remaining_today == 0:
            logger.warning("Free tier exhausted for %s", provider)

    def get_best_free_provider(self) -> str | None:
        """Get the best available free tier provider.

        Returns provider with highest remaining quota.

        Returns:
            Provider name or None if no free tier available.
        """
        available = [
            (provider, status.remaining_today) for provider, status in self._statuses.items() if status.is_available
        ]

        if not available:
            return None

        # Sort by remaining quota (descending)
        available.sort(key=lambda x: x[1], reverse=True)
        return available[0][0]

    def should_use_free_tier(self, provider: str) -> bool:
        """Check if a provider's free tier should be used.

        Args:
            provider: Provider name.

        Returns:
            True if free tier is available and recommended.
        """
        if provider not in self._statuses:
            return False

        status = self._statuses[provider]

        # Use free tier if:
        # 1. Still available
        # 2. Utilization < 80% (leave headroom)
        return status.is_available and status.utilization_pct < 80

    def get_all_statuses(self) -> list[FreeTierStatus]:
        """Get status for all providers.

        Returns:
            List of FreeTierStatus instances.
        """
        return list(self._statuses.values())

    def get_summary(self) -> dict[str, Any]:
        """Get free tier utilization summary.

        Returns:
            Summary dictionary.
        """
        total_remaining = sum(s.remaining_today for s in self._statuses.values())
        total_limit = sum(s.limit_today for s in self._statuses.values())
        available_count = sum(1 for s in self._statuses.values() if s.is_available)

        return {
            "total_remaining": total_remaining,
            "total_limit": total_limit,
            "overall_utilization_pct": round(((total_limit - total_remaining) / max(1, total_limit)) * 100, 1),
            "available_providers": available_count,
            "total_providers": len(self._statuses),
            "providers": {provider: status.to_dict() for provider, status in self._statuses.items()},
        }

    def reset_daily_limits(self) -> None:
        """Reset daily limits (call this once per day)."""
        now = time.time()
        for status in self._statuses.values():
            # Check if reset time has passed
            if status.reset_time and now >= status.reset_time:
                status.remaining_today = status.limit_today
                status.remaining_minute = status.limit_minute
                status.reset_time = now + (24 * 60 * 60)  # Next reset in 24h
                logger.info("Reset daily free tier limits for %s", status.provider)

        self._save_state()
