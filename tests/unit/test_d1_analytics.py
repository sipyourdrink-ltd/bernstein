"""Tests for Cloudflare D1 analytics and billing integration.

Covers data classes, billing tier definitions, SQL execution, event
recording, usage queries, quota checks, and error handling.
"""

from __future__ import annotations

import json
import time
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from bernstein.core.cost.d1_analytics import (
    BILLING_TIERS,
    BillingTier,
    D1AnalyticsClient,
    D1Config,
    QuotaCheckResult,
    UsageEvent,
    UsageSummary,
    _current_period,
    _period_to_timestamps,
    _today_start_timestamp,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def d1_config() -> D1Config:
    """Return a sample D1Config."""
    return D1Config(
        account_id="acct-123",
        api_token="tok-secret",
        database_id="db-456",
    )


@pytest.fixture()
def client(d1_config: D1Config) -> D1AnalyticsClient:
    """Return a D1AnalyticsClient with sample config."""
    return D1AnalyticsClient(d1_config)


_FAKE_REQUEST = httpx.Request("POST", "https://fake.test/query")


def _mock_d1_response(rows: list[dict[str, Any]]) -> httpx.Response:
    """Build a fake httpx.Response mimicking D1 API shape."""
    body = {"result": [{"results": rows}], "success": True}
    return httpx.Response(200, json=body, request=_FAKE_REQUEST)


def _mock_d1_empty_response() -> httpx.Response:
    """Build a fake httpx.Response with no result rows."""
    body = {"result": [{}], "success": True}
    return httpx.Response(200, json=body, request=_FAKE_REQUEST)


# ---------------------------------------------------------------------------
# D1Config
# ---------------------------------------------------------------------------


class TestD1Config:
    """D1Config dataclass tests."""

    def test_creation_with_defaults(self) -> None:
        cfg = D1Config(account_id="a", api_token="t", database_id="d")
        assert cfg.account_id == "a"
        assert cfg.api_token == "t"
        assert cfg.database_id == "d"
        assert cfg.database_name == "bernstein-analytics"

    def test_creation_with_custom_name(self) -> None:
        cfg = D1Config(
            account_id="a",
            api_token="t",
            database_id="d",
            database_name="custom-db",
        )
        assert cfg.database_name == "custom-db"

    def test_frozen(self) -> None:
        cfg = D1Config(account_id="a", api_token="t", database_id="d")
        with pytest.raises(AttributeError):
            cfg.account_id = "x"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# UsageEvent
# ---------------------------------------------------------------------------


class TestUsageEvent:
    """UsageEvent dataclass tests."""

    def test_creation_minimal(self) -> None:
        evt = UsageEvent(user_id="u1", event_type="run_start", timestamp=1000.0)
        assert evt.user_id == "u1"
        assert evt.event_type == "run_start"
        assert evt.timestamp == 1000.0
        assert evt.tokens_input == 0
        assert evt.tokens_output == 0
        assert evt.cost_usd == 0.0
        assert evt.model == ""
        assert evt.run_id == ""
        assert evt.metadata == {}

    def test_creation_full(self) -> None:
        evt = UsageEvent(
            user_id="u2",
            event_type="token_usage",
            timestamp=2000.0,
            metadata={"k": "v"},
            tokens_input=500,
            tokens_output=300,
            cost_usd=0.12,
            model="claude-sonnet-4-20250514",
            run_id="run-99",
        )
        assert evt.tokens_input == 500
        assert evt.tokens_output == 300
        assert evt.cost_usd == 0.12
        assert evt.model == "claude-sonnet-4-20250514"
        assert evt.run_id == "run-99"
        assert evt.metadata == {"k": "v"}


# ---------------------------------------------------------------------------
# UsageSummary
# ---------------------------------------------------------------------------


class TestUsageSummary:
    """UsageSummary dataclass tests."""

    def test_creation_defaults(self) -> None:
        s = UsageSummary(user_id="u1", period="2026-04")
        assert s.total_runs == 0
        assert s.total_agents_spawned == 0
        assert s.total_tokens_input == 0
        assert s.total_tokens_output == 0
        assert s.total_cost_usd == 0.0
        assert s.models_used == []

    def test_creation_full(self) -> None:
        s = UsageSummary(
            user_id="u1",
            period="2026-04",
            total_runs=10,
            total_agents_spawned=20,
            total_tokens_input=50000,
            total_tokens_output=30000,
            total_cost_usd=4.50,
            models_used=["claude-sonnet-4-20250514", "gpt-4o"],
        )
        assert s.total_runs == 10
        assert s.models_used == ["claude-sonnet-4-20250514", "gpt-4o"]


# ---------------------------------------------------------------------------
# BillingTier & BILLING_TIERS
# ---------------------------------------------------------------------------


class TestBillingTier:
    """BillingTier and tier definitions."""

    def test_billing_tiers_keys(self) -> None:
        assert set(BILLING_TIERS) == {"free", "pro", "team", "enterprise"}

    def test_free_tier_limits(self) -> None:
        t = BILLING_TIERS["free"]
        assert t.name == "free"
        assert t.max_runs_per_day == 5
        assert t.max_parallel_agents == 1
        assert t.max_monthly_cost_usd == 0.0
        assert "basic_models" in t.features

    def test_pro_tier_limits(self) -> None:
        t = BILLING_TIERS["pro"]
        assert t.max_runs_per_day == 999999
        assert t.max_parallel_agents == 5
        assert t.max_monthly_cost_usd == 49.0
        assert "all_models" in t.features
        assert "priority_queue" in t.features

    def test_team_tier_limits(self) -> None:
        t = BILLING_TIERS["team"]
        assert t.max_parallel_agents == 10
        assert t.max_monthly_cost_usd == 199.0
        assert "sso" in t.features
        assert "audit_logs" in t.features
        assert "shared_workspaces" in t.features

    def test_enterprise_tier_limits(self) -> None:
        t = BILLING_TIERS["enterprise"]
        assert t.max_parallel_agents == 50
        assert t.max_monthly_cost_usd == 0.0  # unlimited
        assert "dedicated_infra" in t.features
        assert "sla" in t.features

    def test_frozen(self) -> None:
        t = BillingTier(name="test")
        with pytest.raises(AttributeError):
            t.name = "x"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# QuotaCheckResult
# ---------------------------------------------------------------------------


class TestQuotaCheckResult:
    """QuotaCheckResult dataclass tests."""

    def test_within_limits(self) -> None:
        r = QuotaCheckResult(within_limits=True, daily_runs_used=3, daily_runs_limit=5)
        assert r.within_limits is True
        assert r.reason == ""

    def test_over_limits(self) -> None:
        r = QuotaCheckResult(
            within_limits=False,
            daily_runs_used=5,
            daily_runs_limit=5,
            reason="Daily run limit exceeded",
        )
        assert r.within_limits is False
        assert "Daily" in r.reason


# ---------------------------------------------------------------------------
# _execute_sql
# ---------------------------------------------------------------------------


class TestExecuteSQL:
    """Low-level SQL execution tests."""

    @pytest.mark.asyncio()
    async def test_builds_correct_url_and_headers(self, client: D1AnalyticsClient, d1_config: D1Config) -> None:
        expected_url = (
            f"https://api.cloudflare.com/client/v4/accounts/"
            f"{d1_config.account_id}/d1/database/{d1_config.database_id}/query"
        )
        mock_resp = _mock_d1_response([{"x": 1}])
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp) as mock_post:
            await client._execute_sql("SELECT 1")
            mock_post.assert_called_once()
            call_args = mock_post.call_args
            assert call_args[0][0] == expected_url
            headers = call_args[1]["headers"]
            assert headers["Authorization"] == f"Bearer {d1_config.api_token}"
            assert headers["Content-Type"] == "application/json"

    @pytest.mark.asyncio()
    async def test_sends_params(self, client: D1AnalyticsClient) -> None:
        mock_resp = _mock_d1_response([])
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp) as mock_post:
            await client._execute_sql("SELECT ?", ["hello"])
            payload = mock_post.call_args[1]["json"]
            assert payload["sql"] == "SELECT ?"
            assert payload["params"] == ["hello"]

    @pytest.mark.asyncio()
    async def test_no_params_omits_key(self, client: D1AnalyticsClient) -> None:
        mock_resp = _mock_d1_response([])
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp) as mock_post:
            await client._execute_sql("SELECT 1")
            payload = mock_post.call_args[1]["json"]
            assert "params" not in payload

    @pytest.mark.asyncio()
    async def test_returns_rows(self, client: D1AnalyticsClient) -> None:
        rows = [{"id": "a"}, {"id": "b"}]
        mock_resp = _mock_d1_response(rows)
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
            result = await client._execute_sql("SELECT id FROM t")
            assert result == rows

    @pytest.mark.asyncio()
    async def test_empty_result(self, client: D1AnalyticsClient) -> None:
        mock_resp = _mock_d1_empty_response()
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
            result = await client._execute_sql("SELECT 1")
            assert result == []

    @pytest.mark.asyncio()
    async def test_api_error_raises(self, client: D1AnalyticsClient) -> None:
        error_resp = httpx.Response(500, text="Internal Server Error", request=_FAKE_REQUEST)
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=error_resp):
            with pytest.raises(httpx.HTTPStatusError):
                await client._execute_sql("SELECT 1")

    @pytest.mark.asyncio()
    async def test_malformed_response(self, client: D1AnalyticsClient) -> None:
        bad_resp = httpx.Response(200, json={"result": []}, request=_FAKE_REQUEST)
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=bad_resp):
            result = await client._execute_sql("SELECT 1")
            assert result == []


# ---------------------------------------------------------------------------
# record_event / record_events_batch
# ---------------------------------------------------------------------------


class TestRecordEvent:
    """Event recording tests."""

    @pytest.mark.asyncio()
    async def test_record_event_sql_and_params(self, client: D1AnalyticsClient) -> None:
        evt = UsageEvent(
            user_id="u1",
            event_type="run_start",
            timestamp=1000.0,
            metadata={"plan": "p1"},
            tokens_input=100,
            tokens_output=50,
            cost_usd=0.01,
            model="claude-sonnet-4-20250514",
            run_id="r1",
        )
        mock_resp = _mock_d1_response([])
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp) as mock_post:
            await client.record_event(evt)
            payload = mock_post.call_args[1]["json"]
            assert "INSERT INTO usage_events" in payload["sql"]
            params = payload["params"]
            # params: id, user_id, event_type, timestamp, metadata, ...
            assert params[1] == "u1"
            assert params[2] == "run_start"
            assert params[3] == 1000.0
            assert json.loads(params[4]) == {"plan": "p1"}
            assert params[5] == 100
            assert params[6] == 50
            assert params[7] == 0.01
            assert params[8] == "claude-sonnet-4-20250514"
            assert params[9] == "r1"

    @pytest.mark.asyncio()
    async def test_record_events_batch_empty(self, client: D1AnalyticsClient) -> None:
        count = await client.record_events_batch([])
        assert count == 0

    @pytest.mark.asyncio()
    async def test_record_events_batch_multiple(self, client: D1AnalyticsClient) -> None:
        events = [
            UsageEvent(user_id="u1", event_type="run_start", timestamp=1000.0),
            UsageEvent(user_id="u1", event_type="agent_spawn", timestamp=1001.0),
            UsageEvent(user_id="u1", event_type="run_complete", timestamp=1002.0),
        ]
        mock_resp = _mock_d1_response([])
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp) as mock_post:
            count = await client.record_events_batch(events)
            assert count == 3
            assert mock_post.call_count == 3


# ---------------------------------------------------------------------------
# get_usage_summary
# ---------------------------------------------------------------------------


class TestGetUsageSummary:
    """Usage summary query tests."""

    @pytest.mark.asyncio()
    async def test_parses_d1_response(self, client: D1AnalyticsClient) -> None:
        summary_row = {
            "total_runs": 5,
            "total_agents": 12,
            "total_tokens_input": 50000,
            "total_tokens_output": 30000,
            "total_cost_usd": 2.50,
        }
        model_rows = [{"model": "claude-sonnet-4-20250514"}, {"model": "gpt-4o"}]

        call_count = 0

        async def mock_post(*args: Any, **kwargs: Any) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return _mock_d1_response([summary_row])
            return _mock_d1_response(model_rows)

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, side_effect=mock_post):
            summary = await client.get_usage_summary("u1", "2026-04")
            assert summary.user_id == "u1"
            assert summary.period == "2026-04"
            assert summary.total_runs == 5
            assert summary.total_agents_spawned == 12
            assert summary.total_tokens_input == 50000
            assert summary.total_tokens_output == 30000
            assert summary.total_cost_usd == 2.50
            assert summary.models_used == ["claude-sonnet-4-20250514", "gpt-4o"]

    @pytest.mark.asyncio()
    async def test_empty_result(self, client: D1AnalyticsClient) -> None:
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=_mock_d1_response([])):
            summary = await client.get_usage_summary("u1", "2026-04")
            assert summary.total_runs == 0
            assert summary.total_cost_usd == 0.0
            assert summary.models_used == []


# ---------------------------------------------------------------------------
# get_daily_run_count
# ---------------------------------------------------------------------------


class TestGetDailyRunCount:
    """Daily run count query tests."""

    @pytest.mark.asyncio()
    async def test_returns_count(self, client: D1AnalyticsClient) -> None:
        mock_resp = _mock_d1_response([{"cnt": 7}])
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp) as mock_post:
            count = await client.get_daily_run_count("u1")
            assert count == 7
            payload = mock_post.call_args[1]["json"]
            assert "run_start" in payload["sql"]
            assert payload["params"][0] == "u1"

    @pytest.mark.asyncio()
    async def test_empty_returns_zero(self, client: D1AnalyticsClient) -> None:
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=_mock_d1_response([])):
            count = await client.get_daily_run_count("u1")
            assert count == 0


# ---------------------------------------------------------------------------
# check_quota
# ---------------------------------------------------------------------------


class TestCheckQuota:
    """Quota checking tests."""

    @pytest.mark.asyncio()
    async def test_within_limits(self, client: D1AnalyticsClient) -> None:
        # Free tier, 2 runs today (limit 5)
        mock_resp = _mock_d1_response([{"cnt": 2}])
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
            result = await client.check_quota("u1", "free")
            assert result.within_limits is True
            assert result.daily_runs_used == 2
            assert result.daily_runs_limit == 5

    @pytest.mark.asyncio()
    async def test_daily_limit_exceeded(self, client: D1AnalyticsClient) -> None:
        mock_resp = _mock_d1_response([{"cnt": 5}])
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
            result = await client.check_quota("u1", "free")
            assert result.within_limits is False
            assert "Daily" in result.reason

    @pytest.mark.asyncio()
    async def test_monthly_cost_exceeded(self, client: D1AnalyticsClient) -> None:
        call_count = 0

        async def mock_post(*args: Any, **kwargs: Any) -> httpx.Response:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                # daily run count = 0 (within limit)
                return _mock_d1_response([{"cnt": 0}])
            if call_count == 2:
                # summary query: cost exceeds $49
                return _mock_d1_response(
                    [
                        {
                            "total_runs": 100,
                            "total_agents": 200,
                            "total_tokens_input": 999999,
                            "total_tokens_output": 500000,
                            "total_cost_usd": 55.0,
                        }
                    ]
                )
            # models query
            return _mock_d1_response([])

        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, side_effect=mock_post):
            result = await client.check_quota("u1", "pro")
            assert result.within_limits is False
            assert "Monthly" in result.reason
            assert result.monthly_cost_used == 55.0
            assert result.monthly_cost_limit == 49.0

    @pytest.mark.asyncio()
    async def test_unknown_tier(self, client: D1AnalyticsClient) -> None:
        result = await client.check_quota("u1", "nonexistent")
        assert result.within_limits is False
        assert "Unknown tier" in result.reason

    @pytest.mark.asyncio()
    async def test_enterprise_unlimited_cost(self, client: D1AnalyticsClient) -> None:
        # Enterprise has max_monthly_cost_usd=0 → skip cost check
        mock_resp = _mock_d1_response([{"cnt": 0}])
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp):
            result = await client.check_quota("u1", "enterprise")
            assert result.within_limits is True


# ---------------------------------------------------------------------------
# get_top_users
# ---------------------------------------------------------------------------


class TestGetTopUsers:
    """Top users query tests."""

    @pytest.mark.asyncio()
    async def test_orders_by_cost(self, client: D1AnalyticsClient) -> None:
        rows = [
            {
                "user_id": "u2",
                "total_runs": 50,
                "total_agents": 100,
                "total_tokens_input": 500000,
                "total_tokens_output": 250000,
                "total_cost_usd": 25.0,
            },
            {
                "user_id": "u1",
                "total_runs": 10,
                "total_agents": 20,
                "total_tokens_input": 100000,
                "total_tokens_output": 50000,
                "total_cost_usd": 5.0,
            },
        ]
        mock_resp = _mock_d1_response(rows)
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp) as mock_post:
            result = await client.get_top_users("2026-04", limit=2)
            assert len(result) == 2
            assert result[0].user_id == "u2"
            assert result[0].total_cost_usd == 25.0
            assert result[1].user_id == "u1"
            # Verify LIMIT is passed
            payload = mock_post.call_args[1]["json"]
            assert "ORDER BY total_cost_usd DESC" in payload["sql"]
            assert payload["params"][-1] == 2

    @pytest.mark.asyncio()
    async def test_empty_period(self, client: D1AnalyticsClient) -> None:
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=_mock_d1_response([])):
            result = await client.get_top_users("2026-04")
            assert result == []


# ---------------------------------------------------------------------------
# initialize_schema
# ---------------------------------------------------------------------------


class TestInitializeSchema:
    """Schema initialization tests."""

    @pytest.mark.asyncio()
    async def test_creates_tables_and_index(self, client: D1AnalyticsClient) -> None:
        mock_resp = _mock_d1_response([])
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp) as mock_post:
            await client.initialize_schema()
            assert mock_post.call_count == 3
            sqls = [call[1]["json"]["sql"] for call in mock_post.call_args_list]
            assert any("CREATE TABLE" in s and "usage_events" in s for s in sqls)
            assert any("CREATE INDEX" in s for s in sqls)
            assert any("CREATE TABLE" in s and "user_quotas" in s for s in sqls)


# ---------------------------------------------------------------------------
# Helper functions
# ---------------------------------------------------------------------------


class TestHelpers:
    """Period/timestamp helper tests."""

    def test_period_to_timestamps(self) -> None:
        start, end = _period_to_timestamps("2026-04")
        # April 2026 should span 30 days
        assert end > start
        # Rough check: ~30 days in seconds
        assert 29 * 86400 < (end - start) < 32 * 86400

    def test_period_to_timestamps_december(self) -> None:
        start, end = _period_to_timestamps("2026-12")
        assert end > start
        # December → January next year
        assert 30 * 86400 < (end - start) < 32 * 86400

    def test_today_start_timestamp(self) -> None:
        ts = _today_start_timestamp()
        assert ts <= time.time()
        # Should be within the last 24h
        assert time.time() - ts < 86400

    def test_current_period_format(self) -> None:
        period = _current_period()
        parts = period.split("-")
        assert len(parts) == 2
        assert len(parts[0]) == 4
        assert len(parts[1]) == 2


# ---------------------------------------------------------------------------
# Error handling
# ---------------------------------------------------------------------------


class TestErrorHandling:
    """Error and edge-case handling tests."""

    @pytest.mark.asyncio()
    async def test_d1_api_failure_propagates(self, client: D1AnalyticsClient) -> None:
        error_resp = httpx.Response(403, text="Forbidden", request=_FAKE_REQUEST)
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=error_resp):
            with pytest.raises(httpx.HTTPStatusError):
                await client.record_event(
                    UsageEvent(
                        user_id="u1",
                        event_type="run_start",
                        timestamp=1000.0,
                    )
                )

    @pytest.mark.asyncio()
    async def test_record_event_with_empty_metadata(self, client: D1AnalyticsClient) -> None:
        mock_resp = _mock_d1_response([])
        with patch("httpx.AsyncClient.post", new_callable=AsyncMock, return_value=mock_resp) as mock_post:
            await client.record_event(
                UsageEvent(
                    user_id="u1",
                    event_type="run_start",
                    timestamp=1000.0,
                )
            )
            params = mock_post.call_args[1]["json"]["params"]
            assert json.loads(params[4]) == {}
