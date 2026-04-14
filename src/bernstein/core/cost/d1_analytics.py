"""Cloudflare D1 analytics and billing integration.

Tracks per-user usage, metering events, and cost data in Cloudflare D1
(serverless SQLite) for the hosted Bernstein SaaS.  Provides query
interfaces for billing, dashboards, and usage reports.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import UTC
from typing import Any

import httpx

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class D1Config:
    """Configuration for D1 analytics database.

    Attributes:
        account_id: Cloudflare account ID.
        api_token: Cloudflare API bearer token.
        database_id: D1 database UUID.
        database_name: Human-readable database name.
    """

    account_id: str
    api_token: str
    database_id: str
    database_name: str = "bernstein-analytics"


@dataclass(frozen=True)
class UsageEvent:
    """A single metered usage event.

    Attributes:
        user_id: Owning user identifier.
        event_type: One of ``"run_start"``, ``"run_complete"``,
            ``"agent_spawn"``, ``"token_usage"``.
        timestamp: Unix epoch seconds.
        metadata: Arbitrary key/value context.
        tokens_input: Input tokens consumed.
        tokens_output: Output tokens consumed.
        cost_usd: Estimated cost in USD.
        model: Model identifier (e.g. ``"claude-sonnet-4-20250514"``).
        run_id: Orchestration run identifier.
    """

    user_id: str
    event_type: str
    timestamp: float
    metadata: dict[str, Any] = field(default_factory=dict)
    tokens_input: int = 0
    tokens_output: int = 0
    cost_usd: float = 0.0
    model: str = ""
    run_id: str = ""


@dataclass(frozen=True)
class UsageSummary:
    """Aggregated usage summary for a user/period.

    Attributes:
        user_id: User identifier.
        period: Month string in ``"YYYY-MM"`` format.
        total_runs: Number of orchestration runs.
        total_agents_spawned: Number of agents spawned.
        total_tokens_input: Aggregate input tokens.
        total_tokens_output: Aggregate output tokens.
        total_cost_usd: Aggregate cost in USD.
        models_used: Distinct model identifiers used.
    """

    user_id: str
    period: str
    total_runs: int = 0
    total_agents_spawned: int = 0
    total_tokens_input: int = 0
    total_tokens_output: int = 0
    total_cost_usd: float = 0.0
    models_used: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class BillingTier:
    """User billing tier with limits.

    Attributes:
        name: Tier identifier (``"free"``, ``"pro"``, ``"team"``,
            ``"enterprise"``).
        max_runs_per_day: Maximum orchestration runs per calendar day.
        max_parallel_agents: Maximum concurrent agents.
        max_monthly_cost_usd: Spend cap (0 = unlimited).
        features: Enabled feature flags.
    """

    name: str
    max_runs_per_day: int = 5
    max_parallel_agents: int = 1
    max_monthly_cost_usd: float = 0.0
    features: frozenset[str] = frozenset()


@dataclass(frozen=True)
class QuotaCheckResult:
    """Result of a quota check.

    Attributes:
        within_limits: ``True`` when the user may proceed.
        daily_runs_used: Runs consumed today.
        daily_runs_limit: Daily run cap from billing tier.
        monthly_cost_used: Cost accrued this month (USD).
        monthly_cost_limit: Monthly spend cap (USD).
        reason: Human-readable explanation when over quota.
    """

    within_limits: bool
    daily_runs_used: int = 0
    daily_runs_limit: int = 0
    monthly_cost_used: float = 0.0
    monthly_cost_limit: float = 0.0
    reason: str = ""


# ---------------------------------------------------------------------------
# Pre-defined billing tiers
# ---------------------------------------------------------------------------

BILLING_TIERS: dict[str, BillingTier] = {
    "free": BillingTier(
        name="free",
        max_runs_per_day=5,
        max_parallel_agents=1,
        max_monthly_cost_usd=0.0,
        features=frozenset({"basic_models"}),
    ),
    "pro": BillingTier(
        name="pro",
        max_runs_per_day=999999,
        max_parallel_agents=5,
        max_monthly_cost_usd=49.0,
        features=frozenset({"basic_models", "all_models", "priority_queue"}),
    ),
    "team": BillingTier(
        name="team",
        max_runs_per_day=999999,
        max_parallel_agents=10,
        max_monthly_cost_usd=199.0,
        features=frozenset(
            {
                "basic_models",
                "all_models",
                "priority_queue",
                "sso",
                "audit_logs",
                "shared_workspaces",
            }
        ),
    ),
    "enterprise": BillingTier(
        name="enterprise",
        max_runs_per_day=999999,
        max_parallel_agents=50,
        max_monthly_cost_usd=0.0,
        features=frozenset(
            {
                "basic_models",
                "all_models",
                "priority_queue",
                "sso",
                "audit_logs",
                "shared_workspaces",
                "dedicated_infra",
                "sla",
            }
        ),
    ),
}

# ---------------------------------------------------------------------------
# SQL schema
# ---------------------------------------------------------------------------

_CREATE_USAGE_EVENTS_SQL = """\
CREATE TABLE IF NOT EXISTS usage_events (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    timestamp REAL NOT NULL,
    metadata TEXT,
    tokens_input INTEGER DEFAULT 0,
    tokens_output INTEGER DEFAULT 0,
    cost_usd REAL DEFAULT 0.0,
    model TEXT DEFAULT '',
    run_id TEXT DEFAULT ''
);
"""

_CREATE_USAGE_EVENTS_INDEX_SQL = """\
CREATE INDEX IF NOT EXISTS idx_user_events
ON usage_events (user_id, timestamp);
"""

_CREATE_USER_QUOTAS_SQL = """\
CREATE TABLE IF NOT EXISTS user_quotas (
    user_id TEXT PRIMARY KEY,
    tier TEXT NOT NULL DEFAULT 'free',
    updated_at REAL NOT NULL
);
"""


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------


class D1AnalyticsClient:
    """Client for D1 analytics database operations.

    Example::

        client = D1AnalyticsClient(D1Config(
            account_id="...", api_token="...", database_id="..."
        ))
        await client.initialize_schema()
        await client.record_event(UsageEvent(
            user_id="u1", event_type="run_start", timestamp=time.time(),
        ))
        summary = await client.get_usage_summary("u1", "2026-04")
        result = await client.check_quota("u1", "pro")
    """

    def __init__(self, config: D1Config) -> None:
        self._config = config
        self._base_url = (
            f"https://api.cloudflare.com/client/v4/accounts/{config.account_id}/d1/database/{config.database_id}/query"
        )

    # -- schema --------------------------------------------------------------

    async def initialize_schema(self) -> None:
        """Create analytics tables if they don't exist."""
        await self._execute_sql(_CREATE_USAGE_EVENTS_SQL)
        await self._execute_sql(_CREATE_USAGE_EVENTS_INDEX_SQL)
        await self._execute_sql(_CREATE_USER_QUOTAS_SQL)

    # -- write ---------------------------------------------------------------

    async def record_event(self, event: UsageEvent) -> None:
        """Record a single usage event to D1."""
        event_id = uuid.uuid4().hex
        sql = (
            "INSERT INTO usage_events "
            "(id, user_id, event_type, timestamp, metadata, "
            "tokens_input, tokens_output, cost_usd, model, run_id) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
        )
        import json

        params: list[Any] = [
            event_id,
            event.user_id,
            event.event_type,
            event.timestamp,
            json.dumps(event.metadata),
            event.tokens_input,
            event.tokens_output,
            event.cost_usd,
            event.model,
            event.run_id,
        ]
        await self._execute_sql(sql, params)

    async def record_events_batch(self, events: list[UsageEvent]) -> int:
        """Record multiple events in a single transaction.

        Returns:
            Number of events inserted.
        """
        if not events:
            return 0

        for event in events:
            await self.record_event(event)
        return len(events)

    # -- read ----------------------------------------------------------------

    async def get_usage_summary(self, user_id: str, period: str) -> UsageSummary:
        """Get aggregated usage for a user in a given month.

        Args:
            user_id: User identifier.
            period: Month string in ``"YYYY-MM"`` format.
        """
        # period "2026-04" → timestamps for that month
        start_ts, end_ts = _period_to_timestamps(period)

        summary_sql = (
            "SELECT "
            "  COUNT(CASE WHEN event_type = 'run_start' THEN 1 END) AS total_runs, "
            "  COUNT(CASE WHEN event_type = 'agent_spawn' THEN 1 END) AS total_agents, "
            "  COALESCE(SUM(tokens_input), 0) AS total_tokens_input, "
            "  COALESCE(SUM(tokens_output), 0) AS total_tokens_output, "
            "  COALESCE(SUM(cost_usd), 0.0) AS total_cost_usd "
            "FROM usage_events "
            "WHERE user_id = ? AND timestamp >= ? AND timestamp < ?"
        )
        rows = await self._execute_sql(summary_sql, [user_id, start_ts, end_ts])

        models_sql = (
            "SELECT DISTINCT model FROM usage_events "
            "WHERE user_id = ? AND timestamp >= ? AND timestamp < ? "
            "AND model != ''"
        )
        model_rows = await self._execute_sql(models_sql, [user_id, start_ts, end_ts])

        row = rows[0] if rows else {}
        return UsageSummary(
            user_id=user_id,
            period=period,
            total_runs=int(row.get("total_runs", 0)),
            total_agents_spawned=int(row.get("total_agents", 0)),
            total_tokens_input=int(row.get("total_tokens_input", 0)),
            total_tokens_output=int(row.get("total_tokens_output", 0)),
            total_cost_usd=float(row.get("total_cost_usd", 0.0)),
            models_used=[r["model"] for r in model_rows if "model" in r],
        )

    async def get_daily_run_count(self, user_id: str) -> int:
        """Get number of runs today for quota checking."""
        today_start = _today_start_timestamp()
        sql = (
            "SELECT COUNT(*) AS cnt FROM usage_events WHERE user_id = ? AND event_type = 'run_start' AND timestamp >= ?"
        )
        rows = await self._execute_sql(sql, [user_id, today_start])
        if rows:
            return int(rows[0].get("cnt", 0))
        return 0

    async def check_quota(self, user_id: str, tier_name: str) -> QuotaCheckResult:
        """Check if user is within their billing tier limits.

        Args:
            user_id: User identifier.
            tier_name: Billing tier key (must exist in ``BILLING_TIERS``).
        """
        tier = BILLING_TIERS.get(tier_name)
        if tier is None:
            return QuotaCheckResult(within_limits=False, reason=f"Unknown tier: {tier_name}")

        daily_runs = await self.get_daily_run_count(user_id)
        if daily_runs >= tier.max_runs_per_day:
            return QuotaCheckResult(
                within_limits=False,
                daily_runs_used=daily_runs,
                daily_runs_limit=tier.max_runs_per_day,
                reason="Daily run limit exceeded",
            )

        # Monthly cost check (skip if unlimited / 0)
        if tier.max_monthly_cost_usd > 0:
            period = _current_period()
            summary = await self.get_usage_summary(user_id, period)
            if summary.total_cost_usd >= tier.max_monthly_cost_usd:
                return QuotaCheckResult(
                    within_limits=False,
                    daily_runs_used=daily_runs,
                    daily_runs_limit=tier.max_runs_per_day,
                    monthly_cost_used=summary.total_cost_usd,
                    monthly_cost_limit=tier.max_monthly_cost_usd,
                    reason="Monthly cost limit exceeded",
                )

        return QuotaCheckResult(
            within_limits=True,
            daily_runs_used=daily_runs,
            daily_runs_limit=tier.max_runs_per_day,
        )

    async def get_top_users(self, period: str, limit: int = 10) -> list[UsageSummary]:
        """Get top users by cost for a given period.

        Args:
            period: Month string in ``"YYYY-MM"`` format.
            limit: Maximum number of results.
        """
        start_ts, end_ts = _period_to_timestamps(period)
        sql = (
            "SELECT user_id, "
            "  COUNT(CASE WHEN event_type = 'run_start' THEN 1 END) AS total_runs, "
            "  COUNT(CASE WHEN event_type = 'agent_spawn' THEN 1 END) AS total_agents, "
            "  COALESCE(SUM(tokens_input), 0) AS total_tokens_input, "
            "  COALESCE(SUM(tokens_output), 0) AS total_tokens_output, "
            "  COALESCE(SUM(cost_usd), 0.0) AS total_cost_usd "
            "FROM usage_events "
            "WHERE timestamp >= ? AND timestamp < ? "
            "GROUP BY user_id "
            "ORDER BY total_cost_usd DESC "
            "LIMIT ?"
        )
        rows = await self._execute_sql(sql, [start_ts, end_ts, limit])
        return [
            UsageSummary(
                user_id=row.get("user_id", ""),
                period=period,
                total_runs=int(row.get("total_runs", 0)),
                total_agents_spawned=int(row.get("total_agents", 0)),
                total_tokens_input=int(row.get("total_tokens_input", 0)),
                total_tokens_output=int(row.get("total_tokens_output", 0)),
                total_cost_usd=float(row.get("total_cost_usd", 0.0)),
            )
            for row in rows
        ]

    # -- low-level -----------------------------------------------------------

    async def _execute_sql(self, sql: str, params: list[Any] | None = None) -> list[dict[str, Any]]:
        """Execute SQL query against D1 via REST API.

        Args:
            sql: SQL statement.
            params: Positional bind parameters.

        Returns:
            List of result rows as dicts.

        Raises:
            httpx.HTTPStatusError: On non-2xx response.
        """
        headers = {
            "Authorization": f"Bearer {self._config.api_token}",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {"sql": sql}
        if params:
            payload["params"] = params
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(self._base_url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
        results = data.get("result", [{}])
        if results and "results" in results[0]:
            return list(results[0]["results"])
        return []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _period_to_timestamps(period: str) -> tuple[float, float]:
    """Convert ``"YYYY-MM"`` to (start_ts, end_ts) unix timestamps."""
    from datetime import datetime

    year, month = (int(x) for x in period.split("-"))
    start = datetime(year, month, 1, tzinfo=UTC)
    end = datetime(year + 1, 1, 1, tzinfo=UTC) if month == 12 else datetime(year, month + 1, 1, tzinfo=UTC)
    return start.timestamp(), end.timestamp()


def _today_start_timestamp() -> float:
    """Return unix timestamp for midnight UTC today."""
    from datetime import datetime

    now = datetime.now(tz=UTC)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    return midnight.timestamp()


def _current_period() -> str:
    """Return current month as ``"YYYY-MM"``."""
    from datetime import datetime

    now = datetime.now(tz=UTC)
    return f"{now.year}-{now.month:02d}"
