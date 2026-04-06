"""Tests for ENT-008: Per-tenant rate limiting and quota enforcement."""

from __future__ import annotations

from bernstein.core.tenant_rate_limiter import (
    DenialReason,
    QuotaKind,
    TenantQuotaConfig,
    TenantRateLimiter,
)

# ---------------------------------------------------------------------------
# API rate limiting
# ---------------------------------------------------------------------------


class TestAPIRateLimit:
    def test_allows_under_limit(self) -> None:
        limiter = TenantRateLimiter()
        config = TenantQuotaConfig(
            tenant_id="t1",
            requests_per_minute=100,
        )
        limiter.set_tenant_config(config)
        result = limiter.check_api_rate("t1")
        assert result is None

    def test_denies_over_limit(self) -> None:
        limiter = TenantRateLimiter()
        config = TenantQuotaConfig(
            tenant_id="t1",
            requests_per_minute=3,
        )
        limiter.set_tenant_config(config)

        # Use up the limit
        for _ in range(3):
            assert limiter.check_api_rate("t1") is None

        # Next request should be denied
        denial = limiter.check_api_rate("t1")
        assert denial is not None
        assert denial.reason == DenialReason.RATE_LIMITED
        assert denial.quota_kind == QuotaKind.API_REQUESTS
        assert denial.limit == 3
        assert denial.retry_after_s >= 0

    def test_suspended_tenant_denied(self) -> None:
        limiter = TenantRateLimiter()
        config = TenantQuotaConfig(tenant_id="t1", suspended=True)
        limiter.set_tenant_config(config)

        denial = limiter.check_api_rate("t1")
        assert denial is not None
        assert denial.reason == DenialReason.TENANT_SUSPENDED

    def test_unknown_tenant_uses_defaults(self) -> None:
        limiter = TenantRateLimiter(
            default_config=TenantQuotaConfig(requests_per_minute=10),
        )
        result = limiter.check_api_rate("unknown")
        assert result is None


# ---------------------------------------------------------------------------
# Task quota
# ---------------------------------------------------------------------------


class TestTaskQuota:
    def test_allows_under_quota(self) -> None:
        limiter = TenantRateLimiter()
        config = TenantQuotaConfig(
            tenant_id="t1",
            tasks_per_hour=50,
        )
        limiter.set_tenant_config(config)
        assert limiter.check_task_quota("t1") is None

    def test_denies_over_quota(self) -> None:
        limiter = TenantRateLimiter()
        config = TenantQuotaConfig(
            tenant_id="t1",
            tasks_per_hour=2,
        )
        limiter.set_tenant_config(config)

        for _ in range(2):
            assert limiter.check_task_quota("t1") is None

        denial = limiter.check_task_quota("t1")
        assert denial is not None
        assert denial.reason == DenialReason.QUOTA_EXCEEDED
        assert denial.quota_kind == QuotaKind.TASKS_PER_HOUR

    def test_suspended_denied(self) -> None:
        limiter = TenantRateLimiter()
        config = TenantQuotaConfig(tenant_id="t1", suspended=True)
        limiter.set_tenant_config(config)
        denial = limiter.check_task_quota("t1")
        assert denial is not None
        assert denial.reason == DenialReason.TENANT_SUSPENDED


# ---------------------------------------------------------------------------
# Agent concurrency
# ---------------------------------------------------------------------------


class TestAgentConcurrency:
    def test_allows_under_limit(self) -> None:
        limiter = TenantRateLimiter()
        config = TenantQuotaConfig(
            tenant_id="t1",
            max_concurrent_agents=3,
        )
        limiter.set_tenant_config(config)
        assert limiter.check_agent_concurrency("t1") is None

    def test_denies_at_limit(self) -> None:
        limiter = TenantRateLimiter()
        config = TenantQuotaConfig(
            tenant_id="t1",
            max_concurrent_agents=2,
        )
        limiter.set_tenant_config(config)

        limiter.record_agent_start("t1")
        limiter.record_agent_start("t1")

        denial = limiter.check_agent_concurrency("t1")
        assert denial is not None
        assert denial.reason == DenialReason.CONCURRENCY_EXCEEDED

    def test_stop_frees_slot(self) -> None:
        limiter = TenantRateLimiter()
        config = TenantQuotaConfig(
            tenant_id="t1",
            max_concurrent_agents=1,
        )
        limiter.set_tenant_config(config)

        limiter.record_agent_start("t1")
        assert limiter.check_agent_concurrency("t1") is not None

        limiter.record_agent_stop("t1")
        assert limiter.check_agent_concurrency("t1") is None

    def test_stop_does_not_go_negative(self) -> None:
        limiter = TenantRateLimiter()
        limiter.record_agent_stop("t1")
        summary = limiter.get_usage_summary("t1")
        assert summary["concurrent_agents"] == 0


# ---------------------------------------------------------------------------
# Usage summary
# ---------------------------------------------------------------------------


class TestUsageSummary:
    def test_summary_structure(self) -> None:
        limiter = TenantRateLimiter()
        config = TenantQuotaConfig(
            tenant_id="t1",
            requests_per_minute=100,
            tasks_per_hour=50,
            max_concurrent_agents=6,
        )
        limiter.set_tenant_config(config)
        summary = limiter.get_usage_summary("t1")
        assert summary["tenant_id"] == "t1"
        assert summary["requests_per_minute_limit"] == 100
        assert summary["tasks_per_hour_limit"] == 50
        assert summary["max_concurrent_agents"] == 6
        assert not summary["suspended"]


# ---------------------------------------------------------------------------
# Reset
# ---------------------------------------------------------------------------


class TestReset:
    def test_reset_clears_usage(self) -> None:
        limiter = TenantRateLimiter()
        config = TenantQuotaConfig(
            tenant_id="t1",
            requests_per_minute=2,
        )
        limiter.set_tenant_config(config)

        limiter.check_api_rate("t1")
        limiter.check_api_rate("t1")
        assert limiter.check_api_rate("t1") is not None

        limiter.reset_tenant("t1")
        assert limiter.check_api_rate("t1") is None
