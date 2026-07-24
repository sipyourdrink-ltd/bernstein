"""Cloudflare Agents SDK adapter (experimental, currently non-functional).

This adapter is a stub: it can only start a local ``npx wrangler dev`` server,
which is a long-running dev server with no path to trigger the worker or
collect a result.  Every task routed here would run until the timeout watchdog
kills it, producing no artifact (issue #2782).  Rather than pretend, ``spawn()``
refuses immediately with an actionable error.

To run agents on Cloudflare today, deploy a worker that implements the
``/agents/*`` HTTP contract and drive it with
``bernstein.bridges.cloudflare.CloudflareBridge``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ModelConfig

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult

logger = logging.getLogger(__name__)

_UNAVAILABLE_MSG = (
    "Cloudflare Agents adapter is experimental and currently non-functional: "
    "it can only start a local `npx wrangler dev` server, which never triggers "
    "the worker or returns a result, so every task would time out with no "
    "artifact (issue #2782). Run agents with a local adapter (e.g. `claude`, "
    "`codex`, `aider`, or `mock`) instead, or deploy a worker implementing the "
    "`/agents/*` contract and drive it via "
    "`bernstein.bridges.cloudflare.CloudflareBridge`."
)


class CloudflareAgentsAdapter(CLIAdapter):
    """Experimental Cloudflare Agents adapter that refuses to spawn.

    The adapter is registered so the ``cloudflare`` CLI selection still
    resolves, but ``spawn()`` raises a clear error instead of launching a
    ``npx wrangler dev`` server that can never complete a task (issue #2782).
    """

    external_endpoints = (("api.cloudflare.com", 443),)

    def spawn(
        self,
        *,
        prompt: str,
        workdir: Path,
        model_config: ModelConfig,
        session_id: str,
        mcp_config: dict[str, Any] | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        task_scope: str = "medium",
        budget_multiplier: float = 1.0,
        system_addendum: str = "",
        multimodal_context: Any | None = None,
    ) -> SpawnResult:
        """Refuse to spawn: this adapter has no worker-trigger path.

        Args:
            prompt: Task prompt for the agent (unused).
            workdir: Working directory for the agent process (unused).
            model_config: Model and effort configuration (unused).
            session_id: Unique session identifier (unused).
            mcp_config: Optional MCP server definitions (unused).
            timeout_seconds: Process timeout in seconds (unused).
            task_scope: Task scope for budget caps (unused).
            budget_multiplier: Retry budget multiplier (unused).
            system_addendum: System-prompt instructions to inject (unused).
            multimodal_context: Optional multimodal attachments.

        Raises:
            CapabilityRefusal: When multimodal attachments are supplied.
            RuntimeError: Always, because the adapter is non-functional.
        """
        # Preserve the multimodal-refusal contract shared by every adapter
        # before reporting that the adapter itself is unavailable.
        self.refuse_multimodal_if_needed(multimodal_context)
        raise RuntimeError(_UNAVAILABLE_MSG)

    def name(self) -> str:
        """Return the human-readable adapter name."""
        return "Cloudflare Agents"
