"""Read-only MCP-tool allowlist for the bernstein.run docs bot.

The bernstein.run docs bot may
optionally consult the live cluster when answering operational questions
("how many tasks are open", "what's the cluster health"). To keep that
surface narrow we expose **only** the four read-only tools below; write
tools (``bernstein_run``, ``bernstein_approve``, ``bernstein_complete``,
``bernstein_stop``, ``bernstein_create_subtask``) must never reach the bot.

The allowlist lives in code - adding a tool to the bot surface requires a
review of *this file*, not a config flip. That's deliberate: misconfigured
MCP servers cannot grant the bot access to mutation tools by advertising
them, because the discovery endpoint filters through this set.

References:
    - .sdd/backlog/open/2026-05-08-bernstein-mcp-bot-tools-exposure.md
    - OWASP Top-10 for Agentic Apps (Dec 9 2025) - ASI02 Tool Misuse,
      ASI04 Unauthorized Tool Invocation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Final

# ---------------------------------------------------------------------------
# Allowlist
# ---------------------------------------------------------------------------

#: Read-only MCP tools the docs bot may invoke.  Anything not in this set is
#: rejected at the discovery boundary even if the MCP server advertises it.
ALLOWED_BOT_TOOLS: Final[frozenset[str]] = frozenset(
    {
        "bernstein_status",
        "bernstein_tasks",
        "bernstein_health",
        "bernstein_cost",
    }
)


# ---------------------------------------------------------------------------
# Tool specs (what we publish to the bot)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class BotToolSpec:
    """Public-safe description of a bot-callable MCP tool.

    Mirrors the subset of the MCP tool definition the docs bot needs to
    plan a single tool call. We deliberately omit the JSON-schema args
    surface for now - the four allowed tools take at most one optional
    string argument (``status`` on ``bernstein_tasks``), which the system
    prompt handles directly.

    Attributes:
        name: MCP tool name (e.g. ``"bernstein_status"``).
        summary: One-line human description for the bot system prompt.
        args_hint: Optional argument hint (``"status?: open|closed|failed"``).
    """

    name: str
    summary: str
    args_hint: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialise to the JSON shape the discovery endpoint returns."""
        payload: dict[str, Any] = {"name": self.name, "summary": self.summary}
        if self.args_hint is not None:
            payload["args_hint"] = self.args_hint
        return payload


#: Canonical descriptions of the four bot-callable tools.  Kept here (not in
#: ``mcp/server.py``) so that adding the same tool to the MCP catalog later
#: cannot accidentally widen the bot surface.
BOT_TOOL_SPECS: Final[tuple[BotToolSpec, ...]] = (
    BotToolSpec(
        name="bernstein_status",
        summary=(
            "Cluster summary: orchestrator state, task counts, agent counts. "
            "Call when the user asks about live progress."
        ),
    ),
    BotToolSpec(
        name="bernstein_tasks",
        summary="List tasks. Call when the user asks 'how many tasks are X' or 'show me tasks'.",
        args_hint='status?: "open"|"closed"|"failed"',
    ),
    BotToolSpec(
        name="bernstein_health",
        summary="Health check breakdown. Call when the user asks 'is bernstein up' or 'what's broken'.",
    ),
    BotToolSpec(
        name="bernstein_cost",
        summary="Spend summary. Call when the user asks about cost or budget.",
    ),
)


def filter_to_allowed(tool_names: list[str] | tuple[str, ...]) -> list[BotToolSpec]:
    """Return ``BotToolSpec``s for every input name that's on the allowlist.

    Used by the discovery endpoint and by clients that want to defensively
    re-filter an MCP-server response before handing it to the bot. The
    function is total: unknown names are silently dropped (no exception),
    keeping the discovery path fail-open.

    Args:
        tool_names: Names returned by the MCP server's ``list_tools`` call.

    Returns:
        Subset of ``BOT_TOOL_SPECS`` whose names appear in *tool_names* and
        in :data:`ALLOWED_BOT_TOOLS`. Order matches :data:`BOT_TOOL_SPECS`.
    """
    requested = set(tool_names)
    return [spec for spec in BOT_TOOL_SPECS if spec.name in ALLOWED_BOT_TOOLS and spec.name in requested]


def all_allowed_specs() -> list[BotToolSpec]:
    """Return every spec on the allowlist (used by the in-process discovery path)."""
    return [spec for spec in BOT_TOOL_SPECS if spec.name in ALLOWED_BOT_TOOLS]


__all__ = [
    "ALLOWED_BOT_TOOLS",
    "BOT_TOOL_SPECS",
    "BotToolSpec",
    "all_allowed_specs",
    "filter_to_allowed",
]
