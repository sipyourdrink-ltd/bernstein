"""Dataclass and configuration definitions for bernstein.yaml parsing.

All config dataclasses, the ``SeedError`` exception, and ``MetricSchema``
live here. The parent ``seed`` module re-exports every name for backward
compatibility.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

from bernstein.core.models import (
    BatchConfig,
    BridgeConfigSet,
    ClusterConfig,
    SmtpConfig,
    TestAgentConfig,
)

logger = logging.getLogger(__name__)


def check_internal_llm_preflight(provider: str) -> str | None:
    """Return a migration hint if ``provider`` needs env vars that are missing.

    audit-150: a fresh clone with ``internal_llm_provider: openrouter_free``
    but no ``OPENROUTER_API_KEY_FREE`` / ``OPENROUTER_API_KEY_PAID`` crashes
    on the first LLM call. Callers (seed parser, orchestrator bootstrap) can
    invoke this to emit the hint early and suggest switching to ``'none'``.

    Args:
        provider: The configured ``internal_llm_provider`` value.

    Returns:
        Human-readable hint string, or ``None`` if no action is needed.
    """
    if provider == "openrouter_free" and not (
        os.environ.get("OPENROUTER_API_KEY_FREE") or os.environ.get("OPENROUTER_API_KEY_PAID")
    ):
        return (
            "internal_llm_provider='openrouter_free' requires "
            "OPENROUTER_API_KEY_FREE or OPENROUTER_API_KEY_PAID in the "
            "environment. Either export one of those variables or set "
            "'internal_llm_provider: none' in bernstein.yaml to disable "
            "evolution and auto_decompose gracefully."
        )
    return None


if TYPE_CHECKING:
    from bernstein.agents.catalog import CatalogRegistry
    from bernstein.core.compliance import ComplianceConfig
    from bernstein.core.config.visual_config import VisualConfig
    from bernstein.core.formal_verification import FormalVerificationConfig
    from bernstein.core.key_rotation import KeyRotationConfig
    from bernstein.core.quality_gates import QualityGatesConfig
    from bernstein.core.sandbox import DockerSandbox
    from bernstein.core.secrets import SecretsConfig
    from bernstein.core.tenanting import TenantConfig
    from bernstein.core.workspace import Workspace
    from bernstein.core.worktree import WorktreeSetupConfig


class SeedError(Exception):
    """Raised when the seed file is missing, unreadable, or invalid."""


@dataclass(frozen=True)
class StorageConfig:
    """Storage backend configuration.

    Attributes:
        backend: Storage backend type (``memory``, ``postgres``, ``redis``).
        database_url: PostgreSQL DSN, required for postgres/redis backends.
        redis_url: Redis URL, required for redis backend.
    """

    backend: Literal["memory", "postgres", "redis"] = "memory"
    database_url: str | None = None
    redis_url: str | None = None


@dataclass(frozen=True)
class SessionConfig:
    """Session resume configuration.

    Attributes:
        resume: Whether to resume from a prior session when available.
        stale_after_minutes: Sessions older than this are discarded and a fresh
            start is forced (default: 30).
    """

    resume: bool = True
    stale_after_minutes: int = 30


@dataclass(frozen=True)
class NotifyConfig:
    """Webhook notification configuration.

    Attributes:
        webhook_url: URL to POST to on run events.
        on_complete: Send notification when run completes successfully.
        on_failure: Send notification when run fails.
        desktop: Enable local OS notifications for task completion/failure.
    """

    webhook_url: str | None = None
    on_complete: bool = True
    on_failure: bool = True
    desktop: bool = False


@dataclass(frozen=True)
class WebhookConfig:
    """Outbound webhook target for lifecycle notifications."""

    url: str
    events: tuple[str, ...]


@dataclass(frozen=True)
class CORSConfig:
    """CORS middleware configuration.

    Attributes:
        allowed_origins: Origins allowed for cross-origin requests.
            Defaults to localhost on any port.
        allow_methods: HTTP methods allowed for cross-origin requests.
        allow_headers: Headers allowed in cross-origin requests.
        allow_credentials: Whether cookies/auth headers are allowed.
        max_age: Seconds the browser may cache preflight responses.
    """

    # HTTP is intentional: these are localhost CORS origin patterns, not
    # remote URLs.  Browsers do not use HTTPS for localhost by default.
    allowed_origins: tuple[str, ...] = (
        "http://localhost:*",
        "http://127.0.0.1:*",
    )
    allow_methods: tuple[str, ...] = ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
    allow_headers: tuple[str, ...] = ("*",)
    allow_credentials: bool = True
    max_age: int = 600


@dataclass(frozen=True)
class NetworkConfig:
    """Network security configuration.

    Attributes:
        allowed_ips: List of allowed IP ranges in CIDR notation.
    """

    allowed_ips: tuple[str, ...] = ()


@dataclass(frozen=True)
class RateLimitBucketConfig:
    """Config for one endpoint rate-limit bucket.

    Attributes:
        name: Bucket identifier for metrics and error messages.
        requests: Maximum requests allowed in the window.
        window_seconds: Sliding-window size in seconds.
        path_prefixes: Route prefixes covered by this bucket.
        methods: Optional HTTP methods to scope the bucket to.
    """

    name: str
    requests: int
    window_seconds: int = 60
    path_prefixes: tuple[str, ...] = ()
    methods: tuple[str, ...] = ()

    def matches(self, path: str, method: str) -> bool:
        """Return whether this bucket applies to the current request."""
        if self.methods and method.upper() not in self.methods:
            return False
        return any(path.startswith(prefix) for prefix in self.path_prefixes)


@dataclass(frozen=True)
class RateLimitConfig:
    """Top-level request rate-limit config."""

    buckets: tuple[RateLimitBucketConfig, ...] = ()

    def match_request(self, path: str, method: str) -> RateLimitBucketConfig | None:
        """Return the first matching bucket for the current request."""
        for bucket in self.buckets:
            if bucket.matches(path, method):
                return bucket
        return None


@dataclass(frozen=True)
class ModelFallbackSeedConfig:
    """Model fallback chain configuration from bernstein.yaml (AGENT-004).

    Attributes:
        fallback_chain: Ordered list of model names to try in sequence.
        strike_limit: Consecutive errors before switching to next model.
        include_timeouts: Whether connection timeouts count as strikes.
        trigger_codes: HTTP status codes that count as strikes.
    """

    fallback_chain: list[str] = field(default_factory=list)
    strike_limit: int = 3
    include_timeouts: bool = True
    trigger_codes: list[int] = field(default_factory=lambda: [429, 503, 529])


@dataclass(frozen=True)
class MetricSchema:
    """Definition of a single custom metric from bernstein.yaml.

    Attributes:
        formula: Arithmetic expression using built-in metric variables.
        unit: Display unit label (e.g. ``"lines/$"``).
        description: Human-readable description of what the metric measures.
        alert_above: Optional threshold — alert when metric exceeds this value.
        alert_below: Optional threshold — alert when metric falls below this value.
    """

    formula: str
    unit: str = ""
    description: str = ""
    alert_above: float | None = None
    alert_below: float | None = None


@dataclass(frozen=True)
class DashboardAuthConfig:
    """Dashboard session authentication configuration.

    Attributes:
        password: Password required to access /dashboard routes.
            Read from ``cors.password`` in bernstein.yaml or the
            ``BERNSTEIN_DASHBOARD_PASSWORD`` env var.
        session_timeout_seconds: How long dashboard sessions remain valid.
    """

    password: str = ""
    session_timeout_seconds: int = 3600


@dataclass(frozen=True)
class SeedConfig:
    """Validated configuration from bernstein.yaml.

    Attributes:
        goal: The high-level project objective (required).
        budget_usd: Spending cap in USD, parsed from "$N" strings. None if unset.
        team: "auto" for automatic role selection, or an explicit list of role names.
        cli: Which CLI agent backend to use.
        max_agents: Maximum number of concurrent agents.
        model: Optional model override for the CLI backend.
        constraints: Project constraints passed to the manager (e.g. "Python only").
        context_files: Additional file paths to include in manager context.
        agent_catalog: Optional path to an Agency agent catalog directory.
        catalogs: Catalog registry built from the ``catalogs`` section of the
            seed file.  Defaults to Agency-only remote mode when absent.
        mcp_servers: MCP server definitions to pass to spawned agents.
        mcp_allowlist: Explicit allowlist of MCP server names permitted for
            this run.  When set, only servers whose names appear in this list
            are included in agent configs; all others are silently blocked.
            ``None`` (the default) means no restriction — all configured
            servers are allowed.
        notify: Optional webhook notification configuration.
        cells: Number of parallel orchestration cells (1 = single-cell).
        quality_gates: Optional quality gate configuration. When set, lint/type/test
            checks run after each task completes and before the approval gate.
        formal_verification: Optional formal verification gateway config. When set,
            Z3/Lean4 property checks run after quality gates and before merge.
    """

    goal: str
    budget_usd: float | None = None
    team: Literal["auto"] | list[str] = "auto"
    cli: Literal["claude", "codex", "gemini", "qwen", "auto"] = "auto"
    max_agents: int = 6
    model: str | None = None
    constraints: tuple[str, ...] = ()
    context_files: tuple[str, ...] = ()
    agent_catalog: str | None = None
    catalogs: CatalogRegistry | None = None
    mcp_servers: dict[str, dict[str, Any]] | None = None
    mcp_allowlist: tuple[str, ...] | None = None
    notify: NotifyConfig | None = None
    storage: StorageConfig | None = None
    cells: int = 1
    cluster: ClusterConfig | None = None
    workspace: Workspace | None = None
    session: SessionConfig = field(default_factory=SessionConfig)
    worktree_setup: WorktreeSetupConfig | None = None
    quality_gates: QualityGatesConfig | None = None
    formal_verification: FormalVerificationConfig | None = None
    secrets: SecretsConfig | None = None
    key_rotation: KeyRotationConfig | None = None
    model_policy: dict[str, Any] | None = None
    role_model_policy: dict[str, dict[str, str]] | None = None
    compliance: ComplianceConfig | None = None
    visual: VisualConfig | None = None
    sandbox: DockerSandbox | None = None
    bridges: BridgeConfigSet | None = None
    batch: BatchConfig = field(default_factory=BatchConfig)
    max_cost_per_agent: float = 0.0
    webhooks: tuple[WebhookConfig, ...] = ()
    test_agent: TestAgentConfig = field(default_factory=TestAgentConfig)
    smtp: SmtpConfig | None = None
    cors: CORSConfig | None = None
    dashboard_auth: DashboardAuthConfig | None = None
    network: NetworkConfig | None = None
    rate_limit: RateLimitConfig | None = None
    tenants: tuple[TenantConfig, ...] = ()
    # audit-150: default 'none' disables evolution/auto_decompose gracefully
    # on fresh clones without OPENROUTER_API_KEY_* env vars. Callers that want
    # the previous behavior must set 'internal_llm_provider: openrouter_free'
    # explicitly and export the matching API key.
    internal_llm_provider: str = "none"
    internal_llm_model: str = "nvidia/nemotron-3-super-120b-a12b"
    model_fallback: ModelFallbackSeedConfig | None = None
    cost_tags: dict[str, str] = field(default_factory=dict)
    cost_autopilot: bool = False
    deployment_strategy: str = "rolling"
    org_policies: list[str] = field(default_factory=list)
    metrics: dict[str, MetricSchema] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """Emit preflight warnings for provider/env mismatches (audit-150)."""
        hint = check_internal_llm_preflight(self.internal_llm_provider)
        if hint is not None:
            logger.warning("audit-150 preflight: %s", hint)
