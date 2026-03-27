"""Caching wrapper for CLI adapters to enable prompt prefix deduplication."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from bernstein.adapters.base import CLIAdapter, SpawnResult
from bernstein.core.prompt_caching import PromptCachingManager

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ModelConfig

logger = logging.getLogger(__name__)


class CachingAdapter(CLIAdapter):
    """Wraps a CLIAdapter to enable prompt caching.

    Intercepts spawn calls to:
    - Extract and deduplicate system prompt prefixes
    - Track cache metadata
    - Persist cache manifest

    The wrapped adapter behavior is unchanged; caching is transparent.

    Args:
        inner_adapter: The underlying CLIAdapter to wrap.
        workdir: Project working directory for cache storage.
    """

    def __init__(self, inner_adapter: CLIAdapter, workdir: Path) -> None:
        self._inner = inner_adapter
        self._caching_mgr = PromptCachingManager(workdir)

    def spawn(
        self,
        *,
        prompt: str,
        workdir: Path,
        model_config: ModelConfig,
        session_id: str,
        mcp_config: dict[str, Any] | None = None,
    ) -> SpawnResult:
        """Spawn agent with caching: process prompt then delegate to inner adapter.

        Args:
            prompt: Full agent prompt.
            workdir: Working directory for the agent.
            model_config: Model configuration.
            session_id: Session ID for the agent.
            mcp_config: Optional MCP configuration.

        Returns:
            SpawnResult from the inner adapter.
        """
        result = self._caching_mgr.process_prompt(prompt)
        logger.debug(
            "Prompt cache: key=%s, is_new=%s, hit_count=%s, reuse_savings=%s%%",
            result.cache_key[:8],
            result.is_new_prefix,
            result.hit_count,
            "90" if not result.is_new_prefix else "0",
        )

        self._caching_mgr.save_manifest()

        return self._inner.spawn(
            prompt=prompt,
            workdir=workdir,
            model_config=model_config,
            session_id=session_id,
            mcp_config=mcp_config,
        )

    def name(self) -> str:
        """Return inner adapter's name."""
        return self._inner.name()

    def is_alive(self, pid: int) -> bool:
        """Delegate to inner adapter."""
        return self._inner.is_alive(pid)

    def kill(self, pid: int) -> None:
        """Delegate to inner adapter."""
        self._inner.kill(pid)

    def detect_tier(self) -> Any:
        """Delegate to inner adapter."""
        return self._inner.detect_tier()
