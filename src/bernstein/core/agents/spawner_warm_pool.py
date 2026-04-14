"""Warm pool routing and tool allowlist helpers for spawner."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any, cast

from bernstein.core.models import ModelConfig, Task

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


def _should_use_router(
    role_policy: dict[str, str],
    adapter_name: str,
    has_router: bool,
) -> bool:
    """Decide whether the tier-aware router should select the model.

    The router's internal model arms (haiku/sonnet/opus) and the cascade/bandit
    systems are Claude-specific.  When the operator has configured an explicit
    ``cli`` + ``model`` in ``role_model_policy``, or the active adapter is not
    Claude-compatible, the router cannot produce a meaningful selection and must
    be bypassed.

    Precedence logic:
        1. No router registered or no providers -> skip (nothing to route with).
        2. Operator pinned both ``cli`` and ``model`` -> skip (explicit config
           takes absolute priority; the operator knows best).
        3. Active adapter is not Claude-compatible -> skip (router arms are
           meaningless for qwen, gemini, codex, etc.).
        4. Otherwise -> use the router.

    Args:
        role_policy: The ``role_model_policy`` entry for the current role
            (may be empty dict).
        adapter_name: The adapter name from ``adapter.name()``.
        has_router: Whether a TierAwareRouter is configured with providers.

    Returns:
        ``True`` when the router should be consulted.
    """
    if not has_router:
        return False

    # Operator explicitly pinned adapter + model -> router must not override.
    # The seed parser maps ``cli`` -> ``provider``, so check both keys.
    pinned_adapter = role_policy.get("cli") or role_policy.get("provider")
    if pinned_adapter and role_policy.get("model"):
        return False

    # The router's arms are Claude-specific; non-Claude adapters get nonsense.
    from bernstein.core.bandit_router import BanditRouter

    effective_adapter = pinned_adapter or adapter_name
    return BanditRouter.router_applicable(effective_adapter)


def _load_role_config(role: str, templates_dir: Path) -> ModelConfig | None:
    """Load ModelConfig from a role's config.yaml if present.

    Args:
        role: Role name (e.g. "backend", "manager").
        templates_dir: Root of templates/roles/ directory.

    Returns:
        ModelConfig from config.yaml, or None if not found / unreadable.
    """
    from bernstein.core.agents.spawner_core import _read_cached

    config_path = templates_dir / role / "config.yaml"
    if not config_path.exists():
        return None
    try:
        import yaml

        raw_data: object = yaml.safe_load(_read_cached(config_path))
        if not isinstance(raw_data, dict):
            return None
        data: dict[str, Any] = cast("dict[str, Any]", raw_data)
        model = str(data.get("default_model", "sonnet"))
        effort = str(data.get("default_effort", "high"))
        return ModelConfig(model=model, effort=effort)
    except Exception as exc:
        logger.warning("Failed to load role config for '%s': %s", role, exc)
        return None


def _select_batch_config(
    tasks: list[Task],
    templates_dir: Path | None = None,
    metrics_dir: Path | None = None,
    workdir: Path | None = None,
) -> ModelConfig:
    """Pick the highest-tier model config across all tasks in a batch.

    If *templates_dir* is provided, reads the role's config.yaml first and
    uses that as the baseline before falling back to heuristic routing.
    If *metrics_dir* is provided, consults the epsilon-greedy bandit for
    non-high-stakes roles to dynamically pick the cheapest viable model.
    When *workdir* is also provided, effectiveness history seeds the bandit
    so both learning systems share data.
    Routes each task individually, then picks the most capable config
    so the agent can handle the hardest task in its batch.

    Args:
        tasks: Non-empty list of tasks.
        templates_dir: Optional path to templates/roles/ for config.yaml lookup.
        metrics_dir: Optional path to .sdd/metrics for bandit state.
        workdir: Optional project root for effectiveness scorer data.

    Returns:
        ModelConfig suitable for the entire batch.
    """
    # If a role-level config.yaml exists, use it as the baseline
    role = tasks[0].role
    if templates_dir is not None:
        role_config = _load_role_config(role, templates_dir)
        if role_config is not None:
            return role_config

    from bernstein.core.models import Complexity, Scope
    from bernstein.core.router import route_task

    _HIGH_STAKES_ROLES = frozenset({"manager", "architect", "security"})

    def _route_for_batch(task: Task) -> ModelConfig:
        """Batch-specific routing: consult bandit when available, else heuristics."""
        if task.model or task.effort:
            return ModelConfig(model=task.model or "sonnet", effort=task.effort or "normal")
        if task.role in _HIGH_STAKES_ROLES or task.scope == Scope.LARGE or task.priority == 1:
            return ModelConfig(model="opus", effort="max")
        if task.complexity == Complexity.HIGH:
            return ModelConfig(model="opus", effort="high")
        return route_task(task, bandit_metrics_dir=metrics_dir, workdir=workdir)

    configs = [_route_for_batch(t) for t in tasks]
    # Sort by model tier (opus > sonnet > haiku) then effort (max > high > normal)
    model_rank = {"opus": 3, "sonnet": 2, "haiku": 1}
    effort_rank = {"max": 3, "high": 2, "normal": 1}
    return max(
        configs,
        key=lambda c: (model_rank.get(c.model, 0), effort_rank.get(c.effort, 0)),
    )


# ---------------------------------------------------------------------------
# Worker tool allowlist per spawn (T578)
# ---------------------------------------------------------------------------

_TOOL_ALLOWLIST_ENV_VAR = "BERNSTEIN_TOOL_ALLOWLIST"


def build_tool_allowlist_env(allowed_tools: list[str]) -> dict[str, str]:
    """Build environment variables encoding a tool allowlist for a spawned agent (T578).

    The allowlist is passed via the ``BERNSTEIN_TOOL_ALLOWLIST`` environment
    variable as a comma-separated list.  The worker wrapper reads this and
    enforces it before dispatching tool calls.

    Args:
        allowed_tools: List of tool names the agent is permitted to invoke.

    Returns:
        Dict of environment variables to merge into the spawn environment.
    """
    if not allowed_tools:
        return {}
    return {_TOOL_ALLOWLIST_ENV_VAR: ",".join(allowed_tools)}


def parse_tool_allowlist_env() -> list[str] | None:
    """Parse the tool allowlist from the current process environment (T578).

    Returns:
        List of allowed tool names, or None if no allowlist is configured.
    """
    raw = os.environ.get(_TOOL_ALLOWLIST_ENV_VAR, "").strip()
    if not raw:
        return None
    return [t.strip() for t in raw.split(",") if t.strip()]


def check_tool_allowed(tool_name: str, allowlist: list[str] | None) -> bool:
    """Return True if *tool_name* is permitted by *allowlist* (T578).

    Args:
        tool_name: Tool to check.
        allowlist: Permitted tools, or None to allow all.

    Returns:
        True if the tool is allowed.
    """
    if allowlist is None:
        return True
    return tool_name in allowlist
