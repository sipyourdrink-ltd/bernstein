"""Seed file parser for bernstein.yaml.

Reads the project seed configuration, validates it, and produces the
initial manager Task that kicks off orchestration.
"""

from __future__ import annotations

import ipaddress
import os
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, cast
from urllib.parse import urlparse

import yaml

from bernstein.agents.catalog import CatalogRegistry
from bernstein.core.compliance import ComplianceConfig, CompliancePreset
from bernstein.core.formal_verification import FormalProperty, FormalVerificationConfig
from bernstein.core.gate_runner import VALID_GATE_NAMES, GatePipelineStep, normalize_gate_condition
from bernstein.core.key_rotation import KeyRotationConfig, _parse_interval
from bernstein.core.models import (
    BatchConfig,
    BridgeConfigSet,
    ClusterConfig,
    ClusterTopology,
    Complexity,
    OpenClawBridgeConfig,
    Scope,
    SmtpConfig,
    Task,
    TaskStatus,
    TestAgentConfig,
)
from bernstein.core.quality_gates import QualityGatesConfig
from bernstein.core.sandbox import DockerSandbox, parse_docker_sandbox
from bernstein.core.secrets import SecretsConfig
from bernstein.core.tenanting import TenantConfig
from bernstein.core.visual_config import VisualConfig, parse_visual_config
from bernstein.core.workspace import Workspace
from bernstein.core.worktree import WorktreeSetupConfig

if TYPE_CHECKING:
    from pathlib import Path


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


def _parse_model_fallback(raw: object) -> ModelFallbackSeedConfig | None:
    """Parse the optional model_fallback section from bernstein.yaml.

    Args:
        raw: Raw YAML value for the ``model_fallback`` section.

    Returns:
        Parsed ModelFallbackSeedConfig, or None when the section is absent.

    Raises:
        SeedError: If the section is malformed.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SeedError(f"model_fallback must be a mapping, got: {type(raw).__name__}")
    mf: dict[str, object] = cast("dict[str, object]", raw)

    chain_raw = mf.get("fallback_chain")
    chain: list[str] = []
    if chain_raw is not None:
        if not isinstance(chain_raw, list) or not all(isinstance(m, str) for m in chain_raw):
            raise SeedError("model_fallback.fallback_chain must be a list of strings")
        chain = [str(m) for m in chain_raw]

    strike_raw = mf.get("strike_limit", 3)
    if not isinstance(strike_raw, int) or strike_raw < 1:
        raise SeedError(f"model_fallback.strike_limit must be a positive integer, got: {strike_raw!r}")

    include_timeouts_raw = mf.get("include_timeouts", True)
    if not isinstance(include_timeouts_raw, bool):
        raise SeedError(f"model_fallback.include_timeouts must be a bool, got: {type(include_timeouts_raw).__name__}")

    codes_raw = mf.get("trigger_codes", [429, 503, 529])
    if not isinstance(codes_raw, list) or not all(isinstance(c, int) for c in codes_raw):
        raise SeedError("model_fallback.trigger_codes must be a list of integers")

    return ModelFallbackSeedConfig(
        fallback_chain=chain,
        strike_limit=int(strike_raw),
        include_timeouts=include_timeouts_raw,
        trigger_codes=[int(c) for c in codes_raw],
    )


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
    internal_llm_provider: str = "openrouter_free"
    internal_llm_model: str = "nvidia/nemotron-3-super-120b-a12b"
    model_fallback: ModelFallbackSeedConfig | None = None
    cost_tags: dict[str, str] = field(default_factory=dict)
    cost_autopilot: bool = False
    deployment_strategy: str = "rolling"
    org_policies: list[str] = field(default_factory=list)


_BUDGET_RE = re.compile(r"^\$(\d+(?:\.\d+)?)$")
_ENV_REF_RE = re.compile(r"^\$\{([A-Z0-9_]+)\}$")
_VALID_CLIS = frozenset({"claude", "codex", "gemini", "qwen", "auto"})
_ALLOWED_WEBHOOK_EVENTS = frozenset(
    {
        "run.started",
        "task.completed",
        "task.failed",
        "run.completed",
        "budget.warning",
        "approval.needed",
    }
)
_WEBHOOK_EVENT_ALIASES: dict[str, str] = {
    "task.done": "task.completed",
}


def _parse_budget(raw: str | int | float | None) -> float | None:
    """Extract a numeric dollar amount from a budget value.

    Args:
        raw: Value from YAML — may be "$20", 20, 20.0, or None.

    Returns:
        Parsed float amount or None.

    Raises:
        SeedError: If the format is unrecognised.
    """
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return float(raw)
    # At this point raw must be str (the only remaining type).
    m = _BUDGET_RE.match(raw.strip())
    if m:
        return float(m.group(1))
    # Try bare numeric string.
    try:
        return float(raw.strip())
    except ValueError:
        pass
    raise SeedError(f"Invalid budget format: {raw!r}. Expected '$N' or a number.")


def _parse_team(raw: object) -> Literal["auto"] | list[str]:
    """Parse team field — "auto", a list of role strings, or empty list (=> "auto").

    Args:
        raw: Value from YAML.

    Returns:
        "auto" or a non-empty list of role name strings.

    Raises:
        SeedError: If the value is neither "auto" nor a list of strings.
    """
    if raw is None or raw == "auto":
        return "auto"
    if isinstance(raw, list):
        items: list[object] = cast("list[object]", raw)
        if len(items) == 0:
            return "auto"
        if all(isinstance(r, str) for r in items):
            return [str(r) for r in items]
        raise SeedError(f"team list must contain only strings, got: {raw!r}")
    raise SeedError(f"team must be 'auto' or a list of role names, got: {raw!r}")


def _parse_string_list(raw: object, field_name: str) -> tuple[str, ...]:
    """Parse an optional list-of-strings field from YAML.

    Args:
        raw: Value from YAML — should be None or a list of strings.
        field_name: Name of the field, for error messages.

    Returns:
        Tuple of strings (empty if raw is None).

    Raises:
        SeedError: If the value is not None or a list of strings.
    """
    if raw is None:
        return ()
    if isinstance(raw, list):
        items: list[object] = cast("list[object]", raw)
        if all(isinstance(s, str) for s in items):
            return tuple(str(s) for s in items)
    raise SeedError(f"{field_name} must be a list of strings, got: {raw!r}")


def _parse_network_config(raw: object) -> NetworkConfig | None:
    """Parse the optional network config block from ``bernstein.yaml``.

    Args:
        raw: Raw YAML value for the ``network`` section.

    Returns:
        Parsed network config, or ``None`` when the section is absent.

    Raises:
        SeedError: If the network section is malformed or contains invalid CIDRs.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SeedError(f"network must be a mapping, got: {type(raw).__name__}")
    allowed_ips = _parse_string_list(raw.get("allowed_ips"), "network.allowed_ips")
    for ip_range in allowed_ips:
        try:
            ipaddress.ip_network(ip_range, strict=False)
        except ValueError as exc:
            raise SeedError(f"network.allowed_ips contains invalid CIDR {ip_range!r}") from exc
    return NetworkConfig(allowed_ips=allowed_ips)


def _parse_cors_config(raw: object) -> CORSConfig | None:
    """Parse the optional CORS config block from ``bernstein.yaml``.

    Args:
        raw: Raw YAML value for the ``cors`` section.

    Returns:
        Parsed CORS config, or ``None`` when the section is absent.

    Raises:
        SeedError: If the CORS section is malformed.
    """
    if raw is None:
        return None
    if isinstance(raw, bool):
        return CORSConfig() if raw else None
    if not isinstance(raw, dict):
        raise SeedError(f"cors must be a mapping or boolean, got: {type(raw).__name__}")

    cors_dict: dict[str, object] = cast("dict[str, object]", raw)

    origins = _parse_string_list(cors_dict.get("allowed_origins"), "cors.allowed_origins")
    if not origins:
        origins = CORSConfig.allowed_origins

    methods = _parse_string_list(cors_dict.get("allow_methods"), "cors.allow_methods")
    if not methods:
        methods = CORSConfig.allow_methods

    headers = _parse_string_list(cors_dict.get("allow_headers"), "cors.allow_headers")
    if not headers:
        headers = CORSConfig.allow_headers

    credentials_raw = cors_dict.get("allow_credentials", True)
    if not isinstance(credentials_raw, bool):
        raise SeedError(f"cors.allow_credentials must be a bool, got: {type(credentials_raw).__name__}")

    max_age_raw = cors_dict.get("max_age", 600)
    if not isinstance(max_age_raw, int) or max_age_raw < 0:
        raise SeedError(f"cors.max_age must be a non-negative integer, got: {max_age_raw!r}")

    return CORSConfig(
        allowed_origins=origins,
        allow_methods=methods,
        allow_headers=headers,
        allow_credentials=credentials_raw,
        max_age=max_age_raw,
    )


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


def _parse_dashboard_auth(raw: object) -> DashboardAuthConfig | None:
    """Parse the optional dashboard_auth config block from ``bernstein.yaml``.

    Args:
        raw: Raw YAML value for the ``dashboard_auth`` section.

    Returns:
        Parsed dashboard auth config, or ``None`` when the section is absent.

    Raises:
        SeedError: If the section is malformed.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SeedError(f"dashboard_auth must be a mapping, got: {type(raw).__name__}")

    da_dict: dict[str, object] = cast("dict[str, object]", raw)

    password_raw = da_dict.get("password", "")
    if not isinstance(password_raw, str):
        raise SeedError(f"dashboard_auth.password must be a string, got: {type(password_raw).__name__}")
    # Support env var references
    password = str(_expand_env_value(password_raw, "dashboard_auth.password"))

    timeout_raw = da_dict.get("session_timeout_seconds", 3600)
    if not isinstance(timeout_raw, int) or timeout_raw < 0:
        raise SeedError(f"dashboard_auth.session_timeout_seconds must be a non-negative integer, got: {timeout_raw!r}")

    return DashboardAuthConfig(password=password, session_timeout_seconds=timeout_raw)


_DEFAULT_RATE_LIMIT_PATHS: dict[str, tuple[str, ...]] = {
    "auth": ("/auth",),
    "tasks": ("/tasks",),
}


def _parse_rate_limit_bucket(name: str, raw: object) -> RateLimitBucketConfig:
    """Parse one rate-limit bucket definition."""
    if isinstance(raw, int):
        requests = raw
        window_seconds = 60
        path_prefixes = _DEFAULT_RATE_LIMIT_PATHS.get(name, ())
        methods: tuple[str, ...] = ()
    elif isinstance(raw, dict):
        requests_raw = raw.get("requests_per_minute", raw.get("requests"))
        if not isinstance(requests_raw, int) or requests_raw <= 0:
            raise SeedError(f"rate_limit.{name}.requests_per_minute must be a positive integer")
        requests = requests_raw
        window_raw = raw.get("window_seconds", 60)
        if not isinstance(window_raw, int) or window_raw <= 0:
            raise SeedError(f"rate_limit.{name}.window_seconds must be a positive integer")
        window_seconds = window_raw
        path_prefixes = _parse_string_list(raw.get("paths"), f"rate_limit.{name}.paths")
        if not path_prefixes:
            path_prefixes = _DEFAULT_RATE_LIMIT_PATHS.get(name, ())
        methods_raw = _parse_string_list(raw.get("methods"), f"rate_limit.{name}.methods")
        methods = tuple(method.upper() for method in methods_raw)
    else:
        raise SeedError(f"rate_limit.{name} must be an integer or mapping, got: {type(raw).__name__}")

    if requests <= 0:
        raise SeedError(f"rate_limit.{name}.requests_per_minute must be a positive integer")
    if not path_prefixes:
        raise SeedError(f"rate_limit.{name}.paths is required for custom buckets")
    return RateLimitBucketConfig(
        name=name,
        requests=requests,
        window_seconds=window_seconds,
        path_prefixes=path_prefixes,
        methods=methods,
    )


def _parse_rate_limit_config(raw: object) -> RateLimitConfig | None:
    """Parse the optional request rate-limit config block."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SeedError(f"rate_limit must be a mapping, got: {type(raw).__name__}")
    buckets: list[RateLimitBucketConfig] = []
    for name, bucket_raw in raw.items():
        if not isinstance(name, str) or not name:
            raise SeedError("rate_limit bucket names must be non-empty strings")
        buckets.append(_parse_rate_limit_bucket(name, bucket_raw))
    return RateLimitConfig(buckets=tuple(buckets))


def _parse_tenants(raw: object) -> tuple[TenantConfig, ...]:
    """Parse the optional `tenants` config block."""

    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise SeedError(f"tenants must be a list, got: {type(raw).__name__}")
    parsed: list[TenantConfig] = []
    seen: set[str] = set()
    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            raise SeedError(f"tenants[{index}] must be a mapping")
        entry = cast("dict[str, object]", item)
        tenant_id_raw = entry.get("id")
        if not isinstance(tenant_id_raw, str) or not tenant_id_raw.strip():
            raise SeedError(f"tenants[{index}].id must be a non-empty string")
        tenant_id = tenant_id_raw.strip()
        if tenant_id in seen:
            raise SeedError(f"Duplicate tenant id: {tenant_id!r}")
        seen.add(tenant_id)
        budget_usd = _parse_budget(cast("str | int | float | None", entry.get("budget")))
        allowed_agents_raw = entry.get("allowed_agents", entry.get("agents"))
        allowed_agents = _parse_string_list(allowed_agents_raw, f"tenants[{index}].allowed_agents")
        parsed.append(TenantConfig(id=tenant_id, budget_usd=budget_usd, allowed_agents=allowed_agents))
    return tuple(parsed)


def _expand_env_value(raw: object, field_name: str) -> object:
    """Expand exact ``${VAR}`` references for secret-like config values.

    Args:
        raw: Raw scalar from YAML.
        field_name: Field name for validation errors.

    Returns:
        Expanded string when the value is an env reference, otherwise ``raw``.

    Raises:
        SeedError: If the referenced env var is missing or empty.
    """
    if not isinstance(raw, str):
        return raw
    match = _ENV_REF_RE.fullmatch(raw.strip())
    if match is None:
        return raw
    env_name = match.group(1)
    env_value = os.environ.get(env_name)
    if env_value is None or not env_value.strip():
        raise SeedError(f"{field_name} references unset environment variable {env_name!r}")
    return env_value


def _parse_openclaw_runtime_config(raw: object) -> OpenClawBridgeConfig | None:
    """Parse the optional ``bridges.openclaw`` seed section.

    Args:
        raw: Raw YAML value for the OpenClaw bridge.

    Returns:
        Parsed bridge config, or None when the section is absent.

    Raises:
        SeedError: If the shape or values are invalid.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SeedError(f"bridges.openclaw must be a mapping, got: {type(raw).__name__}")

    data = cast("dict[str, object]", raw)
    enabled_raw = data.get("enabled", False)
    if not isinstance(enabled_raw, bool):
        raise SeedError(f"bridges.openclaw.enabled must be a bool, got: {type(enabled_raw).__name__}")

    url_raw = data.get("url", data.get("endpoint", ""))
    url_value = _expand_env_value(url_raw, "bridges.openclaw.url")
    if not isinstance(url_value, str):
        raise SeedError(f"bridges.openclaw.url must be a string, got: {type(url_value).__name__}")
    url_text = url_value.strip()

    api_key_raw = _expand_env_value(data.get("api_key", ""), "bridges.openclaw.api_key")
    if not isinstance(api_key_raw, str):
        raise SeedError(f"bridges.openclaw.api_key must be a string, got: {type(api_key_raw).__name__}")
    api_key = api_key_raw.strip()

    agent_id_raw = data.get("agent_id", "")
    if not isinstance(agent_id_raw, str):
        raise SeedError(f"bridges.openclaw.agent_id must be a string, got: {type(agent_id_raw).__name__}")
    agent_id = agent_id_raw.strip()

    workspace_mode_raw = data.get("workspace_mode", "shared_workspace")
    if workspace_mode_raw != "shared_workspace":
        raise SeedError("bridges.openclaw.workspace_mode must be 'shared_workspace'")

    fallback_raw = data.get("fallback_to_local", True)
    if not isinstance(fallback_raw, bool):
        raise SeedError(f"bridges.openclaw.fallback_to_local must be a bool, got: {type(fallback_raw).__name__}")

    connect_timeout_raw = data.get("connect_timeout_s", 10.0)
    if not isinstance(connect_timeout_raw, (int, float)) or connect_timeout_raw <= 0:
        raise SeedError("bridges.openclaw.connect_timeout_s must be a positive number")

    request_timeout_raw = data.get("request_timeout_s", 30.0)
    if not isinstance(request_timeout_raw, (int, float)) or request_timeout_raw <= 0:
        raise SeedError("bridges.openclaw.request_timeout_s must be a positive number")

    session_prefix_raw = data.get("session_prefix", "bernstein-")
    if not isinstance(session_prefix_raw, str) or not session_prefix_raw.strip():
        raise SeedError("bridges.openclaw.session_prefix must be a non-empty string")

    max_log_bytes_raw = data.get("max_log_bytes", 1_048_576)
    if not isinstance(max_log_bytes_raw, int) or max_log_bytes_raw < 1:
        raise SeedError("bridges.openclaw.max_log_bytes must be a positive integer")

    model_override_raw = data.get("model_override")
    if model_override_raw is not None and (not isinstance(model_override_raw, str) or not model_override_raw.strip()):
        raise SeedError("bridges.openclaw.model_override must be a non-empty string when set")

    if enabled_raw:
        if not url_text:
            raise SeedError("bridges.openclaw.url is required when the bridge is enabled")
        parsed_url = urlparse(url_text)
        if parsed_url.scheme not in {"ws", "wss"} or not parsed_url.netloc:
            raise SeedError("bridges.openclaw.url must be a valid ws:// or wss:// URL")
        if not api_key:
            raise SeedError("bridges.openclaw.api_key is required when the bridge is enabled")
        if not agent_id:
            raise SeedError("bridges.openclaw.agent_id is required when the bridge is enabled")

    return OpenClawBridgeConfig(
        enabled=enabled_raw,
        url=url_text,
        api_key=api_key,
        agent_id=agent_id,
        workspace_mode="shared_workspace",
        fallback_to_local=fallback_raw,
        connect_timeout_s=float(connect_timeout_raw),
        request_timeout_s=float(request_timeout_raw),
        session_prefix=session_prefix_raw.strip(),
        max_log_bytes=max_log_bytes_raw,
        model_override=model_override_raw.strip() if isinstance(model_override_raw, str) else None,
    )


def _parse_bridge_settings(raw: object) -> BridgeConfigSet | None:
    """Parse the optional ``bridges`` section.

    Args:
        raw: Raw YAML value for ``bridges``.

    Returns:
        Parsed bridge settings or None when absent.

    Raises:
        SeedError: If the section is malformed.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SeedError(f"bridges must be a mapping, got: {type(raw).__name__}")
    data = cast("dict[str, object]", raw)
    return BridgeConfigSet(openclaw=_parse_openclaw_runtime_config(data.get("openclaw")))


def _parse_role_model_policy(raw: object) -> dict[str, dict[str, str]] | None:
    """Parse optional role-specific provider/model overrides."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SeedError("role_model_policy must be a mapping of role -> settings")

    parsed: dict[str, dict[str, str]] = {}
    for role, settings in raw.items():
        if not isinstance(role, str) or not role:
            raise SeedError("role_model_policy keys must be non-empty role strings")
        if not isinstance(settings, dict):
            raise SeedError(f"role_model_policy[{role!r}] must be a mapping")

        normalized: dict[str, str] = {}
        for key in ("provider", "model", "effort"):
            value = settings.get(key)
            if value is None:
                continue
            if not isinstance(value, str) or not value:
                raise SeedError(f"role_model_policy[{role!r}][{key!r}] must be a non-empty string")
            normalized[key] = value

        unknown_keys = sorted(set(settings) - {"provider", "model", "effort"})
        if unknown_keys:
            raise SeedError(f"role_model_policy[{role!r}] has unknown keys: {', '.join(unknown_keys)}")
        parsed[role] = normalized
    return parsed


def _normalize_webhook_event(event: str, field_name: str) -> str:
    """Normalize and validate a webhook event name."""
    normalized = _WEBHOOK_EVENT_ALIASES.get(event, event)
    if normalized not in _ALLOWED_WEBHOOK_EVENTS:
        allowed = ", ".join(sorted(_ALLOWED_WEBHOOK_EVENTS | set(_WEBHOOK_EVENT_ALIASES)))
        raise SeedError(f"{field_name} contains unsupported event {event!r}. Allowed: {allowed}")
    return normalized


def _parse_smtp(raw: object) -> SmtpConfig | None:
    """Parse SMTP configuration for email notifications."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SeedError(f"smtp must be a mapping, got: {type(raw).__name__}")

    data = cast("dict[str, object]", raw)
    host = data.get("host")
    if not isinstance(host, str) or not host:
        raise SeedError("smtp.host is required and must be a string")

    port = data.get("port")
    if not isinstance(port, int):
        raise SeedError("smtp.port is required and must be an integer")

    username = data.get("username", "")
    password = data.get("password", "")
    from_addr = data.get("from_address", "")
    to_addrs = _parse_string_list(data.get("to_addresses"), "smtp.to_addresses")

    return SmtpConfig(
        host=host,
        port=port,
        username=str(username),
        password=str(password),
        from_address=str(from_addr),
        to_addresses=list(to_addrs),
    )


def parse_seed(path: Path) -> SeedConfig:
    """Parse a bernstein.yaml seed file into a validated SeedConfig.

    Args:
        path: Path to the bernstein.yaml file.

    Returns:
        Validated SeedConfig dataclass.

    Raises:
        SeedError: If the file is missing, unreadable, or has invalid content.
    """
    if not path.exists():
        raise SeedError(f"Seed file not found: {path}")

    try:
        raw_text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise SeedError(f"Cannot read seed file {path}: {exc}") from exc

    try:
        data_raw: object = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise SeedError(f"Invalid YAML in {path}: {exc}") from exc

    if not isinstance(data_raw, dict):
        raise SeedError(f"Seed file must be a YAML mapping, got {type(data_raw).__name__}")

    data: dict[str, object] = cast("dict[str, object]", data_raw)

    # --- Required fields ---
    goal: object = data.get("goal")
    if not goal or not isinstance(goal, str):
        raise SeedError("Seed file must contain a non-empty 'goal' string.")

    # --- Optional fields ---
    budget_usd = _parse_budget(cast("str | int | float | None", data.get("budget")))
    team = _parse_team(data.get("team"))

    cli_raw: object = data.get("cli", "auto")
    if cli_raw not in _VALID_CLIS:
        raise SeedError(f"cli must be one of {sorted(_VALID_CLIS)}, got: {cli_raw!r}")
    cli = cast("Literal['claude', 'codex', 'gemini', 'qwen', 'auto']", cli_raw)

    max_agents_raw: object = data.get("max_agents", 6)
    if not isinstance(max_agents_raw, int) or max_agents_raw < 1:
        raise SeedError(f"max_agents must be a positive integer, got: {max_agents_raw!r}")

    model_raw: object = data.get("model")
    if model_raw is not None and not isinstance(model_raw, str):
        raise SeedError(f"model must be a string, got: {type(model_raw).__name__}")
    max_cost_per_agent_raw: object = data.get("max_cost_per_agent")
    max_cost_per_agent = 0.0
    if max_cost_per_agent_raw is not None:
        max_cost_per_agent = _parse_budget(cast("str | int | float | None", max_cost_per_agent_raw)) or 0.0
        if max_cost_per_agent < 0:
            raise SeedError(f"max_cost_per_agent must be >= 0, got: {max_cost_per_agent_raw!r}")

    constraints = _parse_string_list(data.get("constraints"), "constraints")
    context_files = _parse_string_list(data.get("context_files"), "context_files")
    role_model_policy = _parse_role_model_policy(data.get("role_model_policy"))

    agent_catalog_raw: object = data.get("agent_catalog")
    if agent_catalog_raw is not None and not isinstance(agent_catalog_raw, str):
        raise SeedError(f"agent_catalog must be a string path, got: {type(agent_catalog_raw).__name__}")

    mcp_servers_raw: object = data.get("mcp_servers")
    if mcp_servers_raw is not None and not isinstance(mcp_servers_raw, dict):
        raise SeedError(f"mcp_servers must be a mapping, got: {type(mcp_servers_raw).__name__}")

    mcp_allowlist_raw: object = data.get("mcp_allowlist")
    mcp_allowlist: tuple[str, ...] | None = (
        None if mcp_allowlist_raw is None else _parse_string_list(mcp_allowlist_raw, "mcp_allowlist")
    )

    catalogs_raw: object = data.get("catalogs")
    catalogs: CatalogRegistry | None = None
    if catalogs_raw is not None:
        if not isinstance(catalogs_raw, list):
            raise SeedError(f"catalogs must be a list, got: {type(catalogs_raw).__name__}")
        catalogs_list: list[dict[str, Any]] = cast("list[dict[str, Any]]", catalogs_raw)
        try:
            catalogs = CatalogRegistry.from_config(catalogs_list)
        except ValueError as exc:
            raise SeedError(f"Invalid catalogs configuration: {exc}") from exc

    notify_raw: object = data.get("notify")
    notify: NotifyConfig | None = None
    if notify_raw is not None:
        if not isinstance(notify_raw, dict):
            raise SeedError(f"notify must be a mapping, got: {type(notify_raw).__name__}")
        notify_dict: dict[str, object] = cast("dict[str, object]", notify_raw)
        webhook_url: object = notify_dict.get("webhook")
        if webhook_url is not None and not isinstance(webhook_url, str):
            raise SeedError(f"notify.webhook must be a string, got: {type(webhook_url).__name__}")
        on_complete: object = notify_dict.get("on_complete", True)
        on_failure: object = notify_dict.get("on_failure", True)
        desktop: object = notify_dict.get("desktop", False)
        if not isinstance(on_complete, bool):
            raise SeedError(f"notify.on_complete must be a bool, got: {type(on_complete).__name__}")
        if not isinstance(on_failure, bool):
            raise SeedError(f"notify.on_failure must be a bool, got: {type(on_failure).__name__}")
        if not isinstance(desktop, bool):
            raise SeedError(f"notify.desktop must be a bool, got: {type(desktop).__name__}")
        notify = NotifyConfig(
            webhook_url=webhook_url,
            on_complete=on_complete,
            on_failure=on_failure,
            desktop=desktop,
        )

    webhooks_raw: object = data.get("webhooks")
    webhooks: tuple[WebhookConfig, ...] = ()
    if webhooks_raw is not None:
        if not isinstance(webhooks_raw, list):
            raise SeedError(f"webhooks must be a list, got: {type(webhooks_raw).__name__}")
        parsed_targets: list[WebhookConfig] = []
        for idx, item in enumerate(webhooks_raw):
            if not isinstance(item, dict):
                raise SeedError(f"webhooks[{idx}] must be a mapping")
            entry = cast("dict[str, object]", item)
            url_raw: object = entry.get("url")
            if not isinstance(url_raw, str) or not url_raw.strip():
                raise SeedError(f"webhooks[{idx}].url must be a non-empty string")
            events_raw: object = entry.get("events")
            events = _parse_string_list(events_raw, f"webhooks[{idx}].events")
            if len(events) == 0:
                raise SeedError(f"webhooks[{idx}].events must contain at least one event")
            normalized_events = tuple(
                _normalize_webhook_event(event_name, f"webhooks[{idx}].events") for event_name in events
            )
            parsed_targets.append(WebhookConfig(url=url_raw.strip(), events=normalized_events))
        webhooks = tuple(parsed_targets)

    storage_raw: object = data.get("storage")
    storage: StorageConfig | None = None
    if storage_raw is not None:
        if not isinstance(storage_raw, dict):
            raise SeedError(f"storage must be a mapping, got: {type(storage_raw).__name__}")
        storage_dict: dict[str, object] = cast("dict[str, object]", storage_raw)
        storage_backend_raw: object = storage_dict.get("backend", "memory")
        _valid_storage_backends = ("memory", "postgres", "redis")
        if storage_backend_raw not in _valid_storage_backends:
            raise SeedError(
                f"storage.backend must be one of {list(_valid_storage_backends)}, got: {storage_backend_raw!r}"
            )
        storage_backend: Literal["memory", "postgres", "redis"] = storage_backend_raw  # narrowed by membership check
        storage_db_url_raw: object = storage_dict.get("database_url")
        storage_db_url: str | None = str(storage_db_url_raw) if storage_db_url_raw is not None else None
        storage_redis_url_raw: object = storage_dict.get("redis_url")
        storage_redis_url: str | None = str(storage_redis_url_raw) if storage_redis_url_raw is not None else None
        storage = StorageConfig(
            backend=storage_backend,
            database_url=storage_db_url,
            redis_url=storage_redis_url,
        )

    cells_raw: object = data.get("cells", 1)
    if not isinstance(cells_raw, int) or cells_raw < 1:
        raise SeedError(f"cells must be a positive integer, got: {cells_raw!r}")

    cluster_raw: object = data.get("cluster")
    cluster: ClusterConfig | None = None
    if cluster_raw is not None:
        if not isinstance(cluster_raw, dict):
            raise SeedError(f"cluster must be a mapping, got: {type(cluster_raw).__name__}")
        cluster_dict: dict[str, object] = cast("dict[str, object]", cluster_raw)
        topology_str: object = cluster_dict.get("topology", "star")
        try:
            topology = ClusterTopology(topology_str)
        except ValueError:
            valid = [t.value for t in ClusterTopology]
            raise SeedError(f"cluster.topology must be one of {valid}, got: {topology_str!r}") from None
        auth_token_raw: object = cluster_dict.get("auth_token")
        auth_token: str | None = str(auth_token_raw) if auth_token_raw is not None else None
        server_url_raw: object = cluster_dict.get("server_url")
        server_url: str | None = str(server_url_raw) if server_url_raw is not None else None
        cluster = ClusterConfig(
            enabled=bool(cluster_dict.get("enabled", False)),
            topology=topology,
            auth_token=auth_token,
            node_heartbeat_interval_s=int(cast("int", cluster_dict.get("node_heartbeat_interval_s", 15))),
            node_timeout_s=int(cast("int", cluster_dict.get("node_timeout_s", 60))),
            server_url=server_url,
            bind_host=str(cluster_dict.get("bind_host", "127.0.0.1")),
        )

    session_raw: object = data.get("session")
    session_cfg = SessionConfig()
    if session_raw is not None:
        if not isinstance(session_raw, dict):
            raise SeedError(f"session must be a mapping, got: {type(session_raw).__name__}")
        session_dict: dict[str, object] = cast("dict[str, object]", session_raw)
        resume_raw: object = session_dict.get("resume", True)
        if not isinstance(resume_raw, bool):
            raise SeedError(f"session.resume must be a bool, got: {type(resume_raw).__name__}")
        stale_raw: object = session_dict.get("stale_after_minutes", 30)
        if not isinstance(stale_raw, int) or stale_raw < 1:
            raise SeedError(f"session.stale_after_minutes must be a positive integer, got: {stale_raw!r}")
        session_cfg = SessionConfig(resume=resume_raw, stale_after_minutes=stale_raw)

    workspace_raw: object = data.get("workspace")
    repos_raw: object = data.get("repos")
    workspace: Workspace | None = None
    if workspace_raw is not None:
        if not isinstance(workspace_raw, dict):
            raise SeedError(f"workspace must be a mapping, got: {type(workspace_raw).__name__}")
        workspace_dict: dict[str, Any] = cast("dict[str, Any]", workspace_raw)
        try:
            workspace = Workspace.from_config(workspace_dict, root=path.parent)
        except ValueError as exc:
            raise SeedError(f"Invalid workspace configuration: {exc}") from exc
    elif repos_raw is not None:
        if not isinstance(repos_raw, list):
            raise SeedError(f"repos must be a list, got: {type(repos_raw).__name__}")
        try:
            workspace = Workspace.from_config({"repos": repos_raw}, root=path.parent)
        except ValueError as exc:
            raise SeedError(f"Invalid repos configuration: {exc}") from exc

    worktree_setup_raw: object = data.get("worktree_setup")
    worktree_setup: WorktreeSetupConfig | None = None
    if worktree_setup_raw is not None:
        if not isinstance(worktree_setup_raw, dict):
            raise SeedError(f"worktree_setup must be a mapping, got: {type(worktree_setup_raw).__name__}")
        ws_dict: dict[str, object] = cast("dict[str, object]", worktree_setup_raw)
        symlink_dirs = _parse_string_list(ws_dict.get("symlink_dirs"), "worktree_setup.symlink_dirs")
        copy_files = _parse_string_list(ws_dict.get("copy_files"), "worktree_setup.copy_files")
        setup_cmd_raw: object = ws_dict.get("setup_command")
        if setup_cmd_raw is not None and not isinstance(setup_cmd_raw, str):
            raise SeedError(f"worktree_setup.setup_command must be a string, got: {type(setup_cmd_raw).__name__}")
        worktree_setup = WorktreeSetupConfig(
            symlink_dirs=symlink_dirs,
            copy_files=copy_files,
            setup_command=setup_cmd_raw if isinstance(setup_cmd_raw, str) else None,
        )

    batch_raw: object = data.get("batch")
    batch = BatchConfig()
    if batch_raw is not None:
        if not isinstance(batch_raw, dict):
            raise SeedError(f"batch must be a mapping, got: {type(batch_raw).__name__}")
        batch_dict: dict[str, object] = cast("dict[str, object]", batch_raw)
        enabled_raw: object = batch_dict.get("enabled", False)
        if not isinstance(enabled_raw, bool):
            raise SeedError(f"batch.enabled must be a bool, got: {type(enabled_raw).__name__}")
        eligible = list(_parse_string_list(batch_dict.get("eligible"), "batch.eligible"))
        batch = BatchConfig(enabled=enabled_raw, eligible=eligible)

    test_agent_raw: object = data.get("test_agent")
    test_agent = TestAgentConfig()
    if test_agent_raw is not None:
        if not isinstance(test_agent_raw, dict):
            raise SeedError(f"test_agent must be a mapping, got: {type(test_agent_raw).__name__}")
        test_agent_dict: dict[str, object] = cast("dict[str, object]", test_agent_raw)
        always_spawn_raw: object = test_agent_dict.get("always_spawn", False)
        if not isinstance(always_spawn_raw, bool):
            raise SeedError(f"test_agent.always_spawn must be a bool, got: {type(always_spawn_raw).__name__}")
        model_value_raw: object = test_agent_dict.get("model", "sonnet")
        if not isinstance(model_value_raw, str) or not model_value_raw.strip():
            raise SeedError("test_agent.model must be a non-empty string")
        trigger_raw: object = test_agent_dict.get("trigger", "on_task_complete")
        if not isinstance(trigger_raw, str):
            raise SeedError(f"test_agent.trigger must be a string, got: {type(trigger_raw).__name__}")
        if trigger_raw != "on_task_complete":
            raise SeedError("test_agent.trigger must be 'on_task_complete'")
        test_agent = TestAgentConfig(
            always_spawn=always_spawn_raw,
            model=model_value_raw.strip(),
            trigger="on_task_complete",
        )

    model_policy_raw: object = data.get("model_policy")
    model_policy: dict[str, Any] | None = None
    if model_policy_raw is not None:
        if not isinstance(model_policy_raw, dict):
            raise SeedError(f"model_policy must be a mapping, got: {type(model_policy_raw).__name__}")
        model_policy = cast("dict[str, Any]", model_policy_raw)

    quality_gates_raw: object = data.get("quality_gates")
    quality_gates: QualityGatesConfig | None = None
    if quality_gates_raw is not None:
        if not isinstance(quality_gates_raw, dict):
            raise SeedError(f"quality_gates must be a mapping, got: {type(quality_gates_raw).__name__}")
        qg_dict: dict[str, object] = cast("dict[str, object]", quality_gates_raw)

        def _qg_bool(key: str, default: bool) -> bool:
            val = qg_dict.get(key, default)
            if not isinstance(val, bool):
                raise SeedError(f"quality_gates.{key} must be a bool, got: {type(val).__name__}")
            return val

        def _qg_str(key: str, default: str) -> str:
            val = qg_dict.get(key, default)
            if not isinstance(val, str):
                raise SeedError(f"quality_gates.{key} must be a string, got: {type(val).__name__}")
            return val

        def _qg_int(key: str, default: int) -> int:
            val = qg_dict.get(key, default)
            if not isinstance(val, int):
                raise SeedError(f"quality_gates.{key} must be an integer, got: {type(val).__name__}")
            return val

        def _qg_optional_str(key: str) -> str | None:
            val = qg_dict.get(key)
            if val is None:
                return None
            if not isinstance(val, str):
                raise SeedError(f"quality_gates.{key} must be a string, got: {type(val).__name__}")
            return val

        def _qg_str_list(key: str, default: list[str]) -> list[str]:
            raw = qg_dict.get(key, default)
            if not isinstance(raw, list):
                raise SeedError(f"quality_gates.{key} must be a list, got: {type(raw).__name__}")
            if not all(isinstance(item, str) for item in raw):
                raise SeedError(f"quality_gates.{key} must contain only strings")
            return [str(item) for item in raw]

        pipeline_raw = qg_dict.get("pipeline")
        pipeline: list[GatePipelineStep] | None = None
        if pipeline_raw is not None:
            if not isinstance(pipeline_raw, list):
                raise SeedError(f"quality_gates.pipeline must be a list, got: {type(pipeline_raw).__name__}")
            pipeline = []
            for index, entry in enumerate(pipeline_raw):
                if not isinstance(entry, dict):
                    raise SeedError(f"quality_gates.pipeline[{index}] must be a mapping")
                name = entry.get("name")
                if not isinstance(name, str):
                    raise SeedError(f"quality_gates.pipeline[{index}].name must be a string")
                if name not in VALID_GATE_NAMES:
                    raise SeedError(f"quality_gates.pipeline[{index}].name is unsupported: {name!r}")
                required = entry.get("required", True)
                if not isinstance(required, bool):
                    raise SeedError(f"quality_gates.pipeline[{index}].required must be a bool")
                condition_raw = entry.get("condition", "always")
                if not isinstance(condition_raw, str):
                    raise SeedError(f"quality_gates.pipeline[{index}].condition must be a string")
                command_override = entry.get("command_override")
                if command_override is not None and not isinstance(command_override, str):
                    raise SeedError(f"quality_gates.pipeline[{index}].command_override must be a string")
                try:
                    condition = normalize_gate_condition(condition_raw)
                except ValueError as exc:
                    raise SeedError(str(exc)) from exc
                pipeline.append(
                    GatePipelineStep(
                        name=name,
                        required=required,
                        condition=condition,
                        command_override=command_override,
                    )
                )

        # PII scan paths default
        pii_scan_paths_raw = qg_dict.get("pii_scan_paths", ["src/"])
        if not isinstance(pii_scan_paths_raw, list):
            raise SeedError(f"quality_gates.pii_scan_paths must be a list, got: {type(pii_scan_paths_raw).__name__}")
        pii_scan_paths = [str(p) for p in pii_scan_paths_raw]

        quality_gates = QualityGatesConfig(
            enabled=_qg_bool("enabled", True),
            lint=_qg_bool("lint", True),
            lint_command=_qg_str("lint_command", "ruff check ."),
            type_check=_qg_bool("type_check", False),
            type_check_command=_qg_str("type_check_command", "pyright"),
            tests=_qg_bool("tests", False),
            test_command=_qg_str("test_command", "uv run python scripts/run_tests.py -x"),
            timeout_s=_qg_int("timeout_s", 120),
            pipeline=pipeline,
            allow_bypass=_qg_bool("allow_bypass", False),
            cache_enabled=_qg_bool("cache_enabled", True),
            base_ref=_qg_str("base_ref", "main"),
            pii_scan=_qg_bool("pii_scan", True),
            pii_scan_paths=pii_scan_paths,
            pii_ignore_paths=_qg_str_list("pii_ignore_paths", []),
            pii_allowlist_prefixes=_qg_str_list(
                "pii_allowlist_prefixes",
                ["FAKE", "TEST", "EXAMPLE", "DUMMY", "PLACEHOLDER", "LOCALHOST"],
            ),
            security_scan=_qg_bool("security_scan", False),
            security_scan_command=_qg_optional_str("security_scan_command"),
            coverage_delta=_qg_bool("coverage_delta", False),
            coverage_delta_command=_qg_optional_str("coverage_delta_command"),
            complexity_check=_qg_bool("complexity_check", False),
            complexity_threshold=float(qg_dict.get("complexity_threshold", 0.20)),
            complexity_check_command=_qg_optional_str("complexity_check_command"),
            dead_code_check=_qg_bool("dead_code_check", False),
            dead_code_command=_qg_str("dead_code_command", "vulture"),
            dead_code_min_confidence=_qg_int("dead_code_min_confidence", 80),
            import_cycle_check=_qg_bool("import_cycle_check", False),
            import_cycle_command=_qg_optional_str("import_cycle_command"),
            merge_conflict_check=_qg_bool("merge_conflict_check", False),
            flaky_detection=_qg_bool("flaky_detection", False),
            flaky_min_runs=_qg_int("flaky_min_runs", 5),
            flaky_threshold=float(qg_dict.get("flaky_threshold", 0.15)),
        )

    formal_verification_raw: object = data.get("formal_verification")
    formal_verification: FormalVerificationConfig | None = None
    if formal_verification_raw is not None:
        if not isinstance(formal_verification_raw, dict):
            raise SeedError(f"formal_verification must be a mapping, got: {type(formal_verification_raw).__name__}")
        fv_dict: dict[str, object] = cast("dict[str, object]", formal_verification_raw)
        fv_enabled = fv_dict.get("enabled", True)
        if not isinstance(fv_enabled, bool):
            raise SeedError(f"formal_verification.enabled must be a bool, got: {type(fv_enabled).__name__}")
        fv_block = fv_dict.get("block_on_violation", True)
        if not isinstance(fv_block, bool):
            raise SeedError(f"formal_verification.block_on_violation must be a bool, got: {type(fv_block).__name__}")
        fv_timeout = fv_dict.get("timeout_s", 60)
        if not isinstance(fv_timeout, int):
            raise SeedError(f"formal_verification.timeout_s must be an integer, got: {type(fv_timeout).__name__}")
        fv_properties: list[FormalProperty] = []
        props_raw = fv_dict.get("properties", [])
        if not isinstance(props_raw, list):
            raise SeedError("formal_verification.properties must be a list")
        for idx, entry in enumerate(props_raw):
            if not isinstance(entry, dict):
                raise SeedError(f"formal_verification.properties[{idx}] must be a mapping")
            prop_name = entry.get("name", f"property_{idx}")
            if not isinstance(prop_name, str):
                raise SeedError(f"formal_verification.properties[{idx}].name must be a string")
            prop_invariant = entry.get("invariant", "True")
            if not isinstance(prop_invariant, str):
                raise SeedError(f"formal_verification.properties[{idx}].invariant must be a string")
            prop_checker = entry.get("checker", "z3")
            if not isinstance(prop_checker, str) or prop_checker not in ("z3", "lean4"):
                raise SeedError(
                    f"formal_verification.properties[{idx}].checker must be 'z3' or 'lean4', got: {prop_checker!r}"
                )
            prop_lemmas = entry.get("lemmas_file")
            if prop_lemmas is not None and not isinstance(prop_lemmas, str):
                raise SeedError(f"formal_verification.properties[{idx}].lemmas_file must be a string")
            from typing import Literal as _Literal

            fv_properties.append(
                FormalProperty(
                    name=prop_name,
                    invariant=prop_invariant,
                    checker=cast("_Literal['z3', 'lean4']", prop_checker),
                    lemmas_file=prop_lemmas if isinstance(prop_lemmas, str) else None,
                )
            )
        formal_verification = FormalVerificationConfig(
            enabled=fv_enabled,
            properties=fv_properties,
            timeout_s=fv_timeout,
            block_on_violation=fv_block,
        )

    secrets_raw: object = data.get("secrets")
    secrets: SecretsConfig | None = None
    if secrets_raw is not None:
        if not isinstance(secrets_raw, dict):
            raise SeedError(f"secrets must be a mapping, got: {type(secrets_raw).__name__}")
        secrets_dict: dict[str, object] = cast("dict[str, object]", secrets_raw)
        secrets_provider_raw: object = secrets_dict.get("provider")
        if not isinstance(secrets_provider_raw, str):
            raise SeedError("secrets.provider is required and must be a string")
        from bernstein.core.secrets import _VALID_PROVIDERS

        if secrets_provider_raw not in _VALID_PROVIDERS:
            raise SeedError(
                f"secrets.provider must be one of {sorted(_VALID_PROVIDERS)}, got: {secrets_provider_raw!r}"
            )
        secrets_path_raw: object = secrets_dict.get("path")
        if not isinstance(secrets_path_raw, str):
            raise SeedError("secrets.path is required and must be a string")
        secrets_ttl_raw: object = secrets_dict.get("ttl", 300)
        if not isinstance(secrets_ttl_raw, int) or secrets_ttl_raw < 0:
            raise SeedError(f"secrets.ttl must be a non-negative integer, got: {secrets_ttl_raw!r}")
        field_map_raw: object = secrets_dict.get("field_map")
        field_map: dict[str, str] = {}
        if field_map_raw is not None:
            if not isinstance(field_map_raw, dict):
                raise SeedError(f"secrets.field_map must be a mapping, got: {type(field_map_raw).__name__}")
            for fk, fv in cast("dict[str, object]", field_map_raw).items():
                if not isinstance(fv, str):
                    raise SeedError(f"secrets.field_map values must be strings, got: {type(fv).__name__}")
                field_map[str(fk)] = fv
        secrets = SecretsConfig(
            provider=secrets_provider_raw,  # type: ignore[arg-type]
            path=secrets_path_raw,
            ttl=secrets_ttl_raw,
            field_map=field_map,
        )

    # ---- key_rotation ----
    kr_raw: object = data.get("key_rotation")
    key_rotation: KeyRotationConfig | None = None
    if kr_raw is not None:
        if not isinstance(kr_raw, dict):
            raise SeedError(f"key_rotation must be a mapping, got: {type(kr_raw).__name__}")
        kr_dict: dict[str, object] = cast("dict[str, object]", kr_raw)

        kr_interval_raw: object = kr_dict.get("interval", 2592000)
        try:
            if isinstance(kr_interval_raw, (str, int)):
                kr_interval = _parse_interval(kr_interval_raw)
            else:
                raise SeedError(f"key_rotation.interval must be a string or int, got: {type(kr_interval_raw).__name__}")
        except ValueError as exc:
            raise SeedError(f"key_rotation.interval: {exc}") from exc

        kr_on_leak_raw: object = kr_dict.get("on_leak", "revoke_immediately")
        _valid_policies = ("revoke_immediately", "revoke_after_rotation", "alert_only")
        if not isinstance(kr_on_leak_raw, str) or kr_on_leak_raw not in _valid_policies:
            raise SeedError(f"key_rotation.on_leak must be one of {list(_valid_policies)}, got: {kr_on_leak_raw!r}")

        kr_provider_raw: object = kr_dict.get("secrets_provider")
        kr_provider: str | None = None
        if kr_provider_raw is not None:
            if not isinstance(kr_provider_raw, str):
                raise SeedError("key_rotation.secrets_provider must be a string")
            kr_provider = kr_provider_raw

        kr_path_raw: object = kr_dict.get("secrets_path")
        kr_path: str | None = None
        if kr_path_raw is not None:
            if not isinstance(kr_path_raw, str):
                raise SeedError("key_rotation.secrets_path must be a string")
            kr_path = kr_path_raw

        kr_patterns_raw: object = kr_dict.get("leak_patterns")
        kr_patterns: list[str] = []
        if kr_patterns_raw is not None:
            if not isinstance(kr_patterns_raw, list):
                raise SeedError(f"key_rotation.leak_patterns must be a list, got: {type(kr_patterns_raw).__name__}")
            kr_patterns = [str(p) for p in kr_patterns_raw]

        key_rotation = KeyRotationConfig(
            interval_seconds=kr_interval,
            on_leak=kr_on_leak_raw,  # type: ignore[arg-type]
            secrets_provider=kr_provider,
            secrets_path=kr_path,
            leak_patterns=kr_patterns,
        )

    compliance_raw: object = data.get("compliance")
    compliance: ComplianceConfig | None = None
    if compliance_raw is not None:
        if isinstance(compliance_raw, str):
            # Simple preset name: compliance: standard
            _valid_presets = tuple(p.value for p in CompliancePreset)
            if compliance_raw.lower() not in _valid_presets:
                raise SeedError(
                    f"compliance must be one of {list(_valid_presets)} or a mapping, got: {compliance_raw!r}"
                )
            compliance = ComplianceConfig.from_preset(CompliancePreset(compliance_raw.lower()))
        elif isinstance(compliance_raw, dict):
            compliance = ComplianceConfig.from_dict(cast("dict[str, Any]", compliance_raw))
        else:
            raise SeedError(f"compliance must be a string or mapping, got: {type(compliance_raw).__name__}")

    visual_raw: object = data.get("visual")
    visual: VisualConfig | None = None
    if visual_raw is not None:
        try:
            visual = parse_visual_config(visual_raw)
        except ValueError as exc:
            raise SeedError(str(exc)) from exc

    sandbox_raw: object = data.get("sandbox")
    sandbox: DockerSandbox | None = None
    if sandbox_raw is not None:
        try:
            sandbox = parse_docker_sandbox(sandbox_raw)
        except ValueError as exc:
            raise SeedError(str(exc)) from exc

    bridges = _parse_bridge_settings(data.get("bridges"))

    cors = _parse_cors_config(data.get("cors"))
    dashboard_auth = _parse_dashboard_auth(data.get("dashboard_auth"))
    network = _parse_network_config(data.get("network"))
    rate_limit = _parse_rate_limit_config(data.get("rate_limit"))

    tenants = _parse_tenants(data.get("tenants"))

    # --- Internal LLM provider / model ---
    internal_llm_provider_raw: object = data.get("internal_llm_provider", "openrouter_free")
    if not isinstance(internal_llm_provider_raw, str):
        raise SeedError(f"internal_llm_provider must be a string, got: {type(internal_llm_provider_raw).__name__}")
    internal_llm_model_raw: object = data.get("internal_llm_model", "nvidia/nemotron-3-super-120b-a12b")
    if not isinstance(internal_llm_model_raw, str):
        raise SeedError(f"internal_llm_model must be a string, got: {type(internal_llm_model_raw).__name__}")

    model_fallback = _parse_model_fallback(data.get("model_fallback"))

    # --- Cost allocation tags ---
    cost_tags_raw: object = data.get("cost_tags", {})
    if not isinstance(cost_tags_raw, dict):
        raise SeedError(f"cost_tags must be a mapping, got: {type(cost_tags_raw).__name__}")
    cost_tags: dict[str, str] = {str(k): str(v) for k, v in cost_tags_raw.items()}

    # --- Cost autopilot ---
    cost_autopilot_raw: object = data.get("cost_autopilot", False)
    if not isinstance(cost_autopilot_raw, bool):
        raise SeedError(f"cost_autopilot must be a boolean, got: {type(cost_autopilot_raw).__name__}")

    # --- Deployment strategy ---
    deployment_strategy_raw: object = data.get("deployment_strategy", "rolling")
    if not isinstance(deployment_strategy_raw, str):
        raise SeedError(f"deployment_strategy must be a string, got: {type(deployment_strategy_raw).__name__}")

    # --- Org policies ---
    org_policies_raw: object = data.get("org_policies", [])
    if not isinstance(org_policies_raw, list):
        raise SeedError(f"org_policies must be a list of file paths, got: {type(org_policies_raw).__name__}")
    org_policies: list[str] = [str(p) for p in org_policies_raw]

    return SeedConfig(
        goal=goal,
        budget_usd=budget_usd,
        team=team,
        cli=cli,
        max_agents=max_agents_raw,
        model=model_raw,
        max_cost_per_agent=max_cost_per_agent,
        constraints=constraints,
        context_files=context_files,
        agent_catalog=agent_catalog_raw,
        catalogs=catalogs,
        mcp_servers=cast("dict[str, dict[str, Any]] | None", mcp_servers_raw),
        mcp_allowlist=mcp_allowlist if mcp_allowlist is not None else None,
        notify=notify,
        webhooks=webhooks,
        storage=storage,
        cells=cells_raw,
        cluster=cluster,
        workspace=workspace,
        session=session_cfg,
        worktree_setup=worktree_setup,
        secrets=secrets,
        key_rotation=key_rotation,
        quality_gates=quality_gates,
        formal_verification=formal_verification,
        model_policy=model_policy,
        role_model_policy=role_model_policy,
        compliance=compliance,
        visual=visual,
        sandbox=sandbox,
        bridges=bridges,
        batch=batch,
        test_agent=test_agent,
        smtp=_parse_smtp(data.get("smtp")),
        cors=cors,
        dashboard_auth=dashboard_auth,
        network=network,
        rate_limit=rate_limit,
        tenants=tenants,
        internal_llm_provider=internal_llm_provider_raw,
        internal_llm_model=internal_llm_model_raw,
        model_fallback=model_fallback,
        cost_tags=cost_tags,
        cost_autopilot=cost_autopilot_raw,
        deployment_strategy=deployment_strategy_raw,
        org_policies=org_policies,
    )


def seed_to_initial_task(seed: SeedConfig, workdir: Path | None = None) -> Task:
    """Create the initial manager task from a seed configuration.

    The manager task is the entry point for orchestration: it receives
    the project goal, constraints, and context and is responsible for
    decomposing it into subtasks.

    Args:
        seed: Validated seed configuration.
        workdir: Project working directory, used to resolve context_files.

    Returns:
        A Task assigned to the "manager" role with priority 10 (highest).
    """
    description = _build_manager_description(seed, workdir)
    return Task(
        id="task-000",
        title="Initial goal",
        description=description,
        role="manager",
        priority=10,
        scope=Scope.LARGE,
        complexity=Complexity.HIGH,
        estimated_minutes=0,
        status=TaskStatus.OPEN,
    )


def _build_manager_description(seed: SeedConfig, workdir: Path | None) -> str:
    """Build the full manager task description from seed config.

    Assembles the goal, team preference, budget, constraints, and any
    context file contents into a structured description.

    Args:
        seed: Validated seed configuration.
        workdir: Project root for resolving relative context_files paths.

    Returns:
        Formatted description string for the manager task.
    """
    parts: list[str] = [f"## Goal\n{seed.goal}"]

    # Team preference
    if seed.team != "auto":
        parts.append(f"## Team\nRoles: {', '.join(seed.team)}")

    # Budget
    if seed.budget_usd is not None:
        parts.append(f"## Budget\nMax spend: ${seed.budget_usd:.2f}")

    # Constraints
    if seed.constraints:
        lines = "\n".join(f"- {c}" for c in seed.constraints)
        parts.append(f"## Constraints\n{lines}")

    # Context files
    if seed.context_files and workdir is not None:
        context_parts: list[str] = []
        for rel_path in seed.context_files:
            full_path = workdir / rel_path
            if full_path.is_file():
                try:
                    content = full_path.read_text(encoding="utf-8")
                    context_parts.append(f"### {rel_path}\n```\n{content}\n```")
                except OSError:
                    context_parts.append(f"### {rel_path}\n(could not read file)")
            else:
                context_parts.append(f"### {rel_path}\n(file not found)")
        if context_parts:
            parts.append("## Context files\n" + "\n\n".join(context_parts))

    return "\n\n".join(parts)


# ---------------------------------------------------------------------------
# Feature gate provenance (T501, T503, T504, T505, T558)
# ---------------------------------------------------------------------------


@dataclass
class FeatureGateEntry:
    """A single feature gate with provenance and staleness tracking.

    Attributes:
        name: Gate identifier.
        enabled: Whether the gate is currently enabled.
        source: Where the gate value came from (``"override_file"``,
            ``"seed"``, ``"default"``).
        override_file: Path to the override file, if applicable.
        refreshed_at: Unix timestamp of last refresh.
        stale_after_seconds: Age threshold for staleness alarms.
        experiment_id: Optional experiment ID for exposure mirroring.
    """

    name: str
    enabled: bool
    source: Literal["override_file", "seed", "default"] = "default"
    override_file: str | None = None
    refreshed_at: float = field(default_factory=time.time)
    stale_after_seconds: float = 3600.0
    experiment_id: str | None = None

    def is_stale(self) -> bool:
        """Return True if the gate value has not been refreshed recently."""
        return (time.time() - self.refreshed_at) > self.stale_after_seconds

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-compatible dict."""
        return {
            "name": self.name,
            "enabled": self.enabled,
            "source": self.source,
            "override_file": self.override_file,
            "refreshed_at": self.refreshed_at,
            "stale_after_seconds": self.stale_after_seconds,
            "experiment_id": self.experiment_id,
            "is_stale": self.is_stale(),
        }


class FeatureGateRegistry:
    """Session-stable registry of feature gates with provenance.

    Gates are latched at session start and cannot change mid-session
    (T558 — session-stable flag latching).  Staleness alarms fire when
    a gate has not been refreshed within its ``stale_after_seconds``
    window (T505).

    Args:
        gates: Initial gate entries.
    """

    def __init__(self, gates: list[FeatureGateEntry] | None = None) -> None:
        self._gates: dict[str, FeatureGateEntry] = {}
        self._latched: bool = False
        for gate in gates or []:
            self._gates[gate.name] = gate

    def register(self, gate: FeatureGateEntry) -> None:
        """Register a gate.  Raises if the registry is already latched.

        Args:
            gate: Gate entry to register.

        Raises:
            RuntimeError: If the registry has been latched.
        """
        if self._latched:
            raise RuntimeError(f"FeatureGateRegistry is latched — cannot register gate '{gate.name}' mid-session")
        self._gates[gate.name] = gate

    def latch(self) -> None:
        """Latch the registry, preventing further changes."""
        self._latched = True

    @property
    def is_latched(self) -> bool:
        """True after :meth:`latch` has been called."""
        return self._latched

    def is_enabled(self, name: str, *, default: bool = False) -> bool:
        """Return whether *name* is enabled.

        Args:
            name: Gate name.
            default: Value to return when the gate is not registered.

        Returns:
            Gate enabled state, or *default* if not found.
        """
        gate = self._gates.get(name)
        return gate.enabled if gate is not None else default

    def stale_gates(self) -> list[FeatureGateEntry]:
        """Return all gates that have exceeded their staleness threshold."""
        return [g for g in self._gates.values() if g.is_stale()]

    def experiment_exposures(self) -> list[dict[str, Any]]:
        """Return experiment exposure records for metrics mirroring (T504)."""
        return [
            {"experiment_id": g.experiment_id, "gate": g.name, "enabled": g.enabled, "ts": g.refreshed_at}
            for g in self._gates.values()
            if g.experiment_id is not None
        ]

    def to_snapshot(self) -> dict[str, Any]:
        """Serialise the full registry state for persistence (T553 pattern)."""
        return {
            "latched": self._latched,
            "captured_at": time.time(),
            "gates": {name: gate.to_dict() for name, gate in self._gates.items()},
        }

    def __len__(self) -> int:
        return len(self._gates)

    def __iter__(self):  # type: ignore[override]
        return iter(self._gates.values())


def load_feature_gate_override_file(path: Path) -> dict[str, bool]:
    """Load and validate a feature gate override YAML file (T503).

    The file must be a YAML mapping of gate name → bool.  Any non-bool
    value raises :class:`SeedError`.

    Args:
        path: Path to the override file.

    Returns:
        Mapping of gate name → enabled state.

    Raises:
        SeedError: If the file is missing, unreadable, or contains invalid
            values.
    """
    from pathlib import Path as _Path

    p = _Path(path) if not isinstance(path, _Path.__class__) else path  # type: ignore[arg-type]
    try:
        raw = yaml.safe_load(p.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SeedError(f"Feature gate override file not found: {path}") from None
    except Exception as exc:
        raise SeedError(f"Failed to read feature gate override file {path}: {exc}") from exc

    if not isinstance(raw, dict):
        raise SeedError(f"Feature gate override file must be a YAML mapping, got {type(raw).__name__}: {path}")

    result: dict[str, bool] = {}
    for key, value in cast("dict[object, object]", raw).items():
        if not isinstance(value, bool):
            raise SeedError(
                f"Feature gate override file {path}: key '{key}' must be a bool, got {type(value).__name__}"
            )
        result[str(key)] = value
    return result


# ---------------------------------------------------------------------------
# Dynamic config snapshot export (T502)
# ---------------------------------------------------------------------------


@dataclass
class ConfigSnapshot:
    """Point-in-time snapshot of the effective Bernstein configuration.

    Suitable for export via ``/status`` or ``bernstein status --config``.

    Attributes:
        captured_at: Unix timestamp when the snapshot was taken.
        seed_path: Path to the bernstein.yaml that was parsed.
        effective_config: Key → value mapping of all resolved settings.
        feature_gates: Serialised feature gate registry snapshot.
        stale_gate_names: Names of gates that have exceeded their staleness
            threshold at capture time.
    """

    captured_at: float
    seed_path: str
    effective_config: dict[str, Any]
    feature_gates: dict[str, Any]
    stale_gate_names: list[str]

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a JSON-compatible dict."""
        return {
            "captured_at": self.captured_at,
            "seed_path": self.seed_path,
            "effective_config": self.effective_config,
            "feature_gates": self.feature_gates,
            "stale_gate_names": self.stale_gate_names,
        }


def build_config_snapshot(
    seed: SeedConfig,
    seed_path: Path,
    gate_registry: FeatureGateRegistry | None = None,
) -> ConfigSnapshot:
    """Build a :class:`ConfigSnapshot` from a parsed seed and optional gate registry.

    Args:
        seed: Parsed seed configuration.
        seed_path: Path to the bernstein.yaml file.
        gate_registry: Optional feature gate registry.

    Returns:
        Populated :class:`ConfigSnapshot`.
    """
    effective: dict[str, Any] = {
        "goal": seed.goal,
        "budget_usd": seed.budget_usd,
        "team": seed.team,
        "cli": seed.cli,
        "max_agents": seed.max_agents,
        "model": seed.model,
        "cells": seed.cells,
    }
    registry = gate_registry or FeatureGateRegistry()
    stale = [g.name for g in registry.stale_gates()]
    return ConfigSnapshot(
        captured_at=time.time(),
        seed_path=str(seed_path),
        effective_config=effective,
        feature_gates=registry.to_snapshot(),
        stale_gate_names=stale,
    )
