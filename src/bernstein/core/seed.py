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
from bernstein.core.models import ClusterConfig, ClusterTopology, Complexity, Scope, Task, TaskStatus
from bernstein.core.quality_gates import QualityGatesConfig
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
    """

    webhook_url: str | None = None
    on_complete: bool = True
    on_failure: bool = True


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


_BUDGET_RE = re.compile(r"^\$(\d+(?:\.\d+)?)$")
_VALID_CLIS = frozenset({"claude", "codex", "gemini", "qwen", "auto"})


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

    constraints = _parse_string_list(data.get("constraints"), "constraints")
    context_files = _parse_string_list(data.get("context_files"), "context_files")

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
        if not isinstance(on_complete, bool):
            raise SeedError(f"notify.on_complete must be a bool, got: {type(on_complete).__name__}")
        if not isinstance(on_failure, bool):
            raise SeedError(f"notify.on_failure must be a bool, got: {type(on_failure).__name__}")
        notify = NotifyConfig(
            webhook_url=webhook_url,
            on_complete=on_complete,
            on_failure=on_failure,
        )

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

        quality_gates = QualityGatesConfig(
            enabled=_qg_bool("enabled", True),
            lint=_qg_bool("lint", True),
            lint_command=_qg_str("lint_command", "ruff check ."),
            type_check=_qg_bool("type_check", False),
            type_check_command=_qg_str("type_check_command", "pyright"),
            tests=_qg_bool("tests", False),
            test_command=_qg_str("test_command", "uv run python scripts/run_tests.py -x"),
            timeout_s=_qg_int("timeout_s", 120),
        )

    return SeedConfig(
        goal=goal,
        budget_usd=budget_usd,
        team=team,
        cli=cli,
        max_agents=max_agents_raw,
        model=model_raw,
        constraints=constraints,
        context_files=context_files,
        agent_catalog=agent_catalog_raw,
        catalogs=catalogs,
        mcp_servers=cast("dict[str, dict[str, Any]] | None", mcp_servers_raw),
        notify=notify,
        storage=storage,
        cells=cells_raw,
        cluster=cluster,
        workspace=workspace,
        session=session_cfg,
        worktree_setup=worktree_setup,
        quality_gates=quality_gates,
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
