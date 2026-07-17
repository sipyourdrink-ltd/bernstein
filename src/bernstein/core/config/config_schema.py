"""Pydantic schema for bernstein.yaml with validation, env expansion, and migration.

CFG-001: Pydantic model matching bernstein.yaml structure with JSON Schema export.
CFG-002: Cross-field validators for conflicting settings.
CFG-003: Secure ${VAR} / ${VAR:-default} environment variable expansion.
CFG-004: Config version field with migration registry.
CFG-005: File path existence checks for config-referenced paths.
"""

from __future__ import annotations

import json
import logging
import os
import re
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal, cast

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# CFG-003: Secure environment variable expansion
# ---------------------------------------------------------------------------

# Matches ${VAR} and ${VAR:-default}
_ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_]\w*)(?::-(.*?))?\}")

# Variables that must never be expanded (prevent exfiltration of secrets
# through config values that end up in logs or error messages).
_BLOCKED_ENV_VARS = frozenset(
    {
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "GITHUB_TOKEN",
        "GH_TOKEN",
    }
)


class EnvExpansionError(Exception):
    """Raised when environment variable expansion fails."""


def expand_env_vars(value: str, *, field_name: str = "<unknown>") -> str:
    """Expand ``${VAR}`` and ``${VAR:-default}`` patterns in a string.

    Args:
        value: Raw string that may contain env var references.
        field_name: Config field name for error messages.

    Returns:
        String with all env var references expanded.

    Raises:
        EnvExpansionError: If a referenced variable is unset and has no default.
    """

    def _replace(match: re.Match[str]) -> str:
        var_name = match.group(1)
        default = match.group(2)  # None when no :- was present

        if var_name in _BLOCKED_ENV_VARS:
            raise EnvExpansionError(
                f"Environment variable {var_name!r} is blocked from expansion "
                f"in field {field_name!r} for security reasons."
            )

        env_value = os.environ.get(var_name)
        if env_value is not None:
            return env_value
        if default is not None:
            return default
        raise EnvExpansionError(
            f"Environment variable {var_name!r} is not set and no default "
            f"provided in field {field_name!r}. Use ${{VAR:-default}} to "
            f"provide a fallback."
        )

    return _ENV_VAR_RE.sub(_replace, value)


def expand_env_recursive(data: object, *, path: str = "") -> object:
    """Recursively expand env vars in all string values of a nested structure.

    Args:
        data: Nested dict/list/scalar from parsed YAML.
        path: Dotted field path for error messages.

    Returns:
        Structure with all string values expanded.
    """
    if isinstance(data, str):
        return expand_env_vars(data, field_name=path)
    if isinstance(data, dict):
        src = cast("dict[str, object]", data)
        return {k: expand_env_recursive(v, path=f"{path}.{k}" if path else k) for k, v in src.items()}
    if isinstance(data, list):
        src_list = cast("list[object]", data)
        return [expand_env_recursive(item, path=f"{path}[{i}]") for i, item in enumerate(src_list)]
    return data


# ---------------------------------------------------------------------------
# CFG-001: Pydantic models for bernstein.yaml
# ---------------------------------------------------------------------------


class NotifyConfigSchema(BaseModel):
    """Webhook notification configuration."""

    model_config = ConfigDict(extra="forbid")

    webhook: str | None = None
    on_complete: bool = True
    on_failure: bool = True
    desktop: bool = False


class QualityGatesSchema(BaseModel):
    """Quality gate configuration."""

    model_config = ConfigDict(extra="allow")

    enabled: bool = True
    lint: bool = False
    lint_command: str = "ruff check ."
    type_check: bool = False
    type_check_command: str = "pyright ."
    tests: bool = False
    test_command: str = "pytest tests/ -x -q"
    # Citation/reference existence verifier (issue #1402). Off by default to
    # keep the hot path zero-cost; opt in per project by setting to true.
    verify_citations: bool = False
    verify_citations_offline: bool = False
    verify_citations_allowed_hosts: list[str] | None = None


class CouncilCandidateConfig(BaseModel):
    """One candidate (or judge) endpoint in a ``council`` block.

    Mirrors the subset of :class:`RoleModelPolicyEntry`'s endpoint fields
    a single council member needs: which model, and optionally which
    OpenAI-compatible endpoint/credential to reach it through. ``base_url``
    /``api_key_env`` follow the exact same semantics and fail-closed
    credential-allowlist validation as the top-level role policy fields of
    the same name (see :class:`RoleModelPolicyEntry`) - ``api_key_env`` is
    always the NAME of an environment variable, never a literal key.
    """

    model_config = ConfigDict(extra="forbid")

    model: str
    base_url: str | None = None
    api_key_env: str | None = None


class CouncilConfig(BaseModel):
    """ "Council of agents" fan-out/judge configuration for one role.

    When set on a :class:`RoleModelPolicyEntry` (or loaded from a
    ``role_model_policy.<role>.model: "*.yaml"`` council definition file -
    see ``openai_agents_runner._load_council_config``), the role's ENTIRE
    task run is driven by a task-level council instead of a single model:
    every ``candidates`` entry runs the WHOLE task independently in
    parallel (its own full multi-turn run), then ``judge`` synthesizes one
    improved answer from whichever candidates survived. See
    ``src/bernstein/adapters/council_runner.py`` (``run_council``) for the
    runtime implementation and ``openai_agents_runner._run_session``'s
    ``manifest.council`` branch for how this config drives a run instead of
    a single ``Runner.run_sync`` call.
    """

    model_config = ConfigDict(extra="forbid")

    candidates: list[CouncilCandidateConfig]
    judge: CouncilCandidateConfig
    timeout: float = 60.0


class LocalEndpointProfileSchema(BaseModel):
    """A named OpenAI-compatible local endpoint profile (issue #2356).

    Declares once, under ``local_endpoints.<name>``, where a local runtime
    (for example ollama, LM Studio, or an MLX server) is reachable and which
    model it serves. Role entries reference the profile by name via
    ``role_model_policy.<role>.endpoint`` and inherit its ``base_url`` /
    ``model`` / ``api_key_env`` at validation time, so the fleet wiring
    lives in one place instead of being copy-pasted per role.

    ``api_key_env`` is the NAME of an environment variable, never a literal
    key -- the same fail-closed semantics as
    :class:`RoleModelPolicyEntry.api_key_env`. ``engine`` is a free-form
    runtime label recorded in the certification receipt for provenance.

    Whether the endpoint may carry a merge-critical role is NOT a field
    here by design: certification is a signed receipt produced by
    ``bernstein doctor --endpoint`` (see
    :mod:`bernstein.core.endpoints.certification`), verified at config load.
    """

    model_config = ConfigDict(extra="forbid")

    base_url: str
    model: str
    api_key_env: str | None = None
    engine: str | None = None
    timeout: float = Field(default=120.0, gt=0)


class RoleModelPolicyEntry(BaseModel):
    """Per-role model/provider policy.

    ``base_url`` and ``api_key_env`` let a role target an OpenAI-compatible
    endpoint other than the default: some roles (for example a local
    reasoning model behind an OpenAI-compatible gateway) must not share the
    default endpoint's credentials. ``api_key_env`` is the NAME of the
    environment variable holding that endpoint's key, never a literal key;
    it is validated against the same fail-closed credential allowlist the
    ``openai_agents`` runner enforces so a repo-carried config cannot
    forward arbitrary host secrets to an arbitrary endpoint.

    ``temperature``/``top_p``/``top_k``/``max_tokens``/``extra_params`` mirror
    the sampling-field shape already proven on the ``ModelCard`` dataclass
    (see ``feat/model-cards-phase1``): they are per-role sampling overrides
    that flow into the per-spawn ``mcp_config`` via
    :meth:`bernstein.core.agents.spawner_core.AgentSpawner._apply_sampling_overrides`,
    taking precedence over a resolved :class:`ModeProfile`'s sampling
    defaults for the same role. The spawn-time capability gate
    (:func:`bernstein.adapters.plugin_sdk.ensure_sampling_params_supported`)
    still decides whether the target adapter actually honours them -
    setting these fields for a role pinned to an adapter that does not
    declare sampling support raises ``SamplingParamsRefusal`` rather than
    silently dropping the override.
    """

    model_config = ConfigDict(extra="forbid")

    cli: str | None = None
    provider: str | None = None
    model: str | None = None
    effort: str | None = None
    base_url: str | None = None
    api_key_env: str | None = None
    # Name of a ``local_endpoints`` profile this role runs on (issue #2356).
    # Mutually exclusive with inline ``base_url``/``model``/``api_key_env``;
    # the profile's endpoint fields are materialized onto this entry at
    # validation time so downstream consumers see one resolved shape.
    # Merge-critical roles referencing a profile are additionally gated on a
    # verified certification receipt in :func:`load_and_validate`.
    endpoint: str | None = None
    temperature: float | None = None
    top_p: float | None = None
    top_k: int | None = None
    max_tokens: int | None = None
    extra_params: dict[str, Any] = Field(default_factory=dict)
    # Per-role response-style profile applied at spawn
    # (``bernstein.core.agents.response_style``). Resolution order is
    # deterministic and documented there: ``Task.metadata['mode']`` > this
    # entry > the ``role_model_policy.default`` entry > ``"balanced"``.
    # ``balanced`` renders an empty style addendum, keeping unset-profile
    # spawns byte-identical to pre-change spawns.
    response_style: Literal["verbose", "balanced", "terse"] | None = None
    # Optional "council of agents" fan-out/judge override (see
    # ``CouncilConfig``). When set, this role's ENTIRE task run is driven by
    # a task-level council (``bernstein.adapters.council_runner.run_council``)
    # instead of a single model; ``model``/``base_url``/``api_key_env`` above
    # are then ignored in favor of the council's own per-candidate endpoints.
    council: CouncilConfig | None = None


class RoleConfigEntry(BaseModel):
    """Per-role adapter/model override."""

    model_config = ConfigDict(extra="allow")

    cli: str | None = None
    model: str | None = None


class ModelPolicySchema(BaseModel):
    """CISO-level model policy constraints."""

    model_config = ConfigDict(extra="allow")

    allowed_providers: list[str] | None = None
    denied_providers: list[str] | None = None
    prefer: str | None = None


class WorktreeSetupSchema(BaseModel):
    """Worktree environment setup."""

    model_config = ConfigDict(extra="forbid")

    symlink_dirs: list[str] = Field(default_factory=list)
    copy_files: list[str] = Field(default_factory=list)
    setup_command: str | None = None


class StorageSchema(BaseModel):
    """Storage backend configuration."""

    model_config = ConfigDict(extra="forbid")

    backend: Literal["memory", "postgres", "redis"] = "memory"
    database_url: str | None = None
    redis_url: str | None = None


class SovereignProfileSchema(BaseModel):
    """Sovereign deployment profile declaration (issue #2518).

    Declares the residency posture the ``--profile sovereign`` activation
    pins: the EU regions data may reside in and whether region enforcement is
    strict. The remaining posture axes (deny-all egress, offline catalog,
    local storage, compliance pack) are profile constants and need no config
    surface; the profile composes them. This block only exists so the
    residency regions are part of the config snapshot an auditor recomputes
    the posture hash from -- see
    :mod:`bernstein.core.security.deployment_profile`.
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(
        default=False,
        description="Marker that the workspace is intended to run under --profile sovereign.",
    )
    regions: list[str] = Field(
        default_factory=lambda: ["eu-central", "eu-west"],
        description="Residency regions data may reside in; sovereign requires the EU set.",
    )
    enforce_strict: bool = Field(
        default=True,
        description="When true, a residency region outside the pinned set halts the run.",
    )
    allowed_egress: list[str] = Field(
        default_factory=list,
        description=(
            "Egress allow-list under --profile sovereign, as host / host:port / CIDR tokens "
            "(e.g. a self-hosted EU model server on 10.0.0.5:11434). Empty means deny-all. "
            "Declared here (not via --allow-network) so the runtime policy and the signed "
            "posture attestation are sourced from the same config and cannot diverge. Every "
            "entry must resolve to a self-hosted / EU-region destination."
        ),
    )


class SessionSchema(BaseModel):
    """Session resume configuration."""

    model_config = ConfigDict(extra="forbid")

    resume: bool = True
    stale_after_minutes: int = Field(default=30, ge=1)


class GithubSchema(BaseModel):
    """GitHub integration configuration."""

    model_config = ConfigDict(extra="forbid")

    sync_backlog: bool = Field(
        default=False,
        description=(
            "Pull open GitHub issues into .sdd/backlog/open/ at bootstrap. "
            "Off by default: syncing every open issue can silently displace a "
            "seeded goal on a non-empty backlog. Overridable at runtime with "
            "BERNSTEIN_SYNC_GITHUB_BACKLOG."
        ),
    )


class ClusterSchema(BaseModel):
    """Cluster mode configuration."""

    model_config = ConfigDict(extra="allow")

    enabled: bool = False
    topology: Literal["star", "mesh", "hierarchical"] = "star"
    auth_token: str | None = None
    node_heartbeat_interval_s: int = Field(default=15, ge=1)
    node_timeout_s: int = Field(default=60, ge=1)
    server_url: str | None = None
    bind_host: str = "127.0.0.1"


class RemoteSchema(BaseModel):
    """Remote SSH execution configuration."""

    model_config = ConfigDict(extra="allow")

    host: str
    user: str | None = None
    port: int = Field(default=22, ge=1, le=65535)
    key: str | None = None
    remote_dir: str = "~/bernstein-workdir"
    rsync_excludes: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)


class AgencySchema(BaseModel):
    """Agency agent catalog configuration."""

    model_config = ConfigDict(extra="allow")

    path: str | None = None


class CatalogEntry(BaseModel):
    """One catalog source definition."""

    model_config = ConfigDict(extra="allow")

    name: str
    type: str = "agency"
    enabled: bool = True
    source: str | None = None
    path: str | None = None
    priority: int = 100


class FormalPropertySchema(BaseModel):
    """Single formal verification property."""

    model_config = ConfigDict(extra="allow")

    name: str
    invariant: str
    checker: Literal["z3", "lean4"] = "z3"
    lemmas_file: str | None = None


class FormalVerificationSchema(BaseModel):
    """Formal verification gateway configuration."""

    model_config = ConfigDict(extra="allow")

    enabled: bool = True
    block_on_violation: bool = True
    timeout_s: int = Field(default=60, ge=1)
    properties: list[FormalPropertySchema] = Field(default_factory=list)


class BatchSchema(BaseModel):
    """Batch mode configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    eligible: list[str] = Field(default_factory=list)


class ModelPriceSchema(BaseModel):
    """USD rate for one model, per 1 million tokens (issue #2354).

    The scheduling price table lives in config so an operator can override the
    shipped defaults without a code change. Rates are validated non-negative:
    a negative USD rate is a misconfiguration that would corrupt every
    downstream budget decision, so it fails at load time.
    """

    model_config = ConfigDict(extra="forbid")

    input: float = Field(..., ge=0, description="USD per 1M input tokens.")
    output: float = Field(..., ge=0, description="USD per 1M output tokens.")
    cache_read: float = Field(default=0.0, ge=0, description="USD per 1M cache-read tokens.")
    cache_write: float = Field(default=0.0, ge=0, description="USD per 1M cache-write tokens.")


class PricingSchema(BaseModel):
    """Versioned price-table overrides for cost-aware scheduling (#2354)."""

    model_config = ConfigDict(extra="forbid")

    as_of: str = Field(default="", description="ISO date (YYYY-MM-DD) the override rates were captured.")
    revision: int = Field(default=0, ge=0, description="Monotonic price-table revision counter.")
    models: dict[str, ModelPriceSchema] = Field(
        default_factory=dict,
        description="Per-model USD rates; each row overrides / extends the shipped defaults.",
    )


class CostCapsSchema(BaseModel):
    """USD ceilings enforced before dispatch (#2354). ``0`` means unlimited."""

    model_config = ConfigDict(extra="forbid")

    per_task_usd: float = Field(default=0.0, ge=0, description="Per-task USD ceiling; 0 = unlimited.")
    per_run_usd: float = Field(default=0.0, ge=0, description="Per-run USD ceiling; 0 = unlimited.")
    per_day_usd: float = Field(default=0.0, ge=0, description="Per-day USD ceiling; 0 = unlimited.")


class CostPolicySchema(BaseModel):
    """Cost-aware scheduling policy: pricing, USD caps, pools, cache window.

    Issue #2354. USD ceilings and per-pool caps drive deterministic dispatch
    decisions from the existing spend ledger; the cache-window fan-out opt-in
    defaults off (conservative) so priming a shared prompt cache is always a
    deliberate operator choice.
    """

    model_config = ConfigDict(extra="forbid")

    pricing: PricingSchema | None = Field(default=None, description="Price-table overrides.")
    caps: CostCapsSchema | None = Field(default=None, description="Per-task / run / day USD ceilings.")
    pools: dict[str, float] = Field(
        default_factory=dict,
        description="Per-pool USD caps keyed by pool name (e.g. api, subscription); 0 = unlimited.",
    )
    cache_window: bool = Field(
        default=False,
        description="Opt in to cache-window fan-out (one warm-up call primes a shared prefix); default off.",
    )

    @model_validator(mode="after")
    def _reject_negative_pool_caps(self) -> CostPolicySchema:
        for pool, cap in self.pools.items():
            if cap < 0:
                msg = f"cost_policy.pools[{pool!r}] cap must be >= 0 (0 means unlimited), got {cap}"
                raise ValueError(msg)
        return self


class TestAgentSchema(BaseModel):
    """Test agent configuration."""

    model_config = ConfigDict(extra="forbid")

    always_spawn: bool = False
    model: str = "sonnet"
    trigger: Literal["on_task_complete"] = "on_task_complete"


class SmtpSchema(BaseModel):
    """SMTP email notification configuration."""

    model_config = ConfigDict(extra="forbid")

    host: str
    port: int
    username: str = ""
    password: str = ""
    from_address: str = ""
    to_addresses: list[str] = Field(default_factory=list)


class CustomMetricSchema(BaseModel):
    """A single custom metric definition for domain-specific KPIs .

    Example in bernstein.yaml::

        metrics:
          code_per_dollar:
            formula: "lines_changed / total_cost"
            unit: "lines/$"
            description: "Code output per dollar spent"
          success_rate:
            formula: "tasks_completed / (tasks_completed + tasks_failed + 0.001)"
            unit: "ratio"
    """

    model_config = ConfigDict(extra="forbid")

    formula: str = Field(
        ...,
        min_length=1,
        description=(
            "Arithmetic expression using built-in variables: tasks_spawned, "
            "tasks_completed, tasks_failed, tasks_retried, errors, active_agents, "
            "open_tasks, tick_duration_ms, total_spawned, total_completed, "
            "total_failed, total_errors, total_cost, lines_changed, total_tokens."
        ),
    )
    unit: str = Field(default="", description='Unit label shown in dashboards (e.g. "lines/$").')
    description: str = Field(default="", description="Human-readable description of this metric.")
    alert_above: float | None = Field(
        default=None,
        description="Emit an alert when the metric value exceeds this threshold.",
    )
    alert_below: float | None = Field(
        default=None,
        description="Emit an alert when the metric value falls below this threshold.",
    )


class ModelFallbackSchema(BaseModel):
    """Model fallback chain configuration.

    Controls which HTTP error types trigger a model switch and which
    fallback models to try in sequence.

    Example::

        model_fallback:
          fallback_chain: [sonnet, gemini-flash, qwen]
          strike_limit: 3
          include_timeouts: true
    """

    model_config = ConfigDict(extra="forbid")

    fallback_chain: list[str] = Field(
        default_factory=list,
        description="Ordered list of fallback models. e.g. [sonnet, gemini-flash, qwen]",
    )
    strike_limit: int = Field(
        default=3,
        ge=1,
        description="Consecutive errors before falling back to next model.",
    )
    include_timeouts: bool = Field(
        default=True,
        description="Whether connection timeouts count toward the strike limit.",
    )
    trigger_codes: list[int] = Field(
        default_factory=lambda: [429, 503, 529],
        description="HTTP status codes that count as fallback-triggering errors.",
    )


class FallbackChainElementSchema(BaseModel):
    """One (adapter, model) pair in a role's provider fallback chain (#2355)."""

    model_config = ConfigDict(extra="forbid")

    adapter: str = Field(..., min_length=1, description="CLI adapter name, e.g. claude, codex, gemini.")
    model: str = Field(..., min_length=1, description="Model identifier dispatched through that adapter.")
    conformance: Literal["basic", "advanced", "expert"] = Field(
        default="basic",
        description="Declared conformance level, compared against the role's floor.",
    )


class RoleFallbackChainSchema(BaseModel):
    """Per-role fallback chain plus conformance floor (#2355).

    A chain element whose conformance is below the role's floor is rejected
    here, at config validation time, so a fallback is never silently less
    capable than the role requires.
    """

    model_config = ConfigDict(extra="forbid")

    conformance_floor: Literal["basic", "advanced", "expert"] = Field(
        default="basic",
        description="Minimum conformance every chain element must declare.",
    )
    chain: list[FallbackChainElementSchema] = Field(
        ...,
        min_length=1,
        description="Ordered fallback chain; the scheduler picks the first healthy element.",
    )

    @model_validator(mode="after")
    def _validate_floor(self) -> RoleFallbackChainSchema:
        """Reject chain elements below the conformance floor."""
        order = {"basic": 0, "advanced": 1, "expert": 2}
        floor = order[self.conformance_floor]
        for idx, element in enumerate(self.chain):
            if order[element.conformance] < floor:
                raise ValueError(
                    f"chain position {idx} ({element.adapter}/{element.model}) declares "
                    f"conformance {element.conformance!r}, below the role's floor "
                    f"{self.conformance_floor!r}."
                )
        return self


class ProviderAvailabilitySchema(BaseModel):
    """Provider availability policy: per-role fallback chains (#2355).

    Example::

        provider_availability:
          probe_ttl_minutes: 5
          probes_enabled: true
          roles:
            developer:
              conformance_floor: advanced
              chain:
                - {adapter: claude, model: opus, conformance: expert}
                - {adapter: codex, model: gpt-5.2, conformance: advanced}
    """

    model_config = ConfigDict(extra="forbid")

    probe_ttl_minutes: int = Field(
        default=5,
        ge=1,
        description="How long a provider health-probe result is cached before re-probing.",
    )
    probes_enabled: bool = Field(
        default=True,
        description="Disable for offline runs; every chain element is then presumed healthy.",
    )
    roles: dict[str, RoleFallbackChainSchema] = Field(
        default_factory=dict,
        description="Per-role fallback chains keyed by role name.",
    )


class ArchModuleEntry(BaseModel):
    """Boundary definition for one logical module in the architecture conformance config."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., min_length=1, description="Human-readable module name.")
    paths: list[str] = Field(
        default_factory=list,
        description="Glob patterns (relative to repo root) for files in this module.",
    )
    allowed_imports: list[str] = Field(
        default_factory=list,
        description=(
            "Module prefixes that files in this module may import. When non-empty, any unlisted import is a violation."
        ),
    )
    forbidden_imports: list[str] = Field(
        default_factory=list,
        description="Module prefixes that files in this module must not import.",
    )


class ArchConformanceSchema(BaseModel):
    """Architecture conformance checking against declared module boundaries (ROAD-171).

    Example bernstein.yaml::

        guardrails:
          arch_conformance:
            enabled: true
            block_on_violation: true
            modules:
              - name: core
                paths: ["src/bernstein/core/**"]
                forbidden_imports: ["bernstein.cli", "bernstein.adapters"]
    """

    model_config = ConfigDict(extra="forbid")

    enabled: bool = Field(default=False, description="Master switch for boundary checking.")
    modules: list[ArchModuleEntry] = Field(
        default_factory=list,
        description="Module boundary definitions.",
    )
    block_on_violation: bool = Field(
        default=True,
        description="Hard-block merge on violation (DENY). False → ASK (flag only).",
    )


class BernsteinConfig(BaseModel):
    """Top-level Pydantic model for bernstein.yaml.

    This model validates the complete bernstein.yaml configuration file.
    Use :meth:`json_schema` to export the JSON Schema representation.
    """

    model_config = ConfigDict(extra="allow")

    # --- CFG-004: Version field for migration ---
    config_version: int = Field(
        default=1,
        description="Config format version. Used for automated migration.",
    )

    # --- Required ---
    goal: str = Field(..., min_length=1, description="High-level project objective.")

    # --- Core settings ---
    cli: Literal["claude", "codex", "gemini", "qwen", "auto"] = Field(
        default="auto",
        description="CLI agent backend.",
    )
    max_agents: int = Field(default=6, ge=1, description="Maximum concurrent agents.")
    model: str | None = Field(default=None, description="Model override.")
    team: Literal["auto"] | list[str] = Field(default="auto", description="Role team selection.")
    budget: str | int | float | None = Field(default=None, description='Spending cap ("$20", 20, or 20.0).')

    # --- Behavioral flags ---
    evolution_enabled: bool = Field(default=True, description="Enable self-evolution loop.")
    auto_decompose: bool = Field(default=True, description="Enable LLM-based task decomposition.")
    merge_strategy: Literal["pr", "direct"] = Field(default="pr", description="How agent work reaches main branch.")
    auto_merge: bool = Field(default=True, description="Auto-merge PRs.")
    pr_labels: list[str] = Field(
        default_factory=lambda: ["bernstein", "auto-generated"],
    )

    # --- LLM provider ---
    # default 'none' so a fresh clone without OPENROUTER_API_KEY_*
    # env vars no longer crashes on first auto_decompose/evolution LLM call.
    # When set to 'none', evolution_enabled and auto_decompose are auto-disabled
    # unless the user explicitly enabled them (in which case we raise so the
    # misconfiguration surfaces loudly).
    internal_llm_provider: str = Field(
        default="none",
        description=(
            "LLM provider for manager reviews and planning. Use 'none' to "
            "disable evolution/auto_decompose. Providers like 'openrouter_free' "
            "require OPENROUTER_API_KEY_FREE or _PAID in the environment."
        ),
    )
    internal_llm_model: str = Field(
        default="nvidia/nemotron-3-super-120b-a12b",
        description="Model for internal LLM calls.",
    )
    judge_model: str | None = Field(
        default=None,
        description="Model for janitor LLM-judge calls. Falls back to the run's top-level model.",
    )
    judge_provider: str | None = Field(
        default=None,
        description="Provider for janitor LLM-judge calls. Falls back to the run's adapter provider.",
    )

    # --- Constraints and context ---
    constraints: list[str] = Field(default_factory=list)
    context_files: list[str] = Field(default_factory=list)

    # --- Nested configs ---
    quality_gates: QualityGatesSchema | None = None
    local_endpoints: dict[str, LocalEndpointProfileSchema] | None = Field(
        default=None,
        description=(
            "Named OpenAI-compatible endpoint profiles for local runtimes. "
            "Referenced from role_model_policy.<role>.endpoint."
        ),
    )
    role_model_policy: dict[str, RoleModelPolicyEntry] | None = None
    role_config: dict[str, RoleConfigEntry] | None = None
    model_policy: ModelPolicySchema | None = None
    worktree_setup: WorktreeSetupSchema | None = None
    notify: NotifyConfigSchema | None = None
    storage: StorageSchema | None = None
    sovereign: SovereignProfileSchema | None = None
    session: SessionSchema | None = None
    github: GithubSchema | None = None
    cluster: ClusterSchema | None = None
    remote: RemoteSchema | None = None
    agency: AgencySchema | None = None
    catalogs: list[CatalogEntry] | None = None
    formal_verification: FormalVerificationSchema | None = None
    batch: BatchSchema | None = None
    cost_policy: CostPolicySchema | None = None
    test_agent: TestAgentSchema | None = None
    smtp: SmtpSchema | None = None
    mcp_servers: dict[str, Any] | None = None
    model_fallback: ModelFallbackSchema | None = None
    provider_availability: ProviderAvailabilitySchema | None = None
    arch_conformance: ArchConformanceSchema | None = None
    metrics: dict[str, CustomMetricSchema] | None = Field(
        default=None,
        description=(
            "Custom metric definitions for domain-specific KPIs. "
            "Each key is the metric name; the value specifies the formula and display unit."
        ),
    )

    # --- Less common ---
    routing: dict[str, str] | None = None
    max_cost_per_agent: float | None = Field(default=None, ge=0)

    # --- CFG-002: Cross-field validators ---
    @model_validator(mode="after")
    def _validate_cross_fields(self) -> BernsteinConfig:
        """Check for conflicting settings combinations."""
        errors: list[str] = []

        # Negative budget with agents is contradictory.
        # Note: budget=0 or "$0" means UNLIMITED in Bernstein.
        budget_val = self._parse_budget_value()
        if budget_val is not None and budget_val < 0:
            errors.append(
                f"budget is {budget_val} which is negative. Use 0 or '$0' for unlimited, or a positive value for a cap."
            )

        # When internal_llm_provider is 'none' (or ""), features that
        # require an LLM must be disabled. If the user did not explicitly opt in
        # to the feature, silently disable it; if they explicitly enabled it,
        # surface a loud error so the misconfig is obvious.
        provider_disabled = self.internal_llm_provider in ("none", "")
        explicit_fields = self.model_fields_set

        if provider_disabled:
            if "auto_decompose" in explicit_fields and self.auto_decompose:
                errors.append(
                    "auto_decompose is enabled but internal_llm_provider is "
                    f"{self.internal_llm_provider!r}. Decomposition needs an LLM. "
                    "Either set a valid provider or disable auto_decompose."
                )
            elif self.auto_decompose:
                # Auto-disable the defaulted feature so the config validates.
                object.__setattr__(self, "auto_decompose", False)
                logger.info(
                    "internal_llm_provider='%s' - auto_decompose auto-disabled. Set an LLM provider to re-enable.",
                    self.internal_llm_provider,
                )

            if "evolution_enabled" in explicit_fields and self.evolution_enabled:
                errors.append(
                    "evolution_enabled is true but internal_llm_provider is "
                    f"{self.internal_llm_provider!r}. Evolution needs an LLM. "
                    "Either set a valid provider or disable evolution."
                )
            elif self.evolution_enabled:
                object.__setattr__(self, "evolution_enabled", False)
                logger.info(
                    "internal_llm_provider='%s' - evolution_enabled auto-disabled. Set an LLM provider to re-enable.",
                    self.internal_llm_provider,
                )

        # Preflight: openrouter_free requires OPENROUTER_API_KEY_FREE or _PAID.
        # Emit a loud warning (not a hard error) so fresh clones see the hint
        # before the first LLM call crashes. See.
        if self.internal_llm_provider == "openrouter_free" and not (
            os.environ.get("OPENROUTER_API_KEY_FREE") or os.environ.get("OPENROUTER_API_KEY_PAID")
        ):
            logger.warning(
                "internal_llm_provider='openrouter_free' but neither "
                "OPENROUTER_API_KEY_FREE nor OPENROUTER_API_KEY_PAID is set. "
                "LLM calls will fail at runtime. Set one of those env vars, or "
                "switch to 'internal_llm_provider: none' in bernstein.yaml to "
                "disable evolution and auto_decompose."
            )

        # Cluster with no auth_token is a security risk
        if self.cluster and self.cluster.enabled and not self.cluster.auth_token:
            errors.append(
                "cluster is enabled but no auth_token is set. This exposes the cluster API without authentication."
            )

        # Storage backend requires corresponding connection URL
        if self.storage:
            if self.storage.backend == "postgres" and not self.storage.database_url:
                errors.append("storage.backend is 'postgres' but database_url is not set.")
            if self.storage.backend == "redis" and not self.storage.redis_url:
                errors.append("storage.backend is 'redis' but redis_url is not set.")

        # Local endpoint profile references (issue #2356): resolve
        # role_model_policy.<role>.endpoint onto the referenced profile's
        # base_url/model/api_key_env so downstream consumers see one shape.
        self._resolve_local_endpoint_references(errors)

        if errors:
            raise ValueError("Configuration has conflicting settings:\n" + "\n".join(f"  - {e}" for e in errors))

        return self

    def _resolve_local_endpoint_references(self, errors: list[str]) -> None:
        """Materialize ``endpoint`` profile references onto role entries.

        A role entry naming a ``local_endpoints`` profile must not also pin
        an inline ``base_url``/``model``/``api_key_env``: the profile is the
        single source of truth for the endpoint, and the certification
        receipt is keyed on the profile's exact ``(base_url, model)`` pair.
        """
        if not self.role_model_policy:
            return
        profiles = self.local_endpoints or {}
        for role, entry in self.role_model_policy.items():
            if entry.endpoint is None:
                continue
            profile = profiles.get(entry.endpoint)
            if profile is None:
                known = ", ".join(sorted(profiles)) or "(none defined)"
                errors.append(
                    f"role_model_policy.{role}.endpoint references unknown "
                    f"local_endpoints profile {entry.endpoint!r}. Known profiles: {known}."
                )
                continue
            conflicts = [name for name in ("base_url", "model", "api_key_env") if getattr(entry, name) is not None]
            if conflicts:
                errors.append(
                    f"role_model_policy.{role}: {', '.join(conflicts)} cannot be set inline "
                    f"together with endpoint={entry.endpoint!r}; the profile pins the "
                    "certified endpoint. Move overrides into the profile."
                )
                continue
            entry.base_url = profile.base_url
            entry.model = profile.model
            entry.api_key_env = profile.api_key_env

    def _parse_budget_value(self) -> float | None:
        """Parse the budget field into a numeric value."""
        raw = self.budget
        if raw is None:
            return None
        if isinstance(raw, (int, float)):
            return float(raw)
        # raw is str at this point
        s = str(raw).strip().removeprefix("$")
        try:
            return float(s)
        except ValueError:
            return None

    @classmethod
    def json_schema(cls) -> dict[str, Any]:
        """Export the full JSON Schema for bernstein.yaml."""
        return cls.model_json_schema()


# ---------------------------------------------------------------------------
# CFG-004: Config version migration registry
# ---------------------------------------------------------------------------

# Type for migration functions: take a config dict, return an upgraded dict.
MigrationFn = Callable[[dict[str, Any]], dict[str, Any]]

CURRENT_CONFIG_VERSION = 1

_MIGRATIONS: dict[int, MigrationFn] = {}
# Maps source_version -> callable that upgrades to source_version+1.
# Example: _MIGRATIONS[1] upgrades v1 -> v2.


def register_migration(from_version: int, fn: MigrationFn) -> None:
    """Register a migration function for a specific config version.

    Args:
        from_version: The version this migration upgrades FROM.
        fn: Callable that takes and returns a config dict.
    """
    if from_version in _MIGRATIONS:
        raise ValueError(f"Migration from version {from_version} is already registered.")
    _MIGRATIONS[from_version] = fn


def migrate_config(data: dict[str, Any]) -> dict[str, Any]:
    """Apply all necessary migrations to bring config to current version.

    Args:
        data: Raw parsed YAML config dict.

    Returns:
        Config dict at CURRENT_CONFIG_VERSION.

    Raises:
        ValueError: If migration chain is broken or version is unsupported.
    """
    version = data.get("config_version", 1)
    if not isinstance(version, int):
        raise ValueError(f"config_version must be an integer, got {type(version).__name__}.")
    if version > CURRENT_CONFIG_VERSION:
        raise ValueError(
            f"Config version {version} is newer than supported "
            f"version {CURRENT_CONFIG_VERSION}. Please upgrade Bernstein."
        )
    if version < 1:
        raise ValueError(f"config_version must be >= 1, got {version}.")

    result = data.copy()
    while version < CURRENT_CONFIG_VERSION:
        fn = _MIGRATIONS.get(version)
        if fn is None:
            raise ValueError(f"No migration registered for version {version} -> {version + 1}. Cannot upgrade config.")
        result = fn(result)
        version += 1
        result["config_version"] = version

    return result


# ---------------------------------------------------------------------------
# CFG-005: File path validation
# ---------------------------------------------------------------------------


class ConfigPathError(Exception):
    """Raised when a config-referenced file path does not exist."""


def validate_file_paths(
    config: BernsteinConfig,
    project_root: Path,
) -> list[str]:
    """Check that all config-referenced file paths exist on disk.

    Args:
        config: Validated BernsteinConfig instance.
        project_root: Project root directory for resolving relative paths.

    Returns:
        List of error messages for missing paths. Empty list means all OK.
    """
    errors: list[str] = []
    _validate_context_files(config, project_root, errors)
    _validate_agency_path(config, project_root, errors)
    _validate_worktree_setup(config, project_root, errors)
    _validate_formal_verification(config, project_root, errors)
    _validate_remote_key(config, errors)
    _validate_catalog_paths(config, project_root, errors)
    return errors


def _validate_context_files(config: BernsteinConfig, root: Path, errors: list[str]) -> None:
    for ctx_file in config.context_files:
        resolved = root / ctx_file
        if not resolved.exists():
            errors.append(f"context_files: path {ctx_file!r} does not exist (resolved to {resolved})")


def _validate_agency_path(config: BernsteinConfig, root: Path, errors: list[str]) -> None:
    if not config.agency or not config.agency.path:
        return
    agency_path = Path(config.agency.path)
    if not agency_path.is_absolute():
        agency_path = root / agency_path
    if not agency_path.exists():
        errors.append(f"agency.path: {config.agency.path!r} does not exist (resolved to {agency_path})")


def _validate_worktree_setup(config: BernsteinConfig, root: Path, errors: list[str]) -> None:
    if not config.worktree_setup:
        return
    for sym_dir in config.worktree_setup.symlink_dirs:
        resolved = root / sym_dir
        if not resolved.exists():
            errors.append(f"worktree_setup.symlink_dirs: {sym_dir!r} does not exist (resolved to {resolved})")
    for copy_file in config.worktree_setup.copy_files:
        resolved = root / copy_file
        if not resolved.exists():
            errors.append(f"worktree_setup.copy_files: {copy_file!r} does not exist (resolved to {resolved})")


def _validate_formal_verification(config: BernsteinConfig, root: Path, errors: list[str]) -> None:
    if not config.formal_verification or not config.formal_verification.properties:
        return
    for prop in config.formal_verification.properties:
        if not prop.lemmas_file:
            continue
        resolved = root / prop.lemmas_file
        if not resolved.exists():
            errors.append(
                f"formal_verification.properties[{prop.name!r}].lemmas_file: "
                f"{prop.lemmas_file!r} does not exist (resolved to {resolved})"
            )


def _validate_remote_key(config: BernsteinConfig, errors: list[str]) -> None:
    if not config.remote or not config.remote.key:
        return
    key_path = Path(os.path.expanduser(config.remote.key))
    if not key_path.exists():
        errors.append(f"remote.key: {config.remote.key!r} does not exist (resolved to {key_path})")


def _validate_catalog_paths(config: BernsteinConfig, root: Path, errors: list[str]) -> None:
    if not config.catalogs:
        return
    for catalog in config.catalogs:
        if not catalog.path or not catalog.enabled:
            continue
        cat_path = Path(catalog.path)
        if not cat_path.is_absolute():
            cat_path = root / cat_path
        if not cat_path.exists():
            errors.append(f"catalogs[{catalog.name!r}].path: {catalog.path!r} does not exist (resolved to {cat_path})")


# ---------------------------------------------------------------------------
# Public API: load, validate, and optionally check paths
# ---------------------------------------------------------------------------


def load_and_validate(
    path: Path,
    *,
    check_paths: bool = False,
    expand_env: bool = True,
) -> BernsteinConfig:
    """Load, migrate, expand env vars, validate, and optionally check paths.

    This is the main entry point for config validation.

    Args:
        path: Path to bernstein.yaml.
        check_paths: If True, also validate that referenced files exist.
        expand_env: If True, expand ${VAR} references before validation.

    Returns:
        Validated BernsteinConfig.

    Raises:
        FileNotFoundError: If the config file does not exist.
        yaml.YAMLError: If the file is not valid YAML.
        ValueError: If migration fails.
        pydantic.ValidationError: If the config does not match the schema.
        ConfigPathError: If check_paths is True and paths are missing.
        EnvExpansionError: If env var expansion fails.
    """
    if not path.exists():
        raise FileNotFoundError(f"Config file not found: {path}")

    raw_text = path.read_text(encoding="utf-8")
    data_raw: object = yaml.safe_load(raw_text)

    if not isinstance(data_raw, dict):
        raise ValueError(f"Config file must be a YAML mapping, got {type(data_raw).__name__}")

    data: dict[str, Any] = cast("dict[str, Any]", data_raw)

    # CFG-004: Migrate if needed
    data = migrate_config(data)

    # CFG-003: Expand environment variables
    if expand_env:
        expanded = expand_env_recursive(data)
        if isinstance(expanded, dict):
            data = cast("dict[str, Any]", expanded)

    # CFG-001 + CFG-002: Validate with Pydantic
    config = BernsteinConfig.model_validate(data)

    # Issue #2356: gate merge-critical roles on a verified endpoint
    # certification receipt. Local profiles on low-stakes roles pass
    # without a receipt (best-effort by policy); a gated role requires a
    # signed receipt certifying that exact role for that exact endpoint.
    endpoint_errors = _validate_endpoint_certifications(config, project_root=path.parent)
    if endpoint_errors:
        raise ValueError(
            "Configuration failed the endpoint certification gate:\n" + "\n".join(f"  - {e}" for e in endpoint_errors)
        )

    # CFG-005: Check file paths
    if check_paths:
        path_errors = validate_file_paths(config, project_root=path.parent)
        if path_errors:
            raise ConfigPathError("Config references missing paths:\n" + "\n".join(f"  - {e}" for e in path_errors))

    return config


def _validate_endpoint_certifications(config: BernsteinConfig, *, project_root: Path) -> list[str]:
    """Collect certification-gate errors for local endpoint assignments."""
    policy = config.role_model_policy or {}
    assignments = [
        (role, entry.endpoint, entry.base_url or "", entry.model or "")
        for role, entry in policy.items()
        if entry.endpoint is not None
    ]
    if not assignments:
        return []
    # Imported lazily: the endpoints package pulls in signing/lineage
    # machinery most config loads never touch.
    from bernstein.core.endpoints.certification import validate_endpoint_assignments

    return validate_endpoint_assignments(
        [(role, name, base_url, model) for role, name, base_url, model in assignments],
        workdir=project_root,
    )


def export_json_schema(*, indent: int = 2) -> str:
    """Export the bernstein.yaml JSON Schema as a string.

    Args:
        indent: JSON indentation level.

    Returns:
        JSON string of the schema.
    """
    return json.dumps(BernsteinConfig.json_schema(), indent=indent)
