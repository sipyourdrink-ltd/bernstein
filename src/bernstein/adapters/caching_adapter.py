"""Caching wrapper for CLI adapters to enable prompt prefix deduplication and response reuse."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult
from bernstein.core.prompt_caching import (
    CacheBreakEvent,
    CacheBreakReason,
    PromptCachingManager,
)
from bernstein.core.semantic_cache import ResponseCacheManager

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.models import ModelConfig

logger = logging.getLogger(__name__)


class CachingAdapter(CLIAdapter):
    """Wraps a CLIAdapter to enable prompt caching and response reuse.

    Intercepts spawn calls to:
    - Extract and deduplicate system prompt prefixes
    - Track cache break events
    - Skip spawn if a verified response hit is found (Cosine >= 0.95)

    Args:
        inner_adapter: The underlying CLIAdapter to wrap.
        workdir: Project working directory for cache storage.
        ttl: Time-to-live for response cache entries in seconds.
    """

    def __init__(self, inner_adapter: CLIAdapter, workdir: Path, ttl_seconds: int = 3600) -> None:
        self._inner = inner_adapter
        self._caching_mgr = PromptCachingManager(workdir)
        self._cache_break_path = workdir / ".sdd" / "metrics" / "cache_breaks.jsonl"
        self._response_cache = ResponseCacheManager(workdir, ttl_seconds=float(ttl_seconds))

    def _record_cache_break(self, event: CacheBreakEvent) -> None:
        """Append a cache break event to the JSONL file.

        Args:
            event: The cache break event to record.
        """
        self._cache_break_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._cache_break_path, "a") as f:
            f.write(event.to_json_line() + "\n")
        logger.info(
            "Cache break: reason=%s, key=%s, delta_tokens=%s",
            event.reason.value,
            event.new_cache_key[:8],
            event.estimated_token_delta,
        )

    def spawn(
        self,
        *,
        prompt: str,
        workdir: Path,
        model_config: ModelConfig,
        session_id: str,
        mcp_config: dict[str, Any] | None = None,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
    ) -> SpawnResult:
        """Spawn agent with caching: process prompt, check response cache, then delegate.

        Args:
            prompt: Full agent prompt.
            workdir: Working directory for the agent.
            model_config: Model configuration.
            session_id: Session ID for the agent.
            mcp_config: Optional MCP configuration.
            timeout_seconds: Timeout before killing the agent process.

        Returns:
            SpawnResult (pid=0 if cache hit, otherwise from the inner adapter).
        """
        # 1. Prompt prefix caching (Anthropic-style) — pass session_id for per-agent tracking
        cache_res = self._caching_mgr.process_prompt(prompt, session_id=session_id)

        # 2. Emit cache break event when prefix is new
        if cache_res.is_new_prefix and not cache_res.expected_drop_reason:
            event = CacheBreakEvent(
                timestamp=time.time(),
                reason=CacheBreakReason.SYSTEM,
                old_cache_key=None,
                new_cache_key=cache_res.cache_key,
                estimated_token_delta=cache_res.prefix_tokens,
                session_id=session_id,
                model_name=getattr(model_config, "model_name", ""),
                provider_name=getattr(model_config, "provider", ""),
            )
            self._record_cache_break(event)

        logger.debug(
            "Prompt cache: key=%s, is_new=%s, hit_count=%s, reuse_savings=%s%%",
            cache_res.cache_key[:8],
            cache_res.is_new_prefix,
            cache_res.hit_count,
            "90" if not cache_res.is_new_prefix else "0",
        )
        self._caching_mgr.save_manifest()

        # 3. Response caching (Skip execution)
        # Use first 100 chars as title heuristic for the task key
        key = self._response_cache.task_key(
            role=self._inner.name(),
            title=prompt[:100].strip(),
            description=prompt,
        )
        cached_entry, similarity = self._response_cache.lookup_entry(key)

        if cached_entry and cached_entry.verified:
            logger.info(
                "Response cache hit (similarity=%.3f) for session %s -- skipping spawn",
                similarity,
                session_id,
            )
            # Return a "virtual" spawn result with PID 0.
            # Orchestrator handles PID 0 as a completed task from cache.
            return SpawnResult(
                pid=0,
                log_path=workdir / f"{session_id}.log",
            )

        # 4. Cache miss: delegate to inner adapter
        return self._inner.spawn(
            prompt=prompt,
            workdir=workdir,
            model_config=model_config,
            session_id=session_id,
            mcp_config=mcp_config,
            timeout_seconds=timeout_seconds,
        )

    def name(self) -> str:
        """Return inner adapter's name."""
        return self._inner.name()

    def is_alive(self, pid: int) -> bool:
        """Delegate to inner adapter (always False for cached PID 0)."""
        if pid == 0:
            return False
        return self._inner.is_alive(pid)

    def kill(self, pid: int) -> None:
        """Delegate to inner adapter."""
        if pid == 0:
            return
        self._inner.kill(pid)

    def detect_tier(self) -> Any:
        """Delegate to inner adapter."""
        return self._inner.detect_tier()
