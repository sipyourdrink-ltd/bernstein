"""Tiered MCP tool exposure with an explicit context-budget knob.

Exposing the full MCP tool catalogue to every adapter costs context tokens
on every turn whether or not the tools are used. Operators running
cost-sensitive tasks or smaller-context adapters need a single knob to trade
capability for context budget.

Three named tiers cover the realistic axis without inventing a per-tool
policy language:

  * ``core``     - the small always-on subset (health + the read-only query
                   tools needed to drive and observe a run).
  * ``standard`` - the typical session (core plus the common mutation and
                   skill tools). This is the default.
  * ``all``      - everything Bernstein ships, including the scenario bridge
                   and any optional tools.

Tiers are cumulative: ``core`` is a subset of ``standard`` which is a subset
of ``all``. Selecting a tier advertises that tier's tools and *only* that
tier's tools in the ``tools/list`` response; out-of-tier tools are neither
advertised nor callable.

The tier annotation is a property of the tool registration, expressed once
in :data:`TOOL_TIERS` keyed by the tool's advertised name. Adding a new tool
means adding one entry here with its declared tier; there is no separate
runtime registry to keep in sync.
"""

from __future__ import annotations

import os
from typing import Final, Literal, get_args

#: The three named tiers, ordered from smallest to largest budget.
ToolTier = Literal["core", "standard", "all"]

#: Ordered tuple of every valid tier name, smallest first.
TIER_ORDER: Final[tuple[ToolTier, ...]] = get_args(ToolTier)

#: Default tier when no env var or session flag is set.
DEFAULT_TIER: Final[ToolTier] = "standard"

#: Env var that selects the active tier (acceptance criterion 2).
TIER_ENV_VAR: Final[str] = "BERNSTEIN_MCP_TOOL_TIER"

#: The declared minimum tier for every shipped MCP tool, keyed by the name
#: advertised over MCP. A tool with tier ``core`` is exposed in all tiers; a
#: tool with tier ``all`` is exposed only when the active tier is ``all``.
#:
#: This mapping is the single source of truth for the annotation. Adding a
#: new tool means adding one line here at declaration time. A missing entry
#: is not a no-op: the tool falls back to ``all`` and disappears from
#: ``tools/list`` under the default ``standard`` tier. A coverage test
#: (``tests/unit/test_mcp_server.py``) compares the registered tools against
#: this mapping so an omission fails instead of silently hiding a tool.
TOOL_TIERS: Final[dict[str, ToolTier]] = {
    # core - always on: liveness plus the read-only surface needed to start
    # and observe a run.
    "bernstein_health": "core",
    "bernstein_run": "core",
    "bernstein_status": "core",
    "bernstein_tasks": "core",
    "bernstein_task_handle": "core",
    # standard - the typical session: cost, mutation, and skill tools.
    "bernstein_cost": "standard",
    "bernstein_stop": "standard",
    "bernstein_approve": "standard",
    "bernstein_create_subtask": "standard",
    "bernstein_claim": "standard",
    "bernstein_update": "standard",
    "bernstein_post_artifact": "standard",
    "bernstein_context": "standard",
    "load_skill": "standard",
    # all - power-user surface: the scenario bridge and lineage verifier.
    "bernstein_scenarios": "all",
    "bernstein_scenario": "all",
    "bernstein_scenario_status": "all",
    "verify_chain": "all",
}


def tier_rank(tier: ToolTier) -> int:
    """Return the cumulative rank of ``tier`` (``core`` = 0, ``all`` = 2).

    A tool is exposed under an active tier when the tool's declared rank is
    less than or equal to the active tier's rank.
    """
    return TIER_ORDER.index(tier)


def normalize_tier(value: str | None) -> ToolTier:
    """Coerce a raw string into a valid :data:`ToolTier`.

    Args:
        value: Raw tier string (env var or CLI flag), or ``None``.

    Returns:
        The matching tier, or :data:`DEFAULT_TIER` when ``value`` is empty.

    Raises:
        ValueError: When ``value`` is a non-empty string that is not one of
            the named tiers.
    """
    if value is None:
        return DEFAULT_TIER
    candidate = value.strip().lower()
    if not candidate:
        return DEFAULT_TIER
    if candidate not in TIER_ORDER:
        valid = ", ".join(TIER_ORDER)
        raise ValueError(f"Unknown MCP tool tier {value!r}. Valid tiers: {valid}.")
    # candidate is now known to be a member of TIER_ORDER.
    return candidate  # type: ignore[return-value]


def resolve_active_tier(override: str | None = None) -> ToolTier:
    """Resolve the active tier from an explicit override or the environment.

    Resolution order (first wins):

      1. ``override`` - the ``--mcp-tier`` session flag, when provided.
      2. ``BERNSTEIN_MCP_TOOL_TIER`` env var.
      3. :data:`DEFAULT_TIER`.

    Args:
        override: Optional explicit tier (e.g. from a CLI flag).

    Returns:
        The resolved, validated tier.
    """
    if override is not None and override.strip():
        return normalize_tier(override)
    return normalize_tier(os.environ.get(TIER_ENV_VAR))


def tool_in_tier(tool_name: str, active_tier: ToolTier) -> bool:
    """Return whether ``tool_name`` is advertised under ``active_tier``.

    A tool whose name is not present in :data:`TOOL_TIERS` is treated as
    ``all`` tier: it is only exposed at the widest budget so an unannotated
    tool never silently leaks into a smaller tier.

    Args:
        tool_name: The MCP-advertised tool name.
        active_tier: The currently selected tier.

    Returns:
        ``True`` when the tool should be advertised and callable.
    """
    declared = TOOL_TIERS.get(tool_name, "all")
    return tier_rank(declared) <= tier_rank(active_tier)


def tools_for_tier(active_tier: ToolTier) -> list[str]:
    """Return the sorted names of every tool advertised under ``active_tier``.

    Args:
        active_tier: The tier to enumerate.

    Returns:
        Sorted list of advertised tool names.
    """
    return sorted(name for name in TOOL_TIERS if tool_in_tier(name, active_tier))


def tier_audit() -> dict[ToolTier, list[str]]:
    """Return a per-tier map of advertised tool names for auditing.

    Powers ``bernstein mcp tools`` so operators can see what each tier would
    advertise before switching.
    """
    return {tier: tools_for_tier(tier) for tier in TIER_ORDER}
