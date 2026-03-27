"""Seed file parser for bernstein.yaml.

Reads the project seed configuration, validates it, and produces the
initial manager Task that kicks off orchestration.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path  # noqa: TC003 — used at runtime in parse_seed
from typing import Any, Literal, cast

import yaml

from bernstein.core.models import Complexity, Scope, Task, TaskStatus, TaskType, UpgradeProposalDetails, RiskAssessment, RollbackPlan


class SeedError(Exception):
    """Raised when the seed file is missing, unreadable, or invalid."""


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
        mcp_servers: MCP server definitions to pass to spawned agents.
    """

    goal: str
    budget_usd: float | None = None
    team: Literal["auto"] | list[str] = "auto"
    cli: Literal["claude", "codex", "gemini", "qwen"] = "claude"
    max_agents: int = 6
    model: str | None = None
    constraints: tuple[str, ...] = ()
    context_files: tuple[str, ...] = ()
    agent_catalog: str | None = None
    mcp_servers: dict[str, dict[str, Any]] | None = None


_BUDGET_RE = re.compile(r"^\$(\d+(?:\.\d+)?)$")
_VALID_CLIS = frozenset({"claude", "codex", "gemini", "qwen"})


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
    if isinstance(raw, str):
        m = _BUDGET_RE.match(raw.strip())
        if m:
            return float(m.group(1))
        # Try bare numeric string.
        try:
            return float(raw.strip())
        except ValueError:
            pass
        raise SeedError(f"Invalid budget format: {raw!r}. Expected '$N' or a number.")
    raise SeedError(f"Invalid budget type: {type(raw).__name__}")


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
        if len(raw) == 0:
            return "auto"
        if all(isinstance(r, str) for r in raw):
            return [str(r) for r in raw]
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
        if all(isinstance(s, str) for s in raw):
            return tuple(str(s) for s in raw)
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
        data: Any = yaml.safe_load(raw_text)
    except yaml.YAMLError as exc:
        raise SeedError(f"Invalid YAML in {path}: {exc}") from exc

    if not isinstance(data, dict):
        raise SeedError(f"Seed file must be a YAML mapping, got {type(data).__name__}")

    # --- Required fields ---
    goal = data.get("goal")
    if not goal or not isinstance(goal, str):
        raise SeedError("Seed file must contain a non-empty 'goal' string.")

    # --- Optional fields ---
    budget_usd = _parse_budget(data.get("budget"))
    team = _parse_team(data.get("team"))

    cli_raw = data.get("cli", "claude")
    if cli_raw not in _VALID_CLIS:
        raise SeedError(f"cli must be one of {sorted(_VALID_CLIS)}, got: {cli_raw!r}")
    cli = cast(Literal["claude", "codex", "gemini", "qwen"], cli_raw)

    max_agents_raw = data.get("max_agents", 6)
    if not isinstance(max_agents_raw, int) or max_agents_raw < 1:
        raise SeedError(f"max_agents must be a positive integer, got: {max_agents_raw!r}")

    model_raw = data.get("model")
    if model_raw is not None and not isinstance(model_raw, str):
        raise SeedError(f"model must be a string, got: {type(model_raw).__name__}")

    constraints = _parse_string_list(data.get("constraints"), "constraints")
    context_files = _parse_string_list(data.get("context_files"), "context_files")

    agent_catalog_raw = data.get("agent_catalog")
    if agent_catalog_raw is not None and not isinstance(agent_catalog_raw, str):
        raise SeedError(f"agent_catalog must be a string path, got: {type(agent_catalog_raw).__name__}")

    mcp_servers_raw = data.get("mcp_servers")
    if mcp_servers_raw is not None and not isinstance(mcp_servers_raw, dict):
        raise SeedError(f"mcp_servers must be a mapping, got: {type(mcp_servers_raw).__name__}")

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
        mcp_servers=mcp_servers_raw,
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
