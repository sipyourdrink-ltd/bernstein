"""YAML parsing logic for bernstein.yaml seed files.

Contains ``parse_seed()`` and all ``_parse_*`` helper functions plus
parsing constants. The parent ``seed`` module re-exports every name for
backward compatibility.
"""

from __future__ import annotations

import ipaddress
import logging
import os
import re
from contextlib import suppress
from itertools import starmap
from typing import TYPE_CHECKING, Any, Literal, cast
from urllib.parse import urlparse

import yaml

from bernstein.agents.catalog import CatalogRegistry
from bernstein.core.compliance import ComplianceConfig, CompliancePreset
from bernstein.core.config.seed_config import (
    CORSConfig,
    DashboardAuthConfig,
    GithubConfig,
    MetricSchema,
    ModelFallbackSeedConfig,
    NetworkConfig,
    NotifyConfig,
    RateLimitBucketConfig,
    RateLimitConfig,
    SeedConfig,
    SeedError,
    SessionConfig,
    StorageConfig,
    WebhookConfig,
)
from bernstein.core.config.visual_config import parse_visual_config
from bernstein.core.formal_verification import FormalProperty, FormalVerificationConfig
from bernstein.core.gate_runner import VALID_GATE_NAMES, GatePipelineStep, normalize_gate_condition
from bernstein.core.key_rotation import KeyRotationConfig, _parse_interval
from bernstein.core.models import (
    BatchConfig,
    BridgeConfigSet,
    ClusterConfig,
    ClusterTopology,
    OpenClawBridgeConfig,
    SmtpConfig,
    TestAgentConfig,
)
from bernstein.core.quality_gates import BenchmarkConfig, QualityGatesConfig
from bernstein.core.sandbox import parse_docker_sandbox
from bernstein.core.secrets import SecretsConfig
from bernstein.core.tenanting import TenantConfig
from bernstein.core.workspace import Workspace
from bernstein.core.worktree import WorktreeSetupConfig

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# Type alias for the common cast target used when parsing untyped YAML dicts.
type _StrObjDict = dict[str, object]


_BUDGET_RE = re.compile(r"^\$(\d+(?:\.\d+)?)$")
_ENV_REF_RE = re.compile(r"^\$\{([A-Z0-9_]+)\}$")
_VALID_CLIS = frozenset({"claude", "codex", "gemini", "qwen", "opencode", "aider", "auto"})
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

_DEFAULT_RATE_LIMIT_PATHS: dict[str, tuple[str, ...]] = {
    "auth": ("/auth",),
    "tasks": ("/tasks",),
}


# Shared cast-type constants to avoid string duplication (Sonar S1192).
_CAST_DICT_STR_ANY = "dict[str, Any]"
_CAST_STR_INT_FLOAT_NONE = "str | int | float | None"


def _parse_budget(raw: str | int | float | None) -> float | None:
    """Extract a numeric dollar amount from a budget value.

    Args:
        raw: Value from YAML - may be "$20", 20, 20.0, or None.

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
    with suppress(ValueError):
        return float(raw.strip())
    raise SeedError(f"Invalid budget format: {raw!r}. Expected '$N' or a number.")


def _parse_team(raw: object) -> Literal["auto"] | list[str]:
    """Parse team field - "auto", a list of role strings, or empty list (=> "auto").

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
        if not items:
            return "auto"
        if all(isinstance(r, str) for r in items):
            return [str(r) for r in items]
        raise SeedError(f"team list must contain only strings, got: {raw!r}")
    raise SeedError(f"team must be 'auto' or a list of role names, got: {raw!r}")


def _expand_team_manifest(
    raw_ref: object,
    *,
    raw_team: object,
    raw_role_policy: object,
    workdir: Path,
) -> tuple[list[str], object, str, str]:
    """Expand a ``team_manifest: <name>[@sha256]`` reference (issue #2248).

    A pure front-end over the existing structures: the manifest is
    resolved from ``templates/teams/`` (workdir first, then the bundled
    defaults), expanded to a plain role list plus raw per-role policy
    dicts, and merged under any seed-level ``role_model_policy`` (seed
    keys win per role key). The merged mapping then flows through the
    standard ``_parse_role_model_policy`` validator, so a manifest-driven
    seed parses to byte-identical structures as the equivalent
    hand-written one.

    Args:
        raw_ref: The ``team_manifest`` YAML value.
        raw_team: The ``team`` YAML value, for the mutual-exclusion check.
        raw_role_policy: The ``role_model_policy`` YAML value to merge over
            the expansion.
        workdir: The seed file's directory; manifest resolution root.

    Returns:
        ``(team, merged_role_policy, manifest_name, manifest_digest)``
        where ``merged_role_policy`` is ``None`` when neither the manifest
        nor the seed declares any policy.

    Raises:
        SeedError: On a malformed reference or a ``team``/``team_manifest``
            conflict.
        TeamManifestNotFoundError: When the manifest does not exist.
        TeamManifestDigestMismatchError: When an ``@sha256`` pin does not
            match the resolved manifest (AC4). Both subclass ``SeedError``.
    """
    if not isinstance(raw_ref, str) or not raw_ref.strip():
        raise SeedError(
            f"team_manifest must be a non-empty string of the form '<name>' or '<name>@<sha256>', got: {raw_ref!r}"
        )
    if isinstance(raw_team, list) and raw_team:
        raise SeedError("team and team_manifest are mutually exclusive; remove one of them")

    # Imported lazily so parsing a seed without a manifest reference does
    # not pay for the teams package (mirrors the response_style import).
    from bernstein.core.teams.manifest import (
        TeamManifestDigestMismatchError,
        expand_manifest,
        parse_manifest_ref,
        resolve_team_manifest,
    )

    name, pinned = parse_manifest_ref(raw_ref)
    manifest = resolve_team_manifest(name, workdir=workdir)
    digest = manifest.digest()
    if pinned is not None and pinned != digest:
        raise TeamManifestDigestMismatchError(
            f"team_manifest {name!r} digest mismatch: pinned {pinned}, resolved {digest}. "
            "Update the pin to the resolved digest or restore the manifest it was created from."
        )

    expanded = expand_manifest(manifest)
    merged: dict[str, object] = {role: dict(policy) for role, policy in expanded.role_model_policy.items()}
    if raw_role_policy is not None:
        if not isinstance(raw_role_policy, dict):
            raise SeedError("role_model_policy must be a mapping of role -> settings")
        for role, settings in cast("_StrObjDict", raw_role_policy).items():
            base = merged.get(role)
            if isinstance(base, dict) and isinstance(settings, dict):
                merged[role] = {**cast("_StrObjDict", base), **cast("_StrObjDict", settings)}
            else:
                merged[role] = settings

    return list(expanded.team), (merged or None), name, digest


def _parse_string_list(raw: object, field_name: str) -> tuple[str, ...]:
    """Parse an optional list-of-strings field from YAML.

    Args:
        raw: Value from YAML - should be None or a list of strings.
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


def _parse_metric_entry(name: str, entry: object) -> MetricSchema:
    """Parse a single metric entry from the ``metrics`` section.

    Args:
        name: Metric name (used in error messages).
        entry: Raw YAML value for the metric.

    Returns:
        Parsed ``MetricSchema``.

    Raises:
        SeedError: If the entry is invalid.
    """
    if not isinstance(entry, dict):
        raise SeedError(f"metrics.{name} must be a mapping, got: {type(entry).__name__}")
    entry_dict: dict[str, object] = cast("dict[str, object]", entry)

    formula = entry_dict.get("formula")
    if not isinstance(formula, str) or not formula.strip():
        raise SeedError(f"metrics.{name}.formula must be a non-empty string")

    unit_raw = entry_dict.get("unit", "")
    if not isinstance(unit_raw, str):
        raise SeedError(f"metrics.{name}.unit must be a string, got: {type(unit_raw).__name__}")

    description_raw = entry_dict.get("description", "")
    if not isinstance(description_raw, str):
        raise SeedError(f"metrics.{name}.description must be a string, got: {type(description_raw).__name__}")

    def _parse_alert_threshold(field: str) -> float | None:
        raw_val = entry_dict.get(field)
        if raw_val is None:
            return None
        if not isinstance(raw_val, (int, float)):
            raise SeedError(f"metrics.{name}.{field} must be a number, got: {type(raw_val).__name__}")
        return float(raw_val)

    return MetricSchema(
        formula=formula.strip(),
        unit=unit_raw,
        description=description_raw,
        alert_above=_parse_alert_threshold("alert_above"),
        alert_below=_parse_alert_threshold("alert_below"),
    )


def _parse_metrics(raw: object) -> dict[str, MetricSchema]:
    """Parse the optional ``metrics`` section from ``bernstein.yaml``.

    Each key is a metric name; each value is a mapping with a required
    ``formula`` field and optional ``unit``, ``description``,
    ``alert_above``, and ``alert_below``.

    Example YAML::

        metrics:
          code_per_dollar:
            formula: "lines_changed / total_cost"
            unit: "lines/$"
            description: "Code produced per dollar spent"

    Args:
        raw: Raw YAML value for the ``metrics`` section.

    Returns:
        Dict mapping metric name to a parsed ``MetricSchema``.

    Raises:
        SeedError: If the section is not a mapping or any entry is invalid.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise SeedError(f"metrics must be a mapping, got: {type(raw).__name__}")

    result: dict[str, MetricSchema] = {}
    metrics_dict: dict[str, object] = cast("_StrObjDict", raw)
    for name, entry in metrics_dict.items():
        if not isinstance(name, str) or not name.strip():
            raise SeedError(f"metrics keys must be non-empty strings, got: {name!r}")
        result[name] = _parse_metric_entry(name, entry)

    return result


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


# Accepted glob origin shape - scheme and host are literal, only
# the port may be a ``*`` wildcard (e.g. ``http://localhost:*``).  Anything
# outside this shape is rejected with a clear error because
# ``starlette.middleware.cors.CORSMiddleware`` compares ``allow_origins``
# literally and would silently break the origin check.
_CORS_PORT_GLOB_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+\-.]*://[^/\s*]+:\*$")


def _validate_cors_origin(origin: str) -> None:
    """Reject CORS origin strings CORSMiddleware cannot match literally.

    A bare ``*`` is allowed (starlette treats it specially).  A glob that
    matches ``scheme://host:*`` is allowed - ``server_app._split_cors_origins``
    will translate it into an ``allow_origin_regex`` argument.  Any other
    use of ``*`` is rejected because CORSMiddleware would compare it
    literally and silently drop the origin header.

    Args:
        origin: One origin entry from ``cors.allowed_origins``.

    Raises:
        SeedError: When the origin contains an unsupported glob pattern.
    """
    if "*" not in origin or origin == "*":
        return
    if _CORS_PORT_GLOB_RE.match(origin):
        return
    raise SeedError(
        f"cors.allowed_origins entry {origin!r} contains an unsupported "
        f"wildcard; starlette CORSMiddleware matches allow_origins "
        f"literally. Use the port-glob form 'scheme://host:*' (e.g. "
        f"'http://localhost:*') or remove the '*' and rely on the "
        f"allow_origin_regex translation."
    )


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

    cors_dict: dict[str, object] = cast("_StrObjDict", raw)

    origins = _parse_string_list(cors_dict.get("allowed_origins"), "cors.allowed_origins")
    if not origins:
        origins = CORSConfig.allowed_origins
    for origin in origins:
        _validate_cors_origin(origin)

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

    da_dict: dict[str, object] = cast("_StrObjDict", raw)

    password_raw = da_dict.get("password", "")
    if not isinstance(password_raw, str):
        raise SeedError(f"dashboard_auth.password must be a string, got: {type(password_raw).__name__}")
    # Support env var references
    password = str(_expand_env_value(password_raw, "dashboard_auth.password"))

    timeout_raw = da_dict.get("session_timeout_seconds", 3600)
    if not isinstance(timeout_raw, int) or timeout_raw < 0:
        raise SeedError(f"dashboard_auth.session_timeout_seconds must be a non-negative integer, got: {timeout_raw!r}")

    return DashboardAuthConfig(password=password, session_timeout_seconds=timeout_raw)


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
        entry = cast("_StrObjDict", item)
        tenant_id_raw = entry.get("id")
        if not isinstance(tenant_id_raw, str) or not tenant_id_raw.strip():
            raise SeedError(f"tenants[{index}].id must be a non-empty string")
        tenant_id = tenant_id_raw.strip()
        if tenant_id in seen:
            raise SeedError(f"Duplicate tenant id: {tenant_id!r}")
        seen.add(tenant_id)
        budget_usd = _parse_budget(cast(_CAST_STR_INT_FLOAT_NONE, entry.get("budget")))
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


def _require_bool(data: dict[str, object], key: str, default: bool, prefix: str) -> bool:
    """Extract and validate a boolean field from a seed mapping."""
    raw = data.get(key, default)
    if not isinstance(raw, bool):
        raise SeedError(f"{prefix}.{key} must be a bool, got: {type(raw).__name__}")
    return raw


def _require_str(data: dict[str, object], key: str, default: str, prefix: str) -> str:
    """Extract and validate a string field from a seed mapping."""
    raw = data.get(key, default)
    if not isinstance(raw, str):
        raise SeedError(f"{prefix}.{key} must be a string, got: {type(raw).__name__}")
    return raw.strip()


def _require_positive_number(data: dict[str, object], key: str, default: float, prefix: str) -> float:
    """Extract and validate a positive numeric field from a seed mapping."""
    raw = data.get(key, default)
    if not isinstance(raw, (int, float)) or raw <= 0:
        raise SeedError(f"{prefix}.{key} must be a positive number")
    return float(raw)


def _require_positive_int(data: dict[str, object], key: str, default: int, prefix: str) -> int:
    """Extract and validate a positive integer field from a seed mapping."""
    raw = data.get(key, default)
    if not isinstance(raw, int) or raw < 1:
        raise SeedError(f"{prefix}.{key} must be a positive integer")
    return raw


def _validate_openclaw_enabled(url_text: str, api_key: str, agent_id: str) -> None:
    """Validate fields required when the OpenClaw bridge is enabled."""
    if not url_text:
        raise SeedError("bridges.openclaw.url is required when the bridge is enabled")
    parsed_url = urlparse(url_text)
    if parsed_url.scheme not in {"ws", "wss"} or not parsed_url.netloc:
        raise SeedError("bridges.openclaw.url must be a valid ws:// or wss:// URL")
    if not api_key:
        raise SeedError("bridges.openclaw.api_key is required when the bridge is enabled")
    if not agent_id:
        raise SeedError("bridges.openclaw.agent_id is required when the bridge is enabled")


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

    _P = "bridges.openclaw"
    data = cast("_StrObjDict", raw)
    enabled_raw = _require_bool(data, "enabled", False, _P)

    url_raw = data.get("url", data.get("endpoint", ""))
    url_value = _expand_env_value(url_raw, f"{_P}.url")
    if not isinstance(url_value, str):
        raise SeedError(f"{_P}.url must be a string, got: {type(url_value).__name__}")
    url_text = url_value.strip()

    api_key_raw = _expand_env_value(data.get("api_key", ""), f"{_P}.api_key")
    if not isinstance(api_key_raw, str):
        raise SeedError(f"{_P}.api_key must be a string, got: {type(api_key_raw).__name__}")
    api_key = api_key_raw.strip()

    agent_id = _require_str(data, "agent_id", "", _P)

    workspace_mode_raw = data.get("workspace_mode", "shared_workspace")
    if workspace_mode_raw != "shared_workspace":
        raise SeedError(f"{_P}.workspace_mode must be 'shared_workspace'")

    fallback_raw = _require_bool(data, "fallback_to_local", True, _P)
    connect_timeout_raw = _require_positive_number(data, "connect_timeout_s", 10.0, _P)
    request_timeout_raw = _require_positive_number(data, "request_timeout_s", 30.0, _P)

    session_prefix_raw = data.get("session_prefix", "bernstein-")
    if not isinstance(session_prefix_raw, str) or not session_prefix_raw.strip():
        raise SeedError(f"{_P}.session_prefix must be a non-empty string")

    max_log_bytes_raw = _require_positive_int(data, "max_log_bytes", 1_048_576, _P)

    model_override_raw = data.get("model_override")
    if model_override_raw is not None and (not isinstance(model_override_raw, str) or not model_override_raw.strip()):
        raise SeedError(f"{_P}.model_override must be a non-empty string when set")

    if enabled_raw:
        _validate_openclaw_enabled(url_text, api_key, agent_id)

    return OpenClawBridgeConfig(
        enabled=enabled_raw,
        url=url_text,
        api_key=api_key,
        agent_id=agent_id,
        workspace_mode="shared_workspace",
        fallback_to_local=fallback_raw,
        connect_timeout_s=connect_timeout_raw,
        request_timeout_s=request_timeout_raw,
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
    data = cast("_StrObjDict", raw)
    return BridgeConfigSet(openclaw=_parse_openclaw_runtime_config(data.get("openclaw")))


# Keys accepted on a ``local_endpoints.<name>`` profile. Mirrors
# ``LocalEndpointProfileSchema`` (config_schema.py) which is ``extra="forbid"``,
# so the seed parser and the pydantic schema reject the same malformed
# profiles. ``base_url``/``model`` are required; the rest are optional.
_LOCAL_ENDPOINT_KEYS: tuple[str, ...] = ("base_url", "model", "api_key_env", "engine", "timeout")


def _parse_local_endpoints(raw: object) -> dict[str, dict[str, str]] | None:
    """Parse the optional ``local_endpoints`` section (issue #2356).

    Named OpenAI-compatible endpoint profiles referenced from
    ``role_model_policy.<role>.endpoint``. This mirrors
    :class:`bernstein.core.config.config_schema.LocalEndpointProfileSchema`
    so a seed file parses via ``parse_seed`` (the runtime spawn path)
    exactly as it validates via ``load_and_validate``.

    Returns a mapping of profile name -> resolved endpoint fields
    (``base_url`` and ``model`` always present; ``api_key_env`` only when
    the profile declares one). ``engine``/``timeout`` are validated for
    shape but not carried onto role entries - they are provenance/runtime
    metadata the seed's role-policy consumers do not read.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SeedError("local_endpoints must be a mapping of profile-name -> settings")

    profiles: dict[str, dict[str, str]] = {}
    for name, settings in raw.items():
        if not isinstance(name, str) or not name:
            raise SeedError("local_endpoints keys must be non-empty profile-name strings")
        if not isinstance(settings, dict):
            raise SeedError(f"local_endpoints[{name!r}] must be a mapping")

        resolved: dict[str, str] = {}
        for key in ("base_url", "model"):
            value = settings.get(key)
            if not isinstance(value, str) or not value:
                raise SeedError(f"local_endpoints[{name!r}][{key!r}] must be a non-empty string")
            resolved[key] = value

        api_key_env = settings.get("api_key_env")
        if api_key_env is not None:
            if not isinstance(api_key_env, str) or not api_key_env:
                raise SeedError(f"local_endpoints[{name!r}]['api_key_env'] must be a non-empty string")
            # Reuse the adapter's fail-closed credential-name allowlist so an
            # unrelated host secret cannot be forwarded to an endpoint by
            # hiding it in a profile instead of an inline role entry.
            from bernstein.adapters.openai_agents_runner import validate_api_key_env_name

            try:
                validate_api_key_env_name(api_key_env)
            except RuntimeError as exc:
                raise SeedError(f"local_endpoints[{name!r}][api_key_env]: {exc}") from exc
            resolved["api_key_env"] = api_key_env

        engine = settings.get("engine")
        if engine is not None and (not isinstance(engine, str) or not engine):
            raise SeedError(f"local_endpoints[{name!r}]['engine'] must be a non-empty string")

        timeout = settings.get("timeout")
        if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or timeout <= 0):
            raise SeedError(f"local_endpoints[{name!r}]['timeout'] must be a positive number")

        unknown_keys = sorted(set(settings) - set(_LOCAL_ENDPOINT_KEYS))
        if unknown_keys:
            raise SeedError(f"local_endpoints[{name!r}] has unknown keys: {', '.join(unknown_keys)}")

        profiles[name] = resolved
    return profiles


def _parse_role_model_policy(
    raw: object,
    *,
    local_endpoints: dict[str, dict[str, str]] | None = None,
) -> dict[str, dict[str, str | int | dict[str, object]]] | None:
    """Parse optional role-specific provider/model overrides.

    ``local_endpoints`` is the parsed ``local_endpoints`` section (profile
    name -> resolved endpoint fields). It is threaded through so a role's
    ``endpoint`` reference can be validated against the declared profiles
    and resolved onto the entry - see :func:`_parse_single_role_policy`.
    """
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SeedError("role_model_policy must be a mapping of role -> settings")

    parsed: dict[str, dict[str, str | int | dict[str, object]]] = {}
    for role, settings in raw.items():
        if not isinstance(role, str) or not role:
            raise SeedError("role_model_policy keys must be non-empty role strings")
        parsed[role] = _parse_single_role_policy(role, settings, local_endpoints=local_endpoints)
    return parsed


_ROLE_POLICY_KEYS: tuple[str, ...] = (
    "provider",
    "model",
    "effort",
    "cli",
    "base_url",
    "api_key_env",
)

# Integer-typed role policy keys, validated and stored separately from the
# string-typed keys above. ``max_tokens`` is a per-role sampling override
# (see ``RoleModelPolicyEntry.max_tokens`` in config_schema.py:179) that must
# flow through the seed parser unchanged so an operator can cap completion
# length for a role - without this branch a seed file setting
# ``role_model_policy.<role>.max_tokens`` was rejected at parse time with
# "unknown keys: max_tokens" (parser/schema divergence fixed here).
_ROLE_POLICY_INT_KEYS: tuple[str, ...] = ("max_tokens",)

# ``response_style`` declares the per-role response-style profile
# (``verbose``/``balanced``/``terse``) applied at spawn time (see
# ``bernstein.core.agents.response_style``). The value is validated against
# the closed style vocabulary in ``_parse_single_role_policy``; the mapped
# mode-profile template file is validated for existence in ``parse_seed``
# so a dangling template reference fails at config-validation time with a
# typed ``ResponseStyleTemplateError``, not at spawn time.
_ROLE_POLICY_STYLE_KEY = "response_style"

# ``council`` is parsed and validated separately (its value is a nested
# mapping, not a scalar string/int like every other role-policy key), so it
# is carved out of the unknown-keys check in ``_parse_single_role_policy``
# rather than added to ``_ROLE_POLICY_KEYS``.
_ROLE_POLICY_COUNCIL_KEY = "council"

# ``endpoint`` names a ``local_endpoints`` profile this role runs on (issue
# #2356). The referenced profile's ``base_url``/``model``/``api_key_env`` are
# materialized onto the entry at parse time, so downstream consumers see one
# resolved shape - matching the pydantic schema's
# ``BernsteinConfig._resolve_local_endpoint_references``. Without this branch a
# seed file setting ``role_model_policy.<role>.endpoint`` was rejected at parse
# time with "unknown keys: endpoint" (parser/schema divergence fixed here),
# even though the schema path (``load_and_validate``) accepted the same file.
_ROLE_POLICY_ENDPOINT_KEY = "endpoint"

# Endpoint fields that the ``endpoint`` profile reference pins; setting any of
# them inline alongside ``endpoint`` is a conflict (the profile is the single
# source of truth for the certified endpoint).
_ROLE_POLICY_ENDPOINT_PINNED_KEYS: tuple[str, ...] = ("base_url", "model", "api_key_env")

_COUNCIL_CANDIDATE_KEYS: tuple[str, ...] = ("model", "base_url", "api_key_env")


def _parse_council_candidate(role: str, member: str, raw: object) -> dict[str, str]:
    """Parse one ``candidates[]`` entry or the ``judge`` entry of a council block.

    ``member`` is a human-readable label (``"candidates[0]"`` or
    ``"judge"``) used only for error messages. ``model`` is required;
    ``base_url``/``api_key_env`` are optional and follow the same
    fail-closed ``api_key_env`` credential-allowlist validation as the
    top-level role policy fields of the same name.
    """
    if not isinstance(raw, dict):
        raise SeedError(f"role_model_policy[{role!r}].council.{member} must be a mapping")

    model = raw.get("model")
    if not isinstance(model, str) or not model:
        raise SeedError(f"role_model_policy[{role!r}].council.{member}.model must be a non-empty string")
    parsed: dict[str, str] = {"model": model}

    for key in ("base_url", "api_key_env"):
        value = raw.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or not value:
            raise SeedError(f"role_model_policy[{role!r}].council.{member}.{key} must be a non-empty string")
        parsed[key] = value

    if "api_key_env" in parsed:
        from bernstein.adapters.openai_agents_runner import validate_api_key_env_name

        try:
            validate_api_key_env_name(parsed["api_key_env"])
        except RuntimeError as exc:
            raise SeedError(f"role_model_policy[{role!r}].council.{member}.api_key_env: {exc}") from exc

    unknown_keys = sorted(set(raw) - set(_COUNCIL_CANDIDATE_KEYS))
    if unknown_keys:
        raise SeedError(f"role_model_policy[{role!r}].council.{member} has unknown keys: {', '.join(unknown_keys)}")
    return parsed


def _parse_council(role: str, raw: object) -> dict[str, object]:
    """Parse the optional ``council:`` block of a role's model policy.

    Shape (mirrors :class:`bernstein.core.config.config_schema.CouncilConfig`)::

        council:
          candidates:
            - model: gpt-5-mini
            - model: deepseek/deepseek-v4-flash
              base_url: https://openrouter.ai/api/v1
              api_key_env: OPENROUTER_API_KEY
          judge:
            model: gpt-5
          timeout: 60.0

    ``candidates`` must be a non-empty list; ``judge`` is required.
    ``timeout`` is optional and defaults to 60.0 seconds (matching
    ``CouncilConfig.timeout``'s Pydantic default) when absent.
    """
    if not isinstance(raw, dict):
        raise SeedError(f"role_model_policy[{role!r}].council must be a mapping")

    raw_candidates = raw.get("candidates")
    if not isinstance(raw_candidates, list) or not raw_candidates:
        raise SeedError(f"role_model_policy[{role!r}].council.candidates must be a non-empty list")
    candidates = [_parse_council_candidate(role, f"candidates[{i}]", entry) for i, entry in enumerate(raw_candidates)]

    raw_judge = raw.get("judge")
    if raw_judge is None:
        raise SeedError(f"role_model_policy[{role!r}].council.judge is required")
    judge = _parse_council_candidate(role, "judge", raw_judge)

    parsed: dict[str, object] = {"candidates": candidates, "judge": judge}

    raw_timeout = raw.get("timeout")
    if raw_timeout is not None:
        if isinstance(raw_timeout, bool) or not isinstance(raw_timeout, (int, float)) or raw_timeout <= 0:
            raise SeedError(f"role_model_policy[{role!r}].council.timeout must be a positive number")
        parsed["timeout"] = float(raw_timeout)

    unknown_keys = sorted(set(raw) - {"candidates", "judge", "timeout"})
    if unknown_keys:
        raise SeedError(f"role_model_policy[{role!r}].council has unknown keys: {', '.join(unknown_keys)}")
    return parsed


def _parse_single_role_policy(
    role: str,
    settings: object,
    *,
    local_endpoints: dict[str, dict[str, str]] | None = None,
) -> dict[str, str | int | dict[str, object]]:
    """Parse and validate a single role's model policy settings.

    ``endpoint`` names a ``local_endpoints`` profile (see
    :func:`_parse_local_endpoints`) this role runs on. It must reference a
    declared profile and is mutually exclusive with an inline
    ``base_url``/``model``/``api_key_env`` (the profile pins the certified
    endpoint). On success the profile's endpoint fields are materialized
    onto the returned entry, so downstream consumers see the same resolved
    shape the pydantic schema produces via
    :meth:`bernstein.core.config.config_schema.BernsteinConfig._resolve_local_endpoint_references`.

    ``base_url`` and ``api_key_env`` are optional per-role endpoint
    overrides that flow through the spawn path into the adapter manifest
    the same way ``model``/``provider`` do. ``api_key_env`` is the NAME of
    an environment variable, never a literal key, and is validated against
    the same fail-closed credential allowlist the ``openai_agents`` runner
    enforces so a repo-carried config cannot forward an unrelated host
    secret to an arbitrary endpoint.

    ``max_tokens`` is an optional per-role integer cap on completion length
    (see :data:`_ROLE_POLICY_INT_KEYS`); it is validated as a positive int
    here and flows unchanged into
    :meth:`bernstein.core.agents.spawner_core.AgentSpawner._apply_sampling_overrides`.

    ``council`` is an optional "council of agents" fan-out/judge block (see
    :func:`_parse_council` and
    :class:`bernstein.core.config.config_schema.CouncilConfig`). When
    present, it is parsed into a nested dict and carried under the
    ``"council"`` key alongside the scalar fields above - the adapter/spawn
    path is responsible for using it to drive a TASK-LEVEL council run
    (``bernstein.adapters.council_runner.run_council``) instead of a single
    model for this role.

    A separate, simpler convention also produces a council run: setting
    ``model`` itself to a ``.yaml``/``.yml`` path (e.g.
    ``"councils/planning.yaml"``) instead of a real model id. That path is
    stored here completely unresolved/unvalidated - deliberately, so a seed
    file with a council-file reference for a role parses successfully even
    when the file's contents are only meaningful at run time, in the
    worktree, relative to ``.bernstein/``. The runner
    (``openai_agents_runner._load_council_config``) is what actually
    resolves and loads it, at spawn/run time, not here.
    """
    if not isinstance(settings, dict):
        raise SeedError(f"role_model_policy[{role!r}] must be a mapping")

    normalized: dict[str, str | int | dict[str, object]] = {}
    for key in _ROLE_POLICY_KEYS:
        value = settings.get(key)
        if value is None:
            continue
        if not isinstance(value, str) or not value:
            raise SeedError(f"role_model_policy[{role!r}][{key!r}] must be a non-empty string")
        normalized[key] = value

    for key in _ROLE_POLICY_INT_KEYS:
        value = settings.get(key)
        if value is None:
            continue
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise SeedError(f"role_model_policy[{role!r}][{key!r}] must be a positive integer")
        normalized[key] = value

    # Reuse the adapter's fail-closed credential-name allowlist. Imported
    # lazily so parsing a seed file does not import the adapters package
    # (and its optional SDK path) at module load. A rejected name surfaces
    # as a SeedError so the misconfig fails at parse time, not at spawn.
    if "api_key_env" in normalized:
        from bernstein.adapters.openai_agents_runner import validate_api_key_env_name

        try:
            validate_api_key_env_name(str(normalized["api_key_env"]))
        except RuntimeError as exc:
            raise SeedError(f"role_model_policy[{role!r}][api_key_env]: {exc}") from exc

    if "cli" in normalized and "provider" not in normalized:
        normalized["provider"] = normalized["cli"]

    raw_style = settings.get(_ROLE_POLICY_STYLE_KEY)
    if raw_style is not None:
        from bernstein.core.agents.response_style import RESPONSE_STYLES

        if not isinstance(raw_style, str) or raw_style not in RESPONSE_STYLES:
            allowed = ", ".join(RESPONSE_STYLES)
            raise SeedError(
                f"role_model_policy[{role!r}][{_ROLE_POLICY_STYLE_KEY!r}] must be one of: {allowed} (got {raw_style!r})"
            )
        normalized[_ROLE_POLICY_STYLE_KEY] = raw_style

    raw_council = settings.get(_ROLE_POLICY_COUNCIL_KEY)
    if raw_council is not None:
        normalized[_ROLE_POLICY_COUNCIL_KEY] = _parse_council(role, raw_council)

    raw_endpoint = settings.get(_ROLE_POLICY_ENDPOINT_KEY)
    if raw_endpoint is not None:
        if not isinstance(raw_endpoint, str) or not raw_endpoint:
            raise SeedError(f"role_model_policy[{role!r}][{_ROLE_POLICY_ENDPOINT_KEY!r}] must be a non-empty string")
        profiles = local_endpoints or {}
        profile = profiles.get(raw_endpoint)
        if profile is None:
            known = ", ".join(sorted(profiles)) or "(none defined)"
            raise SeedError(
                f"role_model_policy[{role!r}].endpoint references unknown local_endpoints "
                f"profile {raw_endpoint!r}. Known profiles: {known}."
            )
        # The profile is the single source of truth for the endpoint: the
        # certification receipt is keyed on its exact (base_url, model) pair,
        # so an inline base_url/model/api_key_env alongside it is a conflict.
        conflicts = sorted(key for key in _ROLE_POLICY_ENDPOINT_PINNED_KEYS if key in normalized)
        if conflicts:
            raise SeedError(
                f"role_model_policy[{role!r}]: {', '.join(conflicts)} cannot be set inline together "
                f"with endpoint={raw_endpoint!r}; the profile pins the certified endpoint. "
                "Move the overrides into the local_endpoints profile."
            )
        normalized[_ROLE_POLICY_ENDPOINT_KEY] = raw_endpoint
        for key, value in profile.items():
            normalized[key] = value

    allowed_keys = (
        set(_ROLE_POLICY_KEYS)
        | set(_ROLE_POLICY_INT_KEYS)
        | {_ROLE_POLICY_COUNCIL_KEY, _ROLE_POLICY_STYLE_KEY, _ROLE_POLICY_ENDPOINT_KEY}
    )
    unknown_keys = sorted(set(settings) - allowed_keys)
    if unknown_keys:
        raise SeedError(f"role_model_policy[{role!r}] has unknown keys: {', '.join(unknown_keys)}")
    return normalized


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

    data = cast("_StrObjDict", raw)
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
    mf: dict[str, object] = cast("_StrObjDict", raw)

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


def _parse_provider_availability(raw: object) -> dict[str, Any] | None:
    """Parse and validate the optional provider_availability section (#2355).

    The section declares per-role provider fallback chains with conformance
    floors. Validation happens here so a chain element below its role's
    floor fails at config load, never at first dispatch. The raw mapping is
    returned unchanged (the spawner re-parses it into typed policies).

    Args:
        raw: Raw YAML value for the ``provider_availability`` section.

    Returns:
        The validated raw mapping, or None when the section is absent.

    Raises:
        SeedError: If the section is malformed or a fallback element sits
            below its role's conformance floor.
    """
    if raw is None:
        return None
    from bernstein.core.routing.provider_availability import (
        AvailabilityPolicyError,
        parse_provider_availability,
    )

    if not isinstance(raw, dict):
        raise SeedError(f"provider_availability must be a mapping, got: {type(raw).__name__}")
    section = cast("dict[str, Any]", raw)
    try:
        parse_provider_availability(section)
    except AvailabilityPolicyError as exc:
        raise SeedError(str(exc)) from exc
    return section


def _parse_tuning(raw: dict[str, object]) -> None:
    """Apply tuning overrides from bernstein.yaml to defaults."""
    from bernstein.core.defaults import override

    tuning = raw.get("tuning", {})
    if not isinstance(tuning, dict):
        return

    for section_name, section_overrides in tuning.items():
        if not isinstance(section_overrides, dict):
            continue
        try:
            override(section_name, section_overrides)
        except (KeyError, AttributeError) as exc:
            logger.warning("tuning.%s: %s", section_name, exc)


def _parse_notify(raw: object) -> NotifyConfig | None:
    """Parse the optional ``notify`` section."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SeedError(f"notify must be a mapping, got: {type(raw).__name__}")
    notify_dict: dict[str, object] = cast("_StrObjDict", raw)
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
    return NotifyConfig(
        webhook_url=webhook_url,
        on_complete=on_complete,
        on_failure=on_failure,
        desktop=desktop,
    )


def _parse_webhooks(raw: object) -> tuple[WebhookConfig, ...]:
    """Parse the optional ``webhooks`` section."""
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise SeedError(f"webhooks must be a list, got: {type(raw).__name__}")
    parsed_targets: list[WebhookConfig] = []
    for idx, item in enumerate(raw):
        if not isinstance(item, dict):
            raise SeedError(f"webhooks[{idx}] must be a mapping")
        entry = cast("_StrObjDict", item)
        url_raw: object = entry.get("url")
        if not isinstance(url_raw, str) or not url_raw.strip():
            raise SeedError(f"webhooks[{idx}].url must be a non-empty string")
        events_raw: object = entry.get("events")
        events = _parse_string_list(events_raw, f"webhooks[{idx}].events")
        if not events:
            raise SeedError(f"webhooks[{idx}].events must contain at least one event")
        normalized_events = tuple(
            _normalize_webhook_event(event_name, f"webhooks[{idx}].events") for event_name in events
        )
        parsed_targets.append(WebhookConfig(url=url_raw.strip(), events=normalized_events))
    return tuple(parsed_targets)


def _parse_storage(raw: object) -> StorageConfig | None:
    """Parse the optional ``storage`` section."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SeedError(f"storage must be a mapping, got: {type(raw).__name__}")
    storage_dict: dict[str, object] = cast("_StrObjDict", raw)
    storage_backend_raw: object = storage_dict.get("backend", "memory")
    _valid_storage_backends = ("memory", "postgres", "redis")
    if storage_backend_raw not in _valid_storage_backends:
        raise SeedError(f"storage.backend must be one of {list(_valid_storage_backends)}, got: {storage_backend_raw!r}")
    storage_backend: Literal["memory", "postgres", "redis"] = storage_backend_raw  # narrowed by membership check
    storage_db_url_raw: object = storage_dict.get("database_url")
    storage_db_url: str | None = str(storage_db_url_raw) if storage_db_url_raw is not None else None
    storage_redis_url_raw: object = storage_dict.get("redis_url")
    storage_redis_url: str | None = str(storage_redis_url_raw) if storage_redis_url_raw is not None else None
    return StorageConfig(
        backend=storage_backend,
        database_url=storage_db_url,
        redis_url=storage_redis_url,
    )


def _parse_cluster(raw: object) -> ClusterConfig | None:
    """Parse the optional ``cluster`` section."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SeedError(f"cluster must be a mapping, got: {type(raw).__name__}")
    cluster_dict: dict[str, object] = cast("_StrObjDict", raw)
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
    return ClusterConfig(
        enabled=bool(cluster_dict.get("enabled", False)),
        topology=topology,
        auth_token=auth_token,
        node_heartbeat_interval_s=cast("int", cluster_dict.get("node_heartbeat_interval_s", 15)),
        node_timeout_s=cast("int", cluster_dict.get("node_timeout_s", 60)),
        server_url=server_url,
        bind_host=str(cluster_dict.get("bind_host", "127.0.0.1")),
    )


def _parse_session(raw: object) -> SessionConfig:
    """Parse the optional ``session`` section."""
    if raw is None:
        return SessionConfig()
    if not isinstance(raw, dict):
        raise SeedError(f"session must be a mapping, got: {type(raw).__name__}")
    session_dict: dict[str, object] = cast("_StrObjDict", raw)
    resume_raw: object = session_dict.get("resume", True)
    if not isinstance(resume_raw, bool):
        raise SeedError(f"session.resume must be a bool, got: {type(resume_raw).__name__}")
    stale_raw: object = session_dict.get("stale_after_minutes", 30)
    if not isinstance(stale_raw, int) or stale_raw < 1:
        raise SeedError(f"session.stale_after_minutes must be a positive integer, got: {stale_raw!r}")
    return SessionConfig(resume=resume_raw, stale_after_minutes=stale_raw)


def _parse_github(raw: object) -> GithubConfig:
    """Parse the optional ``github`` section.

    Only ``sync_backlog`` is recognised today. Auto-sync of open issues into
    the backlog is opt-in (default ``False``) because it can silently displace
    a seeded goal on a non-empty backlog.
    """
    if raw is None:
        return GithubConfig()
    if not isinstance(raw, dict):
        raise SeedError(f"github must be a mapping, got: {type(raw).__name__}")
    sync_raw: object = cast("_StrObjDict", raw).get("sync_backlog", False)
    if not isinstance(sync_raw, bool):
        raise SeedError(f"github.sync_backlog must be a bool, got: {type(sync_raw).__name__}")
    return GithubConfig(sync_backlog=sync_raw)


def _parse_workspace(
    workspace_raw: object,
    repos_raw: object,
    root: Path,
) -> Workspace | None:
    """Parse the optional ``workspace`` or ``repos`` section."""
    if workspace_raw is not None:
        if not isinstance(workspace_raw, dict):
            raise SeedError(f"workspace must be a mapping, got: {type(workspace_raw).__name__}")
        workspace_dict: dict[str, Any] = cast(_CAST_DICT_STR_ANY, workspace_raw)
        try:
            return Workspace.from_config(workspace_dict, root=root)
        except ValueError as exc:
            raise SeedError(f"Invalid workspace configuration: {exc}") from exc
    if repos_raw is not None:
        if not isinstance(repos_raw, list):
            raise SeedError(f"repos must be a list, got: {type(repos_raw).__name__}")
        try:
            return Workspace.from_config({"repos": repos_raw}, root=root)
        except ValueError as exc:
            raise SeedError(f"Invalid repos configuration: {exc}") from exc
    return None


def _parse_worktree_setup(raw: object) -> WorktreeSetupConfig | None:
    """Parse the optional ``worktree_setup`` section."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SeedError(f"worktree_setup must be a mapping, got: {type(raw).__name__}")
    ws_dict: dict[str, object] = cast("_StrObjDict", raw)
    symlink_dirs = _parse_string_list(ws_dict.get("symlink_dirs"), "worktree_setup.symlink_dirs")
    copy_files = _parse_string_list(ws_dict.get("copy_files"), "worktree_setup.copy_files")
    setup_cmd_raw: object = ws_dict.get("setup_command")
    if setup_cmd_raw is not None and not isinstance(setup_cmd_raw, str):
        raise SeedError(f"worktree_setup.setup_command must be a string, got: {type(setup_cmd_raw).__name__}")
    return WorktreeSetupConfig(
        symlink_dirs=symlink_dirs,
        copy_files=copy_files,
        setup_command=setup_cmd_raw if isinstance(setup_cmd_raw, str) else None,
    )


def _parse_batch(raw: object) -> BatchConfig:
    """Parse the optional ``batch`` section."""
    if raw is None:
        return BatchConfig()
    if not isinstance(raw, dict):
        raise SeedError(f"batch must be a mapping, got: {type(raw).__name__}")
    batch_dict: dict[str, object] = cast("_StrObjDict", raw)
    enabled_raw: object = batch_dict.get("enabled", False)
    if not isinstance(enabled_raw, bool):
        raise SeedError(f"batch.enabled must be a bool, got: {type(enabled_raw).__name__}")
    eligible = list(_parse_string_list(batch_dict.get("eligible"), "batch.eligible"))
    return BatchConfig(enabled=enabled_raw, eligible=eligible)


def _parse_test_agent(raw: object) -> TestAgentConfig:
    """Parse the optional ``test_agent`` section."""
    if raw is None:
        return TestAgentConfig()
    if not isinstance(raw, dict):
        raise SeedError(f"test_agent must be a mapping, got: {type(raw).__name__}")
    test_agent_dict: dict[str, object] = cast("_StrObjDict", raw)
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
    return TestAgentConfig(
        always_spawn=always_spawn_raw,
        model=model_value_raw.strip(),
        trigger="on_task_complete",
    )


def _parse_secrets(raw: object) -> SecretsConfig | None:
    """Parse the optional ``secrets`` section."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SeedError(f"secrets must be a mapping, got: {type(raw).__name__}")
    secrets_dict: dict[str, object] = cast("_StrObjDict", raw)
    secrets_provider_raw: object = secrets_dict.get("provider")
    if not isinstance(secrets_provider_raw, str):
        raise SeedError("secrets.provider is required and must be a string")
    from bernstein.core.secrets import _VALID_PROVIDERS

    if secrets_provider_raw not in _VALID_PROVIDERS:
        raise SeedError(f"secrets.provider must be one of {sorted(_VALID_PROVIDERS)}, got: {secrets_provider_raw!r}")
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
        for fk, fv in cast("_StrObjDict", field_map_raw).items():
            if not isinstance(fv, str):
                raise SeedError(f"secrets.field_map values must be strings, got: {type(fv).__name__}")
            field_map[fk] = fv
    return SecretsConfig(
        provider=secrets_provider_raw,  # type: ignore[arg-type]
        path=secrets_path_raw,
        ttl=secrets_ttl_raw,
        field_map=field_map,
    )


def _parse_optional_str(d: dict[str, object], key: str, section: str) -> str | None:
    """Parse an optional string field from a dict, raising SeedError on type mismatch."""
    val = d.get(key)
    if val is None:
        return None
    if not isinstance(val, str):
        raise SeedError(f"{section}.{key} must be a string")
    return val


def _parse_key_rotation(raw: object) -> KeyRotationConfig | None:
    """Parse the optional ``key_rotation`` section."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SeedError(f"key_rotation must be a mapping, got: {type(raw).__name__}")
    kr_dict: dict[str, object] = cast("_StrObjDict", raw)

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

    kr_patterns_raw: object = kr_dict.get("leak_patterns")
    kr_patterns: list[str] = []
    if kr_patterns_raw is not None:
        if not isinstance(kr_patterns_raw, list):
            raise SeedError(f"key_rotation.leak_patterns must be a list, got: {type(kr_patterns_raw).__name__}")
        kr_patterns = [str(p) for p in kr_patterns_raw]

    return KeyRotationConfig(
        interval_seconds=kr_interval,
        on_leak=kr_on_leak_raw,  # type: ignore[arg-type]
        secrets_provider=_parse_optional_str(kr_dict, "secrets_provider", "key_rotation"),
        secrets_path=_parse_optional_str(kr_dict, "secrets_path", "key_rotation"),
        leak_patterns=kr_patterns,
    )


def _parse_compliance(raw: object) -> ComplianceConfig | None:
    """Parse the optional ``compliance`` section."""
    if raw is None:
        return None
    if isinstance(raw, str):
        _valid_presets = tuple(p.value for p in CompliancePreset)
        if raw.lower() not in _valid_presets:
            raise SeedError(f"compliance must be one of {list(_valid_presets)} or a mapping, got: {raw!r}")
        return ComplianceConfig.from_preset(CompliancePreset(raw.lower()))
    if isinstance(raw, dict):
        return ComplianceConfig.from_dict(cast(_CAST_DICT_STR_ANY, raw))
    raise SeedError(f"compliance must be a string or mapping, got: {type(raw).__name__}")


def _parse_formal_verification(raw: object) -> FormalVerificationConfig | None:
    """Parse the optional ``formal_verification`` section."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SeedError(f"formal_verification must be a mapping, got: {type(raw).__name__}")
    fv_dict: dict[str, object] = cast("_StrObjDict", raw)
    fv_enabled = fv_dict.get("enabled", True)
    if not isinstance(fv_enabled, bool):
        raise SeedError(f"formal_verification.enabled must be a bool, got: {type(fv_enabled).__name__}")
    fv_block = fv_dict.get("block_on_violation", True)
    if not isinstance(fv_block, bool):
        raise SeedError(f"formal_verification.block_on_violation must be a bool, got: {type(fv_block).__name__}")
    fv_timeout = fv_dict.get("timeout_s", 60)
    if not isinstance(fv_timeout, int):
        raise SeedError(f"formal_verification.timeout_s must be an integer, got: {type(fv_timeout).__name__}")
    fv_properties = _parse_formal_properties(fv_dict.get("properties", []))
    return FormalVerificationConfig(
        enabled=fv_enabled,
        properties=fv_properties,
        timeout_s=fv_timeout,
        block_on_violation=fv_block,
    )


def _parse_quality_gates(raw: object) -> QualityGatesConfig | None:
    """Parse the optional ``quality_gates`` section."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SeedError(f"quality_gates must be a mapping, got: {type(raw).__name__}")
    qg_dict: dict[str, object] = cast("_StrObjDict", raw)

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
        list_raw = qg_dict.get(key, default)
        if not isinstance(list_raw, list):
            raise SeedError(f"quality_gates.{key} must be a list, got: {type(list_raw).__name__}")
        if not all(isinstance(item, str) for item in list_raw):
            raise SeedError(f"quality_gates.{key} must contain only strings")
        return [str(item) for item in list_raw]

    pipeline = _parse_quality_gate_pipeline(qg_dict.get("pipeline"))
    pii_scan_paths_raw = qg_dict.get("pii_scan_paths", ["src/"])
    if not isinstance(pii_scan_paths_raw, list):
        raise SeedError(f"quality_gates.pii_scan_paths must be a list, got: {type(pii_scan_paths_raw).__name__}")
    benchmark_cfg = _parse_quality_gate_benchmark(qg_dict.get("benchmark"))

    return QualityGatesConfig(
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
        pii_scan_paths=[str(p) for p in pii_scan_paths_raw],
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
        dead_code_check_lost_callers=_qg_bool("dead_code_check_lost_callers", True),
        dead_code_check_unused_imports=_qg_bool("dead_code_check_unused_imports", True),
        dead_code_check_unreachable=_qg_bool("dead_code_check_unreachable", True),
        comment_quality_check=_qg_bool("comment_quality_check", False),
        comment_quality_docstyle=_qg_str("comment_quality_docstyle", "auto"),
        import_cycle_check=_qg_bool("import_cycle_check", False),
        import_cycle_command=_qg_optional_str("import_cycle_command"),
        merge_conflict_check=_qg_bool("merge_conflict_check", False),
        flaky_detection=_qg_bool("flaky_detection", False),
        flaky_min_runs=_qg_int("flaky_min_runs", 5),
        flaky_threshold=float(qg_dict.get("flaky_threshold", 0.15)),
        auto_format=_qg_bool("auto_format", False),
        auto_format_python_command=_qg_str("auto_format_python_command", "ruff format"),
        auto_format_js_command=_qg_str("auto_format_js_command", "prettier --write"),
        auto_format_rust_command=_qg_str("auto_format_rust_command", "rustfmt"),
        benchmark=benchmark_cfg,
    )


def _parse_single_pipeline_step(index: int, entry: object) -> GatePipelineStep:
    """Parse a single pipeline step entry."""
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
    return GatePipelineStep(name=name, required=required, condition=condition, command_override=command_override)


def _parse_quality_gate_pipeline(raw: object) -> list[GatePipelineStep] | None:
    """Parse the quality_gates.pipeline list."""
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise SeedError(f"quality_gates.pipeline must be a list, got: {type(raw).__name__}")
    return list(starmap(_parse_single_pipeline_step, enumerate(raw)))


def _parse_quality_gate_benchmark(raw: object) -> BenchmarkConfig:
    """Parse the quality_gates.benchmark sub-config."""
    if raw is None:
        return BenchmarkConfig()
    if not isinstance(raw, dict):
        raise SeedError(f"quality_gates.benchmark must be a mapping, got: {type(raw).__name__}")
    bm_dict: dict[str, object] = cast("_StrObjDict", raw)
    bm_enabled = bm_dict.get("enabled", False)
    if not isinstance(bm_enabled, bool):
        raise SeedError(f"quality_gates.benchmark.enabled must be a bool, got: {type(bm_enabled).__name__}")
    bm_command = bm_dict.get(
        "command",
        "uv run pytest benchmarks/ --benchmark-json=.benchmark_results.json -q",
    )
    if not isinstance(bm_command, str):
        raise SeedError(f"quality_gates.benchmark.command must be a string, got: {type(bm_command).__name__}")
    bm_threshold = bm_dict.get("threshold", 0.10)
    if not isinstance(bm_threshold, (int, float)):
        raise SeedError(f"quality_gates.benchmark.threshold must be a number, got: {type(bm_threshold).__name__}")
    return BenchmarkConfig(enabled=bm_enabled, command=bm_command, threshold=float(bm_threshold))


def _parse_single_formal_property(idx: int, entry: object) -> FormalProperty:
    """Parse a single formal verification property entry."""
    from typing import Literal as _Literal

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
        raise SeedError(f"formal_verification.properties[{idx}].checker must be 'z3' or 'lean4', got: {prop_checker!r}")
    prop_lemmas = entry.get("lemmas_file")
    if prop_lemmas is not None and not isinstance(prop_lemmas, str):
        raise SeedError(f"formal_verification.properties[{idx}].lemmas_file must be a string")
    return FormalProperty(
        name=prop_name,
        invariant=prop_invariant,
        checker=cast("_Literal['z3', 'lean4']", prop_checker),
        lemmas_file=prop_lemmas if isinstance(prop_lemmas, str) else None,
    )


def _parse_formal_properties(raw: object) -> list[FormalProperty]:
    """Parse the ``formal_verification.properties`` list."""
    if not isinstance(raw, list):
        raise SeedError("formal_verification.properties must be a list")
    return list(starmap(_parse_single_formal_property, enumerate(raw)))


def _parse_catalogs(raw: object) -> CatalogRegistry | None:
    """Parse the optional ``catalogs`` list."""
    if raw is None:
        return None
    if not isinstance(raw, list):
        raise SeedError(f"catalogs must be a list, got: {type(raw).__name__}")
    try:
        return CatalogRegistry.from_config(cast("list[dict[str, Any]]", raw))
    except ValueError as exc:
        raise SeedError(f"Invalid catalogs configuration: {exc}") from exc


def _parse_model_policy(raw: object) -> dict[str, Any] | None:
    """Parse the optional ``model_policy`` mapping."""
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise SeedError(f"model_policy must be a mapping, got: {type(raw).__name__}")
    return cast(_CAST_DICT_STR_ANY, raw)


def _parse_visual(raw: object) -> Any:
    """Parse the optional ``visual`` config section."""
    if raw is None:
        return None
    try:
        return parse_visual_config(raw)
    except ValueError as exc:
        raise SeedError(str(exc)) from exc


def _parse_sandbox(raw: object) -> Any:
    """Parse the optional ``sandbox`` config section."""
    if raw is None:
        return None
    try:
        return parse_docker_sandbox(raw)
    except ValueError as exc:
        raise SeedError(str(exc)) from exc


def _validate_optional_str(data: dict[str, object], key: str, default: str) -> str:
    """Extract and validate a string field with a default."""
    raw: object = data.get(key, default)
    if not isinstance(raw, str):
        raise SeedError(f"{key} must be a string, got: {type(raw).__name__}")
    return raw


def _validate_optional_bool(data: dict[str, object], key: str, default: bool) -> bool:
    """Extract and validate a boolean field with a default."""
    raw: object = data.get(key, default)
    if not isinstance(raw, bool):
        raise SeedError(f"{key} must be a boolean, got: {type(raw).__name__}")
    return raw


def _parse_cost_tags(raw: object) -> dict[str, str]:
    """Parse the optional ``cost_tags`` mapping."""
    if not isinstance(raw, dict):
        raise SeedError(f"cost_tags must be a mapping, got: {type(raw).__name__}")
    return {str(k): str(v) for k, v in raw.items()}


def _parse_cost_envelopes(data: dict[str, object]) -> dict[str, dict[str, Any]]:
    """Parse the optional ``cost.envelopes`` block (issue #1405).

    Accepted shapes::

        cost:
          envelopes:
            subscription:
              budget_usd: 50
              hard_budget_usd: 60
              threshold_pct: 0.8
              model_allowlist: [opus, sonnet]
            agent-sdk-credits:
              budget_usd: 20

    Returns an empty mapping when the block is absent so the legacy
    single-envelope rollup is preserved verbatim.
    """
    cost_block_raw: object = data.get("cost")
    if cost_block_raw is None:
        return {}
    if not isinstance(cost_block_raw, dict):
        raise SeedError(f"cost must be a mapping, got: {type(cost_block_raw).__name__}")
    cost_block = cast("dict[str, Any]", cost_block_raw)
    envelopes_raw: object = cost_block.get("envelopes")
    if envelopes_raw is None:
        return {}
    if not isinstance(envelopes_raw, dict):
        raise SeedError(f"cost.envelopes must be a mapping, got: {type(envelopes_raw).__name__}")
    envelopes = cast("dict[str, Any]", envelopes_raw)
    out: dict[str, dict[str, Any]] = {}
    for name, payload in envelopes.items():
        if not isinstance(payload, dict):
            raise SeedError(f"cost.envelopes.{name} must be a mapping, got: {type(payload).__name__}")
        payload_dict = cast("dict[str, Any]", payload)
        norm: dict[str, Any] = {}
        if "budget_usd" in payload_dict:
            norm["budget_usd"] = _parse_budget(cast("str | int | float | None", payload_dict["budget_usd"])) or 0.0
        if "hard_budget_usd" in payload_dict:
            norm["hard_budget_usd"] = (
                _parse_budget(cast("str | int | float | None", payload_dict["hard_budget_usd"])) or 0.0
            )
        if "threshold_pct" in payload_dict:
            tp_raw = payload_dict["threshold_pct"]
            if not isinstance(tp_raw, int | float) or not (0.0 < float(tp_raw) <= 1.0):
                raise SeedError(f"cost.envelopes.{name}.threshold_pct must be a number in (0, 1], got: {tp_raw!r}")
            norm["threshold_pct"] = float(tp_raw)
        if "model_allowlist" in payload_dict:
            allow_raw = payload_dict["model_allowlist"]
            if not isinstance(allow_raw, list | tuple):
                raise SeedError(
                    f"cost.envelopes.{name}.model_allowlist must be a list, got: {type(allow_raw).__name__}"
                )
            norm["model_allowlist"] = [str(x) for x in cast("list[Any]", allow_raw) if str(x).strip()]
        out[name] = norm
    return out


def _parse_cli(data: dict[str, object]) -> Literal["claude", "codex", "gemini", "qwen", "auto"]:
    cli_raw: object = data.get("cli", "auto")
    if cli_raw not in _VALID_CLIS:
        raise SeedError(f"cli must be one of {sorted(_VALID_CLIS)}, got: {cli_raw!r}")
    return cast("Literal['claude', 'codex', 'gemini', 'qwen', 'auto']", cli_raw)


def _parse_max_agents(data: dict[str, object]) -> int:
    max_agents_raw: object = data.get("max_agents", 6)
    if not isinstance(max_agents_raw, int) or max_agents_raw < 1:
        raise SeedError(f"max_agents must be a positive integer, got: {max_agents_raw!r}")
    return max_agents_raw


def _parse_model(data: dict[str, object]) -> object:
    model_raw: object = data.get("model")
    if model_raw is not None and not isinstance(model_raw, str):
        raise SeedError(f"model must be a string, got: {type(model_raw).__name__}")
    return model_raw


def _parse_max_cost_per_agent(data: dict[str, object]) -> float:
    raw: object = data.get("max_cost_per_agent")
    if raw is None:
        return 0.0
    val = _parse_budget(cast(_CAST_STR_INT_FLOAT_NONE, raw)) or 0.0
    if val < 0:
        raise SeedError(f"max_cost_per_agent must be >= 0, got: {raw!r}")
    return val


def _parse_optional_str_field(data: dict[str, object], field: str) -> object:
    raw: object = data.get(field)
    if raw is not None and not isinstance(raw, str):
        raise SeedError(f"{field} must be a string path, got: {type(raw).__name__}")
    return raw


def _parse_mcp_servers(data: dict[str, object]) -> object:
    raw: object = data.get("mcp_servers")
    if raw is not None and not isinstance(raw, dict):
        raise SeedError(f"mcp_servers must be a mapping, got: {type(raw).__name__}")
    return raw


def _parse_mcp_signing_mode(data: dict[str, object]) -> Literal["warn", "strict", "off"]:
    """Parse the ``mcp.signing_mode`` knob from bernstein.yaml.

    The block is::

        mcp:
          signing_mode: warn   # warn|strict|off (default: warn)

    Returns ``"warn"`` when no ``mcp:`` block is present so the
    default-on path keeps working without operator action.

    Note: YAML 1.1 parses bare ``off`` / ``on`` as booleans.  We map
    ``False -> "off"`` so operators who write ``signing_mode: off``
    (the obvious form) get the intuitive behaviour without remembering
    to quote.  The ``on``/``True`` form is rejected because there is
    no ``on`` mode - they likely meant ``warn`` or ``strict``.

    Raises:
        SeedError: When the value is not one of the three permitted
            literals - operators must spell it correctly so a typo
            doesn't silently downgrade enforcement.
    """
    mcp_block = data.get("mcp")
    if mcp_block is None:
        return "warn"
    if not isinstance(mcp_block, dict):
        raise SeedError(f"mcp must be a mapping, got: {type(mcp_block).__name__}")
    raw = mcp_block.get("signing_mode", "warn")
    # YAML 1.1: bare ``off`` -> False; remap so operators who write the
    # un-quoted form keep getting the documented behaviour.
    if raw is False:
        raw = "off"
    if not isinstance(raw, str):
        raise SeedError(f"mcp.signing_mode must be a string, got: {type(raw).__name__}")
    candidate = raw.strip().lower()
    if candidate not in ("warn", "strict", "off"):
        raise SeedError(f"mcp.signing_mode must be one of warn|strict|off, got: {raw!r}")
    return cast("Literal['warn', 'strict', 'off']", candidate)


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

    data: dict[str, object] = cast("_StrObjDict", data_raw)

    # --- Required fields ---
    goal: object = data.get("goal")
    if not goal or not isinstance(goal, str):
        raise SeedError("Seed file must contain a non-empty 'goal' string.")

    # --- Optional fields ---
    budget_usd = _parse_budget(cast(_CAST_STR_INT_FLOAT_NONE, data.get("budget")))
    team = _parse_team(data.get("team"))

    # ``team_manifest: <name>[@sha256]`` expands deterministically into the
    # inline ``team`` + ``role_model_policy`` structures before validation,
    # so everything downstream of this point sees only the existing shapes.
    team_manifest_name: str | None = None
    team_manifest_digest: str | None = None
    role_policy_raw: object = data.get("role_model_policy")
    raw_manifest_ref = data.get("team_manifest")
    if raw_manifest_ref is not None:
        team, role_policy_raw, team_manifest_name, team_manifest_digest = _expand_team_manifest(
            raw_manifest_ref,
            raw_team=data.get("team"),
            raw_role_policy=role_policy_raw,
            workdir=path.parent,
        )

    cli = _parse_cli(data)
    max_agents_raw = _parse_max_agents(data)
    model_raw = _parse_model(data)
    max_cost_per_agent = _parse_max_cost_per_agent(data)

    constraints = _parse_string_list(data.get("constraints"), "constraints")
    context_files = _parse_string_list(data.get("context_files"), "context_files")
    # ``local_endpoints`` profiles are parsed first so a role's ``endpoint``
    # reference can be validated and resolved against them (issue #2356).
    local_endpoints = _parse_local_endpoints(data.get("local_endpoints"))
    role_model_policy = _parse_role_model_policy(role_policy_raw, local_endpoints=local_endpoints)

    # AC4: every declared response_style
    # must be renderable from the mode-profile templates visible from the
    # seed file's directory. A workdir override directory that lacks the
    # mapped template file fails here with the typed template error instead
    # of surfacing as a spawn failure mid-run.
    if role_model_policy:
        declared_styles = [
            str(entry[_ROLE_POLICY_STYLE_KEY])
            for entry in role_model_policy.values()
            if _ROLE_POLICY_STYLE_KEY in entry
        ]
        if declared_styles:
            from bernstein.core.agents.response_style import (
                ResponseStyleTemplateError,
                validate_style_templates,
            )

            try:
                validate_style_templates(declared_styles, workdir=path.parent)
            except ResponseStyleTemplateError as exc:
                raise SeedError(f"role_model_policy response_style validation failed: {exc}") from exc

    agent_catalog_raw = _parse_optional_str_field(data, "agent_catalog")
    mcp_servers_raw = _parse_mcp_servers(data)
    mcp_allowlist_raw: object = data.get("mcp_allowlist")
    mcp_allowlist: tuple[str, ...] | None = (
        None if mcp_allowlist_raw is None else _parse_string_list(mcp_allowlist_raw, "mcp_allowlist")
    )

    catalogs = _parse_catalogs(data.get("catalogs"))
    notify = _parse_notify(data.get("notify"))
    webhooks = _parse_webhooks(data.get("webhooks"))
    storage = _parse_storage(data.get("storage"))

    cells_raw: object = data.get("cells", 1)
    if not isinstance(cells_raw, int) or cells_raw < 1:
        raise SeedError(f"cells must be a positive integer, got: {cells_raw!r}")

    cluster = _parse_cluster(data.get("cluster"))
    session_cfg = _parse_session(data.get("session"))
    github_cfg = _parse_github(data.get("github"))
    workspace = _parse_workspace(data.get("workspace"), data.get("repos"), path.parent)
    worktree_setup = _parse_worktree_setup(data.get("worktree_setup"))
    batch = _parse_batch(data.get("batch"))
    test_agent = _parse_test_agent(data.get("test_agent"))
    model_policy = _parse_model_policy(data.get("model_policy"))
    quality_gates = _parse_quality_gates(data.get("quality_gates"))
    formal_verification = _parse_formal_verification(data.get("formal_verification"))
    secrets = _parse_secrets(data.get("secrets"))
    key_rotation = _parse_key_rotation(data.get("key_rotation"))
    compliance = _parse_compliance(data.get("compliance"))
    visual = _parse_visual(data.get("visual"))
    sandbox = _parse_sandbox(data.get("sandbox"))
    bridges = _parse_bridge_settings(data.get("bridges"))
    cors = _parse_cors_config(data.get("cors"))
    dashboard_auth = _parse_dashboard_auth(data.get("dashboard_auth"))
    network = _parse_network_config(data.get("network"))
    rate_limit = _parse_rate_limit_config(data.get("rate_limit"))
    tenants = _parse_tenants(data.get("tenants"))

    internal_llm_provider_raw = _validate_optional_str(data, "internal_llm_provider", "openrouter_free")
    internal_llm_model_raw = _validate_optional_str(data, "internal_llm_model", "nvidia/nemotron-3-super-120b-a12b")
    judge_model_raw = _parse_optional_str_field(data, "judge_model")
    judge_provider_raw = _parse_optional_str_field(data, "judge_provider")
    model_fallback = _parse_model_fallback(data.get("model_fallback"))
    provider_availability = _parse_provider_availability(data.get("provider_availability"))
    cost_tags = _parse_cost_tags(data.get("cost_tags", {}))
    cost_autopilot_raw = _validate_optional_bool(data, "cost_autopilot", False)
    cost_envelopes = _parse_cost_envelopes(data)
    deployment_strategy_raw = _validate_optional_str(data, "deployment_strategy", "rolling")

    org_policies_raw: object = data.get("org_policies", [])
    if not isinstance(org_policies_raw, list):
        raise SeedError(f"org_policies must be a list of file paths, got: {type(org_policies_raw).__name__}")
    org_policies: list[str] = [str(p) for p in org_policies_raw]

    metrics = _parse_metrics(data.get("metrics"))
    mcp_signing_mode = _parse_mcp_signing_mode(data)
    _parse_tuning(data)

    return SeedConfig(
        goal=goal,
        budget_usd=budget_usd,
        team=team,
        team_manifest=team_manifest_name,
        team_manifest_digest=team_manifest_digest,
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
        github=github_cfg,
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
        judge_model=cast("str | None", judge_model_raw),
        judge_provider=cast("str | None", judge_provider_raw),
        model_fallback=model_fallback,
        provider_availability=provider_availability,
        cost_tags=cost_tags,
        cost_autopilot=cost_autopilot_raw,
        cost_envelopes=cost_envelopes,
        deployment_strategy=deployment_strategy_raw,
        org_policies=org_policies,
        metrics=metrics,
        mcp_signing_mode=mcp_signing_mode,
    )
