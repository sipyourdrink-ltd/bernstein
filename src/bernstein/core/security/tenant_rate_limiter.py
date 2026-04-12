"""ENT-008: Per-tenant rate limiting and quota enforcement.

Provides tenant-scoped rate limiting with configurable quotas per tenant.
Each tenant gets independent rate limit windows, task quotas, and agent
concurrency limits.  Exceeding any limit returns a structured denial with
retry-after information.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_DEFAULT_REQUESTS_PER_MINUTE = 60
_DEFAULT_TASKS_PER_HOUR = 100
_DEFAULT_MAX_CONCURRENT_AGENTS = 6
_WINDOW_SECONDS = 60.0


class QuotaKind(StrEnum):
    """Types of quota that can be enforced."""

    API_REQUESTS = "api_requests"
    TASKS_PER_HOUR = "tasks_per_hour"
    CONCURRENT_AGENTS = "concurrent_agents"
    STORAGE_BYTES = "storage_bytes"


class DenialReason(StrEnum):
    """Reason a request was denied."""

    RATE_LIMITED = "rate_limited"
    QUOTA_EXCEEDED = "quota_exceeded"
    CONCURRENCY_EXCEEDED = "concurrency_exceeded"
    TENANT_SUSPENDED = "tenant_suspended"


@dataclass(frozen=True)
class TenantQuotaConfig:
    """Per-tenant quota configuration.

    Attributes:
        tenant_id: Tenant identifier.
        requests_per_minute: API request rate limit.
        tasks_per_hour: Maximum tasks created per hour.
        max_concurrent_agents: Maximum agents running concurrently.
        max_storage_bytes: Maximum storage usage in bytes (0 = unlimited).
        suspended: Whether the tenant is suspended.
    """

    tenant_id: str = ""
    requests_per_minute: int = _DEFAULT_REQUESTS_PER_MINUTE
    tasks_per_hour: int = _DEFAULT_TASKS_PER_HOUR
    max_concurrent_agents: int = _DEFAULT_MAX_CONCURRENT_AGENTS
    max_storage_bytes: int = 0
    suspended: bool = False


@dataclass(frozen=True)
class QuotaDenial:
    """Structured denial response when a quota is exceeded.

    Attributes:
        tenant_id: Affected tenant.
        reason: Why the request was denied.
        quota_kind: Which quota was hit.
        limit: The configured limit value.
        current: Current usage value.
        retry_after_s: Suggested seconds to wait before retrying.
        message: Human-readable denial message.
    """

    tenant_id: str = ""
    reason: DenialReason = DenialReason.RATE_LIMITED
    quota_kind: QuotaKind = QuotaKind.API_REQUESTS
    limit: int = 0
    current: int = 0
    retry_after_s: float = 0.0
    message: str = ""


@dataclass
class TenantUsageSnapshot:
    """Current usage counters for a tenant.

    Attributes:
        tenant_id: Tenant identifier.
        request_timestamps: Timestamps of recent API requests.
        task_timestamps: Timestamps of recent task creations.
        concurrent_agents: Current number of running agents.
        storage_bytes: Current storage usage.
    """

    tenant_id: str = ""
    request_timestamps: list[float] = field(default_factory=list[float])
    task_timestamps: list[float] = field(default_factory=list[float])
    concurrent_agents: int = 0
    storage_bytes: int = 0


# ---------------------------------------------------------------------------
# Rate limiter engine
# ---------------------------------------------------------------------------


class TenantRateLimiter:
    """Per-tenant rate limiting and quota enforcement engine.

    Maintains sliding-window counters per tenant and checks requests
    against configured quotas.

    Args:
        default_config: Default quota config for unknown tenants.
    """

    def __init__(
        self,
        default_config: TenantQuotaConfig | None = None,
    ) -> None:
        self._default = default_config or TenantQuotaConfig()
        self._configs: dict[str, TenantQuotaConfig] = {}
        self._usage: dict[str, TenantUsageSnapshot] = {}

    def set_tenant_config(self, config: TenantQuotaConfig) -> None:
        """Register or update quota configuration for a tenant.

        Args:
            config: Tenant-specific quota configuration.
        """
        self._configs[config.tenant_id] = config

    def get_tenant_config(self, tenant_id: str) -> TenantQuotaConfig:
        """Get quota configuration for a tenant, falling back to defaults.

        Args:
            tenant_id: Tenant identifier.

        Returns:
            Tenant-specific or default quota configuration.
        """
        return self._configs.get(tenant_id, self._default)

    def _get_usage(self, tenant_id: str) -> TenantUsageSnapshot:
        """Get or create usage snapshot for a tenant."""
        if tenant_id not in self._usage:
            self._usage[tenant_id] = TenantUsageSnapshot(tenant_id=tenant_id)
        return self._usage[tenant_id]

    def _prune_timestamps(
        self,
        timestamps: list[float],
        window_s: float,
        now: float,
    ) -> list[float]:
        """Remove timestamps outside the sliding window."""
        cutoff = now - window_s
        return [t for t in timestamps if t > cutoff]

    def check_api_rate(self, tenant_id: str) -> QuotaDenial | None:
        """Check if an API request is within the rate limit.

        Args:
            tenant_id: Tenant making the request.

        Returns:
            QuotaDenial if rate limited, None if allowed.
        """
        config = self.get_tenant_config(tenant_id)
        if config.suspended:
            return QuotaDenial(
                tenant_id=tenant_id,
                reason=DenialReason.TENANT_SUSPENDED,
                quota_kind=QuotaKind.API_REQUESTS,
                limit=0,
                current=0,
                retry_after_s=0.0,
                message=f"Tenant {tenant_id} is suspended",
            )

        now = time.time()
        usage = self._get_usage(tenant_id)
        usage.request_timestamps = self._prune_timestamps(
            usage.request_timestamps,
            _WINDOW_SECONDS,
            now,
        )

        if len(usage.request_timestamps) >= config.requests_per_minute:
            oldest = min(usage.request_timestamps) if usage.request_timestamps else now
            retry_after = _WINDOW_SECONDS - (now - oldest)
            return QuotaDenial(
                tenant_id=tenant_id,
                reason=DenialReason.RATE_LIMITED,
                quota_kind=QuotaKind.API_REQUESTS,
                limit=config.requests_per_minute,
                current=len(usage.request_timestamps),
                retry_after_s=max(0.0, retry_after),
                message=f"Rate limit {config.requests_per_minute} req/min exceeded",
            )

        usage.request_timestamps.append(now)
        return None

    def check_task_quota(self, tenant_id: str) -> QuotaDenial | None:
        """Check if creating a new task is within the hourly quota.

        Args:
            tenant_id: Tenant creating the task.

        Returns:
            QuotaDenial if quota exceeded, None if allowed.
        """
        config = self.get_tenant_config(tenant_id)
        if config.suspended:
            return QuotaDenial(
                tenant_id=tenant_id,
                reason=DenialReason.TENANT_SUSPENDED,
                quota_kind=QuotaKind.TASKS_PER_HOUR,
                limit=0,
                current=0,
                message=f"Tenant {tenant_id} is suspended",
            )

        now = time.time()
        usage = self._get_usage(tenant_id)
        hour_s = 3600.0
        usage.task_timestamps = self._prune_timestamps(
            usage.task_timestamps,
            hour_s,
            now,
        )

        if len(usage.task_timestamps) >= config.tasks_per_hour:
            oldest = min(usage.task_timestamps) if usage.task_timestamps else now
            retry_after = hour_s - (now - oldest)
            return QuotaDenial(
                tenant_id=tenant_id,
                reason=DenialReason.QUOTA_EXCEEDED,
                quota_kind=QuotaKind.TASKS_PER_HOUR,
                limit=config.tasks_per_hour,
                current=len(usage.task_timestamps),
                retry_after_s=max(0.0, retry_after),
                message=f"Task quota {config.tasks_per_hour}/hour exceeded",
            )

        usage.task_timestamps.append(now)
        return None

    def check_agent_concurrency(self, tenant_id: str) -> QuotaDenial | None:
        """Check if spawning a new agent is within concurrency limits.

        Args:
            tenant_id: Tenant spawning the agent.

        Returns:
            QuotaDenial if concurrency exceeded, None if allowed.
        """
        config = self.get_tenant_config(tenant_id)
        usage = self._get_usage(tenant_id)

        if usage.concurrent_agents >= config.max_concurrent_agents:
            return QuotaDenial(
                tenant_id=tenant_id,
                reason=DenialReason.CONCURRENCY_EXCEEDED,
                quota_kind=QuotaKind.CONCURRENT_AGENTS,
                limit=config.max_concurrent_agents,
                current=usage.concurrent_agents,
                retry_after_s=5.0,
                message=f"Max {config.max_concurrent_agents} concurrent agents",
            )

        return None

    def record_agent_start(self, tenant_id: str) -> None:
        """Record that an agent has started for a tenant.

        Args:
            tenant_id: Tenant identifier.
        """
        usage = self._get_usage(tenant_id)
        usage.concurrent_agents += 1

    def record_agent_stop(self, tenant_id: str) -> None:
        """Record that an agent has stopped for a tenant.

        Args:
            tenant_id: Tenant identifier.
        """
        usage = self._get_usage(tenant_id)
        usage.concurrent_agents = max(0, usage.concurrent_agents - 1)

    def get_usage_summary(self, tenant_id: str) -> dict[str, Any]:
        """Return a summary of current usage for a tenant.

        Args:
            tenant_id: Tenant identifier.

        Returns:
            Dictionary with current usage metrics.
        """
        config = self.get_tenant_config(tenant_id)
        usage = self._get_usage(tenant_id)
        now = time.time()
        return {
            "tenant_id": tenant_id,
            "requests_last_minute": len(
                self._prune_timestamps(usage.request_timestamps, _WINDOW_SECONDS, now),
            ),
            "requests_per_minute_limit": config.requests_per_minute,
            "tasks_last_hour": len(
                self._prune_timestamps(usage.task_timestamps, 3600.0, now),
            ),
            "tasks_per_hour_limit": config.tasks_per_hour,
            "concurrent_agents": usage.concurrent_agents,
            "max_concurrent_agents": config.max_concurrent_agents,
            "suspended": config.suspended,
        }

    def reset_tenant(self, tenant_id: str) -> None:
        """Reset all usage counters for a tenant.

        Args:
            tenant_id: Tenant to reset.
        """
        self._usage.pop(tenant_id, None)
