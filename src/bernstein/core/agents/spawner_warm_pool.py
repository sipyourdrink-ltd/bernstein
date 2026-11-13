"""Warm pool routing and tool allowlist helpers for spawner."""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Any, cast

from bernstein.core.agents.spawn_errors import ModelNotConfiguredError
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


_CLAUDE_TIER_MODELS = frozenset({"opus", "sonnet", "haiku"})


def _coerce_model_for_non_claude_adapter(
    model_config: ModelConfig,
    *,
    adapter_name: str,
    adapter_default_model: str | None,
) -> ModelConfig:
    """Replace an unpinned Claude tier name with the adapter's default model.

    The batch/heuristic selectors emit Claude cascade tier names (opus/sonnet/
    haiku) with no adapter awareness. Handing one to a non-Claude adapter (e.g.
    Codex) produces ``codex exec -m opus``, which the CLI rejects, and records a
    model in the manifest that never actually ran. This normalises the selected
    model so the recorded and executed model agree.

    Returns the input unchanged for Claude-compatible adapters (byte-identical)
    or for models that are not Claude tiers. Raises
    :class:`ModelNotConfiguredError` when the model IS an unpinned Claude tier
    name being sent to a non-Claude adapter and no adapter default is
    configured - spawning would otherwise send e.g. ``codex exec -m opus``,
    which the CLI rejects, or silently bill a Claude-tier-shaped call to a
    provider that has no such tier. Callers must only invoke this when the
    operator did not pin a model (no per-task ``model`` and no
    ``role_model_policy`` model).
    """
    from bernstein.core.bandit_router import BanditRouter

    if BanditRouter.router_applicable(adapter_name):
        return model_config
    if model_config.model not in _CLAUDE_TIER_MODELS:
        return model_config
    if not adapter_default_model:
        raise ModelNotConfiguredError(
            f"Model '{model_config.model}' is an unpinned Claude tier name but adapter "
            f"'{adapter_name}' is not Claude-compatible and has no default_model configured. "
            "Refusing to spawn with a nonsensical model - configure role_model_policy or an "
            "adapter default.",
        )
    return ModelConfig(
        model=adapter_default_model,
        effort=model_config.effort,
        max_tokens=model_config.max_tokens,
        is_batch=model_config.is_batch,
    )


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
        model = data.get("default_model")
        if model is None:
            logger.debug(
                "Role config for '%s' has no default_model - skipping role-config routing",
                role,
            )
            return None
        effort = str(data.get("default_effort", "high"))
        return ModelConfig(model=str(model), effort=effort)
    except Exception as exc:
        logger.warning("Failed to load role config for '%s': %s", role, exc)
        return None


def _select_batch_config(
    tasks: list[Task],
    templates_dir: Path | None = None,
    metrics_dir: Path | None = None,
    workdir: Path | None = None,
    default_model: str | None = None,
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
        default_model: Optional model name from the seed config, used as the
            primary fallback ahead of the hardcoded Claude tier names.

    Returns:
        ModelConfig suitable for the entire batch.
    """
    # Per-task model/effort declared on the plan step take absolute precedence
    # over the role's default config.yaml.  Otherwise a plan that says
    # ``model: haiku`` for one step and ``model: flash`` for another would
    # silently collapse onto the role's default (e.g. qa/config.yaml ->
    # sonnet), defeating per-step routing for plan-driven runs.
    role = tasks[0].role
    if any(t.model or t.effort for t in tasks):
        # Fall through to the per-task router below; do NOT short-circuit
        # to the role's config.yaml.
        pass
    elif templates_dir is not None:
        role_config = _load_role_config(role, templates_dir)
        if role_config is not None:
            return role_config

    from bernstein.core.models import Complexity, Scope
    from bernstein.core.router import route_task

    _HIGH_STAKES_ROLES = frozenset({"manager", "architect", "security"})

    def _route_for_batch(task: Task) -> ModelConfig:
        """Batch-specific routing: consult bandit when available, else heuristics."""
        if task.model or task.effort:
            model = task.model or default_model
            if model is None:
                raise ModelNotConfiguredError(
                    f"Task {task.id} has effort={task.effort!r} but no model, and no "
                    "default_model is configured. Refusing to guess a model.",
                )
            return ModelConfig(model=model, effort=task.effort or "normal")
        if task.role in _HIGH_STAKES_ROLES or task.scope == Scope.LARGE or task.priority == 1:
            if default_model is None:
                raise ModelNotConfiguredError(
                    f"Task {task.id} (role={task.role}) is high-stakes but no default_model "
                    "is configured. Refusing to guess a model.",
                )
            return ModelConfig(model=default_model, effort="max")
        if task.complexity == Complexity.HIGH:
            if default_model is None:
                raise ModelNotConfiguredError(
                    f"Task {task.id} has HIGH complexity but no default_model is configured. "
                    "Refusing to guess a model.",
                )
            return ModelConfig(model=default_model, effort="high")
        return route_task(
            task,
            bandit_metrics_dir=metrics_dir,
            workdir=workdir,
            default_model=default_model,
        )

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
