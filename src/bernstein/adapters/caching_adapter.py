"""Caching wrapper for CLI adapters to enable prompt prefix deduplication and response reuse."""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from bernstein.adapters.base import DEFAULT_TIMEOUT_SECONDS, CLIAdapter, SpawnResult
from bernstein.core.prompt_caching import (
    CacheBreakCorrelator,
    CacheBreakEvent,
    CacheBreakReason,
    PromptCachingManager,
)
from bernstein.core.semantic_cache import ResponseCacheManager

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.adapters.plugin_sdk import AdapterPluginInfo
    from bernstein.core.config.platform_compat import ProcessReapReceipt
    from bernstein.core.models import ModelConfig

logger = logging.getLogger(__name__)

# Shared correlator for all CachingAdapter instances in the same process.
# Cross-agent systemic detection requires a single shared buffer so that
# breaks from different agents are correlated against each other.
_CORRELATOR: CacheBreakCorrelator = CacheBreakCorrelator()


class CachingAdapter(CLIAdapter):
    """Wraps a CLIAdapter to enable prompt caching and response reuse.

    Intercepts spawn calls to:
    - Extract and deduplicate system prompt prefixes
    - Track cache break events with cross-agent systemic correlation
    - Skip spawn if a verified response hit is found (Cosine >= 0.95)

    Args:
        inner_adapter: The underlying CLIAdapter to wrap.
        workdir: Project working directory for cache storage.
        ttl_seconds: Time-to-live for response cache entries in seconds.
        correlator: Optional CacheBreakCorrelator; defaults to the module-level
            shared instance.  Pass an explicit instance in tests to avoid
            cross-test state bleed.
    """

    def __init__(
        self,
        inner_adapter: CLIAdapter,
        workdir: Path,
        ttl_seconds: int = 3600,
        *,
        correlator: CacheBreakCorrelator | None = None,
    ) -> None:
        super().__init__()
        self._inner = inner_adapter
        self._caching_mgr = PromptCachingManager(workdir)
        self._cache_break_path = workdir / ".sdd" / "metrics" / "cache_breaks.jsonl"
        self._response_cache = ResponseCacheManager(workdir, ttl_seconds=float(ttl_seconds))
        self._correlator = correlator if correlator is not None else _CORRELATOR

    def _record_cache_break(self, event: CacheBreakEvent) -> None:
        """Append a cache break event to the JSONL file and correlate across agents.

        Writes the event to ``.sdd/metrics/cache_breaks.jsonl`` then passes it
        to the shared :class:`CacheBreakCorrelator`.  If two or more agents
        share the same ``component_fingerprint`` within the correlation window,
        the group is labelled *systemic* in structured logs.

        Args:
            event: The cache break event to record.
        """
        self._cache_break_path.parent.mkdir(parents=True, exist_ok=True)
        with self._cache_break_path.open("a") as f:
            f.write(event.to_json_line() + "\n")

        correlation = self._correlator.add_event(event)
        # ``new_cache_key`` is a hash of prompt prefix bytes (not a
        # credential); ``delta_tokens`` is an LLM-token count.
        # nosemgrep: python.lang.security.audit.logging.logger-credential-leak.python-logger-credential-disclosure
        logger.info(
            "Cache break: reason=%s, key=%s, delta_tokens=%s, break_label=%s",
            event.reason.value,
            event.new_cache_key[:8],
            event.estimated_token_delta,
            correlation.label,
            extra={
                "break_label": correlation.label,
                "fingerprint": correlation.fingerprint,
                "agent_count": len(correlation.agent_ids),
                "is_systemic": correlation.is_systemic,
                "reason_class": event.reason.value,
            },
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
        task_scope: str = "medium",
        budget_multiplier: float = 1.0,
        system_addendum: str = "",
        multimodal_context: Any | None = None,
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
        # 1. Prompt prefix caching (Anthropic-style) - pass session_id for per-agent tracking
        cache_res = self._caching_mgr.process_prompt(prompt, session_id=session_id)

        # 2. Emit cache break event when prefix is new
        if cache_res.is_new_prefix and not cache_res.expected_drop_reason:
            import hashlib as _hashlib

            # The component fingerprint is a short hash of the reason class and
            # new cache key - agents that receive the same upstream change (e.g.
            # same template update) will produce identical fingerprints, enabling
            # cross-agent systemic-break correlation.
            _reason = CacheBreakReason.SYSTEM
            _fingerprint = _hashlib.sha256(f"{_reason.value}:{cache_res.cache_key}".encode()).hexdigest()[:16]

            event = CacheBreakEvent(
                timestamp=time.time(),
                reason=_reason,
                old_cache_key=None,
                new_cache_key=cache_res.cache_key,
                estimated_token_delta=cache_res.prefix_tokens,
                session_id=session_id,
                model_name=getattr(model_config, "model_name", ""),
                provider_name=getattr(model_config, "provider", ""),
                component_fingerprint=_fingerprint,
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

        if multimodal_context is None and cached_entry and cached_entry.verified:
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

        # 4. Cache miss: delegate to inner adapter.
        # Forward ALL kwargs from the base CLIAdapter.spawn interface explicitly
        # so that type checkers catch future drift - missing budget_multiplier
        # or system_addendum silently broke retry budgets and role-scoped
        # system prompts.
        return self._inner.spawn(
            prompt=prompt,
            workdir=workdir,
            model_config=model_config,
            session_id=session_id,
            mcp_config=mcp_config,
            timeout_seconds=timeout_seconds,
            task_scope=task_scope,
            budget_multiplier=budget_multiplier,
            system_addendum=system_addendum,
            multimodal_context=multimodal_context,
        )

    def name(self) -> str:
        """Return inner adapter's name."""
        return self._inner.name()

    def plugin_info(self) -> AdapterPluginInfo:
        """Delegate plugin metadata to the wrapped adapter.

        Capability gates (e.g. ``ensure_sampling_params_supported``)
        duck-type on ``plugin_info`` to read declared capabilities, so the
        caching wrapper must be transparent here or the inner adapter's
        capabilities become invisible and valid spawns are refused.

        Raises:
            AttributeError: When the wrapped adapter is not a plugin
                adapter (has no ``plugin_info``), so callers see exactly
                the behaviour of the unwrapped adapter.
        """
        return self._inner.plugin_info()  # type: ignore[attr-defined]

    def is_alive(self, pid: int) -> bool:
        """Delegate to inner adapter (always False for cached PID 0)."""
        if pid == 0:
            return False
        return self._inner.is_alive(pid)

    def kill(self, pid: int) -> ProcessReapReceipt | None:
        """Delegate to inner adapter (no receipt for cached PID 0)."""
        if pid == 0:
            return None
        return self._inner.kill(pid)

    def detect_tier(self) -> Any:
        """Delegate to inner adapter."""
        return self._inner.detect_tier()

    def __getattr__(self, name: str) -> Any:
        """Fall back to the wrapped adapter for any attribute not found here.

        ``CLIAdapter`` subclasses declare capability flags as plain class
        attributes (e.g. ``consumes_heartbeat_dir``, and any future
        capability flag not yet known to this wrapper). Callers duck-type
        on those attributes via ``getattr(adapter, "flag", False)``.
        Without this delegation, wrapping an adapter in ``CachingAdapter``
        silently resets every such flag to its ``getattr`` default because
        Python attribute lookup never reaches the inner adapter's class -
        the capability appears to vanish with no error (bug #11: the
        spawner stopped injecting ``heartbeat_dir`` for every
        caching-wrapped openai_agents spawn, orphaning heartbeats in the
        worktree and triggering false ``no_heartbeat`` kills).

        ``__getattr__`` only fires when normal attribute lookup (instance
        ``__dict__`` plus the ``CachingAdapter``/``CLIAdapter`` MRO) fails,
        so it never shadows an attribute or method this class already
        defines - it is a pure fallback, not an override.

        Raises:
            AttributeError: When the wrapped adapter also lacks the
                attribute, matching the error Python would give for a
                direct lookup on this class.
        """
        try:
            inner = self.__dict__["_inner"]
        except KeyError:
            raise AttributeError(name) from None
        return getattr(inner, name)
