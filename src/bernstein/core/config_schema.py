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
_ENV_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-(.*?))?\}")

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


class RoleModelPolicyEntry(BaseModel):
    """Per-role model/provider policy."""

    model_config = ConfigDict(extra="forbid")

    provider: str | None = None
    model: str | None = None
    effort: str | None = None


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


class SessionSchema(BaseModel):
    """Session resume configuration."""

    model_config = ConfigDict(extra="forbid")

    resume: bool = True
    stale_after_minutes: int = Field(default=30, ge=1)


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
    properties: list[FormalPropertySchema] = Field(default_factory=lambda: [])


class BatchSchema(BaseModel):
    """Batch mode configuration."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    eligible: list[str] = Field(default_factory=list)


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


class ModelFallbackSchema(BaseModel):
    """Model fallback chain configuration (AGENT-004).

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
    internal_llm_provider: str = Field(
        default="openrouter_free",
        description="LLM provider for manager reviews and planning.",
    )
    internal_llm_model: str = Field(
        default="nvidia/nemotron-3-super-120b-a12b",
        description="Model for internal LLM calls.",
    )

    # --- Constraints and context ---
    constraints: list[str] = Field(default_factory=list)
    context_files: list[str] = Field(default_factory=list)

    # --- Nested configs ---
    quality_gates: QualityGatesSchema | None = None
    role_model_policy: dict[str, RoleModelPolicyEntry] | None = None
    role_config: dict[str, RoleConfigEntry] | None = None
    model_policy: ModelPolicySchema | None = None
    worktree_setup: WorktreeSetupSchema | None = None
    notify: NotifyConfigSchema | None = None
    storage: StorageSchema | None = None
    session: SessionSchema | None = None
    cluster: ClusterSchema | None = None
    remote: RemoteSchema | None = None
    agency: AgencySchema | None = None
    catalogs: list[CatalogEntry] | None = None
    formal_verification: FormalVerificationSchema | None = None
    batch: BatchSchema | None = None
    test_agent: TestAgentSchema | None = None
    smtp: SmtpSchema | None = None
    mcp_servers: dict[str, Any] | None = None
    model_fallback: ModelFallbackSchema | None = None

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

        # auto_decompose requires an LLM provider
        if self.auto_decompose and self.internal_llm_provider in ("none", ""):
            errors.append(
                "auto_decompose is enabled but internal_llm_provider is "
                f"{self.internal_llm_provider!r}. Decomposition needs an LLM. "
                "Either set a valid provider or disable auto_decompose."
            )

        # evolution_enabled requires an LLM provider
        if self.evolution_enabled and self.internal_llm_provider in ("none", ""):
            errors.append(
                "evolution_enabled is true but internal_llm_provider is "
                f"{self.internal_llm_provider!r}. Evolution needs an LLM. "
                "Either set a valid provider or disable evolution."
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

        if errors:
            raise ValueError("Configuration has conflicting settings:\n" + "\n".join(f"  - {e}" for e in errors))

        return self

    def _parse_budget_value(self) -> float | None:
        """Parse the budget field into a numeric value."""
        raw = self.budget
        if raw is None:
            return None
        if isinstance(raw, (int, float)):
            return float(raw)
        # raw is str at this point
        s = str(raw).strip()
        if s.startswith("$"):
            s = s[1:]
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

    result = dict(data)
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

    # context_files
    for ctx_file in config.context_files:
        resolved = project_root / ctx_file
        if not resolved.exists():
            errors.append(f"context_files: path {ctx_file!r} does not exist (resolved to {resolved})")

    # agency.path
    if config.agency and config.agency.path:
        agency_path = Path(config.agency.path)
        if not agency_path.is_absolute():
            agency_path = project_root / agency_path
        if not agency_path.exists():
            errors.append(f"agency.path: {config.agency.path!r} does not exist (resolved to {agency_path})")

    # worktree_setup.symlink_dirs and copy_files
    if config.worktree_setup:
        for sym_dir in config.worktree_setup.symlink_dirs:
            resolved = project_root / sym_dir
            if not resolved.exists():
                errors.append(f"worktree_setup.symlink_dirs: {sym_dir!r} does not exist (resolved to {resolved})")
        for copy_file in config.worktree_setup.copy_files:
            resolved = project_root / copy_file
            if not resolved.exists():
                errors.append(f"worktree_setup.copy_files: {copy_file!r} does not exist (resolved to {resolved})")

    # formal_verification lemmas files
    if config.formal_verification and config.formal_verification.properties:
        for prop in config.formal_verification.properties:
            if prop.lemmas_file:
                resolved = project_root / prop.lemmas_file
                if not resolved.exists():
                    errors.append(
                        f"formal_verification.properties[{prop.name!r}].lemmas_file: "
                        f"{prop.lemmas_file!r} does not exist "
                        f"(resolved to {resolved})"
                    )

    # remote.key
    if config.remote and config.remote.key:
        key_path = Path(os.path.expanduser(config.remote.key))
        if not key_path.exists():
            errors.append(f"remote.key: {config.remote.key!r} does not exist (resolved to {key_path})")

    # catalogs with local paths
    if config.catalogs:
        for catalog in config.catalogs:
            if catalog.path and catalog.enabled:
                cat_path = Path(catalog.path)
                if not cat_path.is_absolute():
                    cat_path = project_root / cat_path
                if not cat_path.exists():
                    errors.append(
                        f"catalogs[{catalog.name!r}].path: {catalog.path!r} does not exist (resolved to {cat_path})"
                    )

    return errors


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

    # CFG-005: Check file paths
    if check_paths:
        path_errors = validate_file_paths(config, project_root=path.parent)
        if path_errors:
            raise ConfigPathError("Config references missing paths:\n" + "\n".join(f"  - {e}" for e in path_errors))

    return config


def export_json_schema(*, indent: int = 2) -> str:
    """Export the bernstein.yaml JSON Schema as a string.

    Args:
        indent: JSON indentation level.

    Returns:
        JSON string of the schema.
    """
    return json.dumps(BernsteinConfig.json_schema(), indent=indent)
