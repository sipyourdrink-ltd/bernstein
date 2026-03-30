"""Periodic provider quota polling and persistence."""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.quota_probe import QuotaSnapshot, probe_provider_quota
from bernstein.core.router import ProviderHealthStatus, TierAwareRouter

if TYPE_CHECKING:
    from pathlib import Path


@dataclass
class QuotaPoller:
    """Refresh provider quota metadata on a fixed interval."""

    router: TierAwareRouter
    workdir: Path
    interval_seconds: float = 300.0
    _last_polled_at: float = 0.0

    def maybe_poll(self) -> bool:
        """Poll only when the interval has elapsed."""
        now = time.time()
        if self._last_polled_at and (now - self._last_polled_at) < self.interval_seconds:
            return False
        self._last_polled_at = now
        self.poll_once()
        return True

    def poll_once(self) -> dict[str, Any]:
        """Refresh provider snapshots and persist a status artifact."""
        providers_payload: dict[str, Any] = {}
        generated_at = time.time()
        for provider_name, provider_cfg in self.router.state.providers.items():
            model_name = next(iter(provider_cfg.models), "")
            snapshot = probe_provider_quota(provider_name, model_name)
            if snapshot is not None:
                snapshot = QuotaSnapshot(
                    provider=snapshot.provider,
                    model=snapshot.model,
                    source=snapshot.source,
                    observed_at=snapshot.observed_at,
                    available=snapshot.available
                    and provider_cfg.available
                    and provider_cfg.health.status != ProviderHealthStatus.RATE_LIMITED,
                    requests_per_minute=snapshot.requests_per_minute,
                    requests_per_day=snapshot.requests_per_day,
                    tokens_per_minute=snapshot.tokens_per_minute,
                    tokens_per_day=snapshot.tokens_per_day,
                    reset_at=snapshot.reset_at,
                    detail=snapshot.detail,
                )
            provider_cfg.quota_snapshot = snapshot
            providers_payload[provider_name] = {
                "health": provider_cfg.health.status.value,
                "available": provider_cfg.available,
                "tier": provider_cfg.tier.value,
                "model": model_name,
                "quota_snapshot": snapshot.to_dict() if snapshot is not None else None,
            }

        payload = {"generated_at": generated_at, "providers": providers_payload}
        path = self.workdir / ".sdd" / "runtime" / "provider_status.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
        self._last_polled_at = generated_at
        return payload
