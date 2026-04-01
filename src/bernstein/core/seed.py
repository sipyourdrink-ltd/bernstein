"""Seed file parser for bernstein.yaml.

Reads the project seed configuration, validates it, and produces the
initial manager Task that kicks off orchestration.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, cast

import yaml

from bernstein.agents.catalog import CatalogRegistry
from bernstein.core.compliance import ComplianceConfig, CompliancePreset
from bernstein.core.formal_verification import FormalProperty, FormalVerificationConfig
from bernstein.core.gate_runner import VALID_GATE_NAMES, GatePipelineStep, normalize_gate_condition
from bernstein.core.key_rotation import KeyRotationConfig, _parse_interval
from bernstein.core.models import (
    BatchConfig,
    ClusterConfig,
    ClusterTopology,
    Complexity,
    Scope,
    SmtpConfig,
    Task,
    TaskStatus,
    TestAgentConfig,
)
from bernstein.core.quality_gates import QualityGatesConfig
from bernstein.core.secrets import SecretsConfig
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
    batch: BatchConfig = field(default_factory=BatchConfig)
    max_cost_per_agent: float = 0.0
    webhooks: tuple[WebhookConfig, ...] = ()
    test_agent: TestAgentConfig = field(default_factory=TestAgentConfig)
    smtp: SmtpConfig | None = None


_BUDGET_RE = re.compile(r"^\$(\d+(?:\.\d+)?)$")
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
    raise SeedError(f"{field_name} must be a list of strings, got: {type(raw).__name__}")


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
    workspace: Workspace | None = None
    if workspace_raw is not None:
        if not isinstance(workspace_raw, dict):
            raise SeedError(f"workspace must be a mapping, got: {type(workspace_raw).__name__}")
        workspace_dict: dict[str, Any] = cast("dict[str, Any]", workspace_raw)
        try:
            workspace = Workspace.from_config(workspace_dict, root=path.parent)
        except ValueError as exc:
            raise SeedError(f"Invalid workspace configuration: {exc}") from exc

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
        batch=batch,
        test_agent=test_agent,
        smtp=_parse_smtp(data.get("smtp")),
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
