"""Prompt caching orchestration for token savings via prefix detection."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

logger = logging.getLogger(__name__)

# Hard cap on the number of agent session entries tracked simultaneously.
# When exceeded, the oldest entry is evicted FIFO to keep memory bounded.
MAX_AGENT_CACHE_ENTRIES: int = 10

# Hard cap on prompt-prefix manifest entries.  When exceeded, the oldest
# 10% (by created_at) are evicted to keep memory bounded.
_MAX_MANIFEST_ENTRIES: int = 100


class CacheBreakReason(Enum):
    """Classification of why a prompt cache break occurred."""

    SYSTEM = "system"  # role prompt, specialist agents, or shared context changed
    TOOLS = "tools"  # tool definitions or MCP config changed
    MODEL = "model"  # model or tier routing changed
    CONFIG = "config"  # agent config or project settings changed
    UNKNOWN = "unknown"  # cause not determined


# Savings-per-token at Anthropic's cached-input discount (90% off vs standard
# claude-sonnet-4 input price of $3.00/MTok).  Standard - cached = $2.70/MTok.
CACHED_SAVINGS_PER_TOKEN: float = 2.70 / 1_000_000


def _estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token for English prose.

    Args:
        text: Input text.

    Returns:
        Estimated token count (minimum 1).
    """
    return max(1, len(text) // 4)


@dataclass
class CacheEntry:
    """Single cached system prompt prefix.

    Attributes:
        cache_key: SHA-256 hash of the system prefix.
        system_prefix: The actual prefix text (role prompt + shared context).
        prefix_tokens: Estimated token count of the prefix (for tracking).
        hit_count: Number of times this prefix was reused.
        first_seen_at: Unix timestamp when first encountered.
        last_used_at: Unix timestamp of most recent use.
    """

    cache_key: str
    system_prefix: str
    prefix_tokens: int
    hit_count: int
    first_seen_at: float
    last_used_at: float | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dict."""
        return {
            "cache_key": self.cache_key,
            "system_prefix": self.system_prefix,
            "prefix_tokens": self.prefix_tokens,
            "hit_count": self.hit_count,
            "first_seen_at": self.first_seen_at,
            "last_used_at": self.last_used_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CacheEntry:
        """Deserialize from JSON dict."""
        return cls(
            cache_key=data["cache_key"],
            system_prefix=data["system_prefix"],
            prefix_tokens=data["prefix_tokens"],
            hit_count=data["hit_count"],
            first_seen_at=data["first_seen_at"],
            last_used_at=data.get("last_used_at"),
        )


@dataclass
class CacheBreakEvent:
    """Structured event emitted on every prompt cache break.

    Attributes:
        timestamp: Unix timestamp of the cache break.
        reason: Classification of why the cache broke.
        old_cache_key: Previous cache key that was invalidated (None if brand-new).
        new_cache_key: New cache key after the break.
        estimated_token_delta: Estimated token count difference (old vs new prefix).
        session_id: Agent session ID that triggered the break.
        model_name: Model name used for the request.
        provider_name: API provider name.
        changed_fields: List of field-level descriptions (e.g., "role: ...").
    """

    timestamp: float
    reason: CacheBreakReason
    old_cache_key: str | None
    new_cache_key: str
    estimated_token_delta: int
    session_id: str
    model_name: str = ""
    provider_name: str = ""
    changed_fields: list[str] = field(default_factory=list[str])

    def to_dict(self) -> dict[str, Any]:
        """Serialize to JSON-compatible dict."""
        return {
            "timestamp": self.timestamp,
            "reason": self.reason.value,
            "old_cache_key": self.old_cache_key,
            "new_cache_key": self.new_cache_key,
            "estimated_token_delta": self.estimated_token_delta,
            "session_id": self.session_id,
            "model_name": self.model_name,
            "provider_name": self.provider_name,
            "changed_fields": self.changed_fields,
        }

    def to_json_line(self) -> str:
        """Serialize to a single JSON line for JSONL storage."""
        return json.dumps(self.to_dict(), separators=(",", ":"))

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CacheBreakEvent:
        """Deserialize from JSON dict."""
        return cls(
            timestamp=data["timestamp"],
            reason=CacheBreakReason(data["reason"]),
            old_cache_key=data.get("old_cache_key"),
            new_cache_key=data["new_cache_key"],
            estimated_token_delta=data.get("estimated_token_delta", 0),
            session_id=data["session_id"],
            model_name=data.get("model_name", ""),
            provider_name=data.get("provider_name", ""),
            changed_fields=data.get("changed_fields", []),
        )


@dataclass
class CacheManifest:
    """Collection of cached prefixes with metadata.

    Attributes:
        entries: Dict mapping cache_key → CacheEntry.
        total_cached_tokens: Sum of all prefix_tokens.
        total_cached_requests: Total spawn calls using cached prefixes.
    """

    entries: dict[str, CacheEntry] = field(default_factory=lambda: dict[str, CacheEntry]())
    total_cached_tokens: int = 0
    total_cached_requests: int = 0

    def to_json_line(self) -> str:
        """Serialize entire manifest to single JSON line."""
        data = {
            "entries": {k: v.to_dict() for k, v in self.entries.items()},
            "total_cached_tokens": self.total_cached_tokens,
            "total_cached_requests": self.total_cached_requests,
        }
        return json.dumps(data, separators=(",", ":"))

    @classmethod
    def from_json_line(cls, line: str) -> CacheManifest:
        """Deserialize from JSON line."""
        data = json.loads(line)
        manifest = cls(
            total_cached_tokens=data.get("total_cached_tokens", 0),
            total_cached_requests=data.get("total_cached_requests", 0),
        )
        for cache_key, entry_data in data.get("entries", {}).items():
            manifest.entries[cache_key] = CacheEntry.from_dict(entry_data)
        return manifest


@dataclass
class PromptProcessResult:
    """Result of processing a prompt for caching.

    Attributes:
        cache_key: SHA-256 hash of the system prefix.
        system_prefix: The cached prefix text.
        task_suffix: The task-specific suffix.
        is_new_prefix: True if this is a new cache entry.
        hit_count: Number of times this prefix has been reused (before this spawn).
        first_seen: Timestamp when the prefix was first cached (None if reused).
        prefix_tokens: Estimated token count of the prefix.
        previous_cache_key: Old cache key that was replaced (None if first ever).
    """

    cache_key: str
    system_prefix: str
    task_suffix: str
    is_new_prefix: bool
    hit_count: int
    first_seen: float | None = None
    prefix_tokens: int = 0
    previous_cache_key: str | None = None
    expected_drop_reason: str | None = None  # reason if pre-announced; None = surprise break


def compute_cache_key(prefix: str) -> str:
    """Compute SHA-256 hash of a system prefix.

    Args:
        prefix: System prompt prefix text.

    Returns:
        Lowercase hex string (64 chars) of SHA-256 hash.
    """
    return hashlib.sha256(prefix.encode("utf-8")).hexdigest()


def make_prompt_cache_key(
    system_prompt: str,
    context_files: list[Path] | None = None,
) -> str:
    """Compute orchestrator-level cache key from system prompt and context files.

    Cache key = SHA-256(system_prompt + sorted file contents).
    Automatically invalidates when any context file's content changes.
    Missing files are silently skipped.

    Args:
        system_prompt: Role prompt and project context text.
        context_files: Optional list of file paths whose contents contribute
            to the cache key.  Files are sorted by path before hashing so
            order of the input list does not matter.

    Returns:
        Lowercase hex string (64 chars) of SHA-256 hash.
    """
    h = hashlib.sha256()
    h.update(system_prompt.encode("utf-8"))
    if context_files:
        for path in sorted(context_files):
            with contextlib.suppress(OSError):
                h.update(path.read_bytes())
    return h.hexdigest()


def extract_system_prefix(prompt: str) -> tuple[str, str]:
    """Extract cacheable system prefix from full prompt.

    The prefix includes:
    - Role prompt (e.g., "You are a backend engineer.")
    - Specialist agent descriptions (if present)
    - Project context (if present)

    The suffix includes:
    - Assigned tasks
    - Task-specific context
    - Instructions
    - Signal checks

    Args:
        prompt: Full prompt string.

    Returns:
        Tuple of (system_prefix, task_suffix).
    """
    task_marker = "\n## Assigned tasks\n"
    instruction_marker = "\n## Instructions\n"
    signal_marker = "\n## Signal files —"

    split_points: list[int] = []
    for marker in [task_marker, instruction_marker, signal_marker]:
        idx = prompt.find(marker)
        if idx != -1:
            split_points.append(idx)

    if not split_points:
        return prompt, ""

    split_idx: int = min(split_points)
    prefix = prompt[:split_idx]
    suffix = prompt[split_idx:]

    return prefix, suffix


@dataclass
class AgentCacheTracker:
    """Per-agent cache prefix state tracker with FIFO eviction.

    Tracks which cache key (system prefix hash) each active agent session
    is currently using. A hard cap of *max_entries* is enforced; when the
    cap is exceeded the oldest entry (first inserted, first out) is evicted
    so memory stays bounded during long-running multi-agent runs.

    Eviction policy: **FIFO** — the first session registered is the first to
    be evicted when the cap is reached.  Updating an existing session's cache
    key does **not** change its eviction priority (it keeps its original
    insertion position).

    Attributes:
        max_entries: Maximum number of tracked agent entries (default
            ``MAX_AGENT_CACHE_ENTRIES`` = 10).
    """

    max_entries: int = MAX_AGENT_CACHE_ENTRIES
    # OrderedDict preserves insertion order and supports popitem(last=False)
    # for deterministic FIFO eviction.
    _entries: OrderedDict[str, str] = field(
        default_factory=OrderedDict[str, str],
        repr=False,
    )

    def record(self, session_id: str, cache_key: str) -> str | None:
        """Record the active cache key for a session.

        If *session_id* is already tracked, its cache key is updated in-place
        (no eviction, position unchanged).  If it is new and the tracker is at
        capacity, the oldest session is evicted before the new one is inserted.

        Args:
            session_id: Stable agent session identifier (used elsewhere in
                Bernstein as the worktree/agent name, e.g. ``backend-b0fde029``).
            cache_key: SHA-256 cache key for the agent's current system prefix.

        Returns:
            The evicted *session_id* if eviction occurred, otherwise ``None``.
        """
        if session_id in self._entries:
            # Update in-place — insertion order (FIFO priority) unchanged.
            self._entries[session_id] = cache_key
            return None

        evicted: str | None = None
        if len(self._entries) >= self.max_entries:
            evicted, _ = self._entries.popitem(last=False)  # oldest = FIFO
            logger.debug(
                "AgentCacheTracker: evicted session %s (cap=%d reached)",
                evicted,
                self.max_entries,
            )

        self._entries[session_id] = cache_key
        return evicted

    def get(self, session_id: str) -> str | None:
        """Return the cache key currently tracked for *session_id*, or ``None``.

        Args:
            session_id: Stable agent session identifier.

        Returns:
            Cache key string if the session is tracked, otherwise ``None``.
        """
        return self._entries.get(session_id)

    def remove(self, session_id: str) -> None:
        """Remove *session_id* from the tracker (e.g. when the agent exits).

        A no-op if the session is not currently tracked.

        Args:
            session_id: Stable agent session identifier.
        """
        self._entries.pop(session_id, None)

    def __len__(self) -> int:
        return len(self._entries)

    def __contains__(self, session_id: object) -> bool:
        return session_id in self._entries


class PromptCachingManager:
    """Manages prompt caching: prefix extraction, deduplication, manifest persistence.

    Args:
        workdir: Project working directory.
    """

    def __init__(self, workdir: Path) -> None:
        self._workdir = workdir
        self._manifest = CacheManifest()
        self._manifest_path = workdir / ".sdd" / "caching" / "manifest.jsonl"
        self._last_active_key: str | None = None
        self._expected_drops: set[str] = set()
        self._agent_tracker: AgentCacheTracker = AgentCacheTracker()
        self._load_manifest()

    def mark_expected_drop(self, reason: str) -> None:
        """Record that the next cache break is expected (e.g., after compaction).

        This clears the last-active-key baseline so the next new prefix is
        treated as an expected drop, not a surprise cache break.

        Args:
            reason: Explanation for why the drop is expected (e.g. "compaction").
        """
        self._expected_drops.add(reason)
        self._last_active_key = None  # Reset baseline to avoid false break
        logger.info("Expected cache drop recorded: reason=%s", reason)

    def is_expected_drop(self) -> bool:
        """Check whether a cache break was pre-announced.

        Returns:
            True if mark_expected_drop() was called before this check.
        """
        was_expected = bool(self._expected_drops)
        if was_expected:
            self._expected_drops.clear()
        return was_expected

    def _load_manifest(self) -> None:
        """Load existing cache manifest if it exists."""
        if self._manifest_path.exists():
            try:
                with open(self._manifest_path) as f:
                    line = f.read().strip()
                    if line:
                        self._manifest = CacheManifest.from_json_line(line)
                        self._backfill_token_estimates()
            except (OSError, json.JSONDecodeError) as exc:
                logger.warning("Failed to load cache manifest: %s", exc)

    def _backfill_token_estimates(self) -> None:
        """Estimate prefix_tokens for entries where it was not previously set.

        Older manifest entries have prefix_tokens=0 because estimation was not
        implemented when they were first written.  Re-estimate from the stored
        system_prefix text so that subsequent cache hits correctly accrue to
        total_cached_tokens.
        """
        for entry in self._manifest.entries.values():
            if entry.prefix_tokens == 0 and entry.system_prefix:
                entry.prefix_tokens = _estimate_tokens(entry.system_prefix)

    def _evict_manifest_if_needed(self) -> None:
        """Evict oldest 10% of manifest entries when the cap is exceeded."""
        if len(self._manifest.entries) <= _MAX_MANIFEST_ENTRIES:
            return
        sorted_keys = sorted(
            self._manifest.entries.keys(),
            key=lambda k: self._manifest.entries[k].first_seen_at,
        )
        evict_count = max(1, len(sorted_keys) // 10)
        for k in sorted_keys[:evict_count]:
            del self._manifest.entries[k]
        logger.debug(
            "Prompt cache manifest evicted %d oldest entries (cap=%d)",
            evict_count,
            _MAX_MANIFEST_ENTRIES,
        )

    def process_prompt(
        self,
        prompt: str,
        session_id: str | None = None,
    ) -> PromptProcessResult:
        """Process a prompt: extract prefix, check cache, update manifest.

        When *session_id* is provided the per-agent cache tracker is updated so
        that each active agent's current system-prefix state is recorded with
        FIFO eviction once the cap (``MAX_AGENT_CACHE_ENTRIES``) is reached.

        Args:
            prompt: Full prompt string.
            session_id: Optional stable agent session identifier.  When
                supplied the tracker is updated; the oldest session is evicted
                if the cap is exceeded.

        Returns:
            PromptProcessResult with cache key, prefix, suffix, and hit metadata.
        """
        system_prefix, task_suffix = extract_system_prefix(prompt)
        cache_key = compute_cache_key(system_prefix)

        is_new = cache_key not in self._manifest.entries
        hit_count = 0

        if is_new:
            now = time.time()
            entry = CacheEntry(
                cache_key=cache_key,
                system_prefix=system_prefix,
                prefix_tokens=_estimate_tokens(system_prefix),
                hit_count=0,
                first_seen_at=now,
            )
            self._manifest.entries[cache_key] = entry
            self._evict_manifest_if_needed()
            # Cache break: the new prefix replaces whatever was last used
            prev_key = self._last_active_key if self._last_active_key != cache_key else None
        else:
            entry = self._manifest.entries[cache_key]
            entry.hit_count += 1
            entry.last_used_at = time.time()
            self._manifest.total_cached_requests += 1
            self._manifest.total_cached_tokens += entry.prefix_tokens
            hit_count = entry.hit_count
            prev_key = None

        self._last_active_key = cache_key

        # Update per-agent tracker (FIFO eviction when cap is exceeded)
        if session_id is not None:
            self._agent_tracker.record(session_id, cache_key)

        was_expected = self._expected_drops.pop() if self._expected_drops else None

        return PromptProcessResult(
            cache_key=cache_key,
            system_prefix=system_prefix,
            task_suffix=task_suffix,
            is_new_prefix=is_new,
            hit_count=hit_count,
            first_seen=self._manifest.entries[cache_key].first_seen_at if not is_new else time.time(),
            prefix_tokens=self._manifest.entries[cache_key].prefix_tokens,
            previous_cache_key=prev_key,
            expected_drop_reason=was_expected,
        )

    def save_manifest(self) -> None:
        """Persist manifest to .sdd/caching/manifest.jsonl."""
        self._manifest_path.parent.mkdir(parents=True, exist_ok=True)
        with open(self._manifest_path, "w") as f:
            f.write(self._manifest.to_json_line())
        logger.debug("Saved cache manifest to %s", self._manifest_path)

    def get_statistics(self) -> dict[str, Any]:
        """Return cache statistics for monitoring.

        Returns:
            Dict with cache_entries, total_cached_requests, total_cached_tokens,
            estimated_savings_usd, manifest_path, tracked_agents, and
            max_tracked_agents.
        """
        return {
            "cache_entries": len(self._manifest.entries),
            "total_cached_requests": self._manifest.total_cached_requests,
            "total_cached_tokens": self._manifest.total_cached_tokens,
            "estimated_savings_usd": round(self._manifest.total_cached_tokens * CACHED_SAVINGS_PER_TOKEN, 6),
            "manifest_path": str(self._manifest_path),
            "tracked_agents": len(self._agent_tracker),
            "max_tracked_agents": self._agent_tracker.max_entries,
        }


# ---------------------------------------------------------------------------
# Cache break detection with diff generation (T561)
# ---------------------------------------------------------------------------


def generate_cache_break_diff(old_prefix: str, new_prefix: str) -> list[str]:
    """Generate a human-readable diff summary for a cache break (T561).

    Compares the old and new system prefixes and returns a list of
    field-level change descriptions suitable for logging or trace embedding.

    Args:
        old_prefix: Previous system prefix (before the break).
        new_prefix: New system prefix (after the break).

    Returns:
        List of change description strings.
    """
    import difflib

    if old_prefix == new_prefix:
        return []

    old_lines = old_prefix.splitlines()
    new_lines = new_prefix.splitlines()

    changes: list[str] = []
    for group in difflib.SequenceMatcher(None, old_lines, new_lines).get_grouped_opcodes(n=1):
        for tag, i1, i2, j1, j2 in group:
            if tag == "replace":
                changes.append(f"changed lines {i1 + 1}-{i2}: {old_lines[i1]!r} → {new_lines[j1]!r}")
            elif tag == "delete":
                changes.append(f"removed lines {i1 + 1}-{i2}: {old_lines[i1]!r}")
            elif tag == "insert":
                changes.append(f"added lines {j1 + 1}-{j2}: {new_lines[j1]!r}")

    return changes[:10]  # cap at 10 changes for readability


# ---------------------------------------------------------------------------
# Per-model cache read/write pricing tiers (T569)
# ---------------------------------------------------------------------------


_CACHE_PRICING: dict[str, dict[str, float]] = {
    # Model → {cache_read_per_1m, cache_write_per_1m}
    # Anthropic Claude 3.x / 4.x (approximate)
    "claude-3-5-sonnet": {"cache_read_per_1m": 0.30, "cache_write_per_1m": 3.75},
    "claude-3-5-haiku": {"cache_read_per_1m": 0.08, "cache_write_per_1m": 1.00},
    "claude-3-opus": {"cache_read_per_1m": 1.50, "cache_write_per_1m": 18.75},
    "claude-sonnet-4": {"cache_read_per_1m": 0.30, "cache_write_per_1m": 3.75},
    "claude-haiku-4": {"cache_read_per_1m": 0.08, "cache_write_per_1m": 1.00},
    "claude-opus-4": {"cache_read_per_1m": 1.50, "cache_write_per_1m": 18.75},
    # Default fallback
    "_default": {"cache_read_per_1m": 0.30, "cache_write_per_1m": 3.75},
}


def get_cache_pricing(model_name: str) -> dict[str, float]:
    """Return cache read/write pricing for *model_name* (T569).

    Args:
        model_name: Model identifier (partial match supported).

    Returns:
        Dict with ``cache_read_per_1m`` and ``cache_write_per_1m`` in USD.
    """
    model_lower = model_name.lower()
    for key, pricing in _CACHE_PRICING.items():
        if key == "_default":
            continue
        if key in model_lower or model_lower.startswith(key):
            return dict(pricing)
    return dict(_CACHE_PRICING["_default"])


def compute_cache_cost(
    model_name: str,
    cache_read_tokens: int,
    cache_write_tokens: int,
) -> float:
    """Compute the USD cost for cache read and write tokens (T569).

    Args:
        model_name: Model identifier.
        cache_read_tokens: Number of tokens read from cache.
        cache_write_tokens: Number of tokens written to cache.

    Returns:
        Total USD cost for the cache operations.
    """
    pricing = get_cache_pricing(model_name)
    read_cost = (cache_read_tokens / 1_000_000) * pricing["cache_read_per_1m"]
    write_cost = (cache_write_tokens / 1_000_000) * pricing["cache_write_per_1m"]
    return read_cost + write_cost


# ---------------------------------------------------------------------------
# Cache-safe forked agent params (T597 / T446)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CacheSafeParams:
    """Typed structure for forked agent parameters that preserve cache safety.

    When an agent forks a sub-agent, the sub-agent should reuse the parent's
    system prefix to maximise cache hits.  This dataclass ensures all
    required fields are present and typed correctly so prompt-cache keys
    remain stable.

    Attributes:
        inherited_cache_key: SHA-256 cache key from the parent's system prefix.
        system_prefix: Full system prompt prefix to be inherited by the child.
        fork_role: Role override for the forked agent (empty = inherit parent).
        fork_model: Model override for the forked agent (empty = inherit parent).
        fork_messages: Optional conversation messages to pass to the forked
            agent for continuation context.
        cache_safe: Always True when built via this class — signals to the
            spawner that prompt caching is safe.
    """

    inherited_cache_key: str
    system_prefix: str
    fork_role: str = ""
    fork_model: str = ""
    fork_messages: list[dict[str, str]] = field(default_factory=lambda: list[dict[str, str]]())
    cache_safe: bool = True

    def to_dict(self) -> dict[str, Any]:
        """Convert to a dict suitable for passing to the spawner."""
        result: dict[str, Any] = {
            "inherited_cache_key": self.inherited_cache_key,
            "system_prefix": self.system_prefix,
            "role": self.fork_role,
            "model": self.fork_model,
            "cache_safe": self.cache_safe,
        }
        if self.fork_messages:
            result["fork_messages"] = self.fork_messages
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CacheSafeParams:
        """Reconstruct a CacheSafeParams from a dict (e.g., serialized state).

        Args:
            data: Dict with the same keys as :meth:`to_dict`.

        Returns:
            New CacheSafeParams instance.
        """
        return cls(
            inherited_cache_key=data["inherited_cache_key"],
            system_prefix=data["system_prefix"],
            fork_role=data.get("role", ""),
            fork_model=data.get("model", ""),
            fork_messages=data.get("fork_messages", []),
            cache_safe=data.get("cache_safe", True),
        )


def build_cache_safe_fork_params(
    parent_cache_key: str,
    parent_system_prefix: str,
    fork_role: str = "",
    fork_model: str = "",
    fork_messages: list[dict[str, str]] | None = None,
) -> CacheSafeParams:
    """Build cache-safe parameters for a forked sub-agent (T597 / T446).

    When an agent forks a sub-agent, the sub-agent should reuse the parent's
    system prefix to maximise cache hits.  Returns a typed ``CacheSafeParams``
    that the spawner can use to preserve the parent's prompt-cache prefix.

    Args:
        parent_cache_key: SHA-256 cache key of the parent agent's system prefix.
        parent_system_prefix: The parent's system prefix text for the child to
            inherit (ensures cache prefix alignment).
        fork_role: Role override for the forked agent (empty = inherit parent).
        fork_model: Model override for the forked agent (empty = inherit parent).
        fork_messages: Optional conversation messages to pass to the forked
            agent for continuation context.

    Returns:
        CacheSafeParams instance with stable cache key and inheritance data.
    """
    return CacheSafeParams(
        inherited_cache_key=parent_cache_key,
        system_prefix=parent_system_prefix,
        fork_role=fork_role,
        fork_model=fork_model,
        fork_messages=fork_messages or [],
        cache_safe=True,
    )


# ---------------------------------------------------------------------------
# Cache break detection with diff generation (T561)
# ---------------------------------------------------------------------------

import difflib as _difflib  # noqa: E402


@dataclass
class CacheBreak:
    """Record of a cache break with diff analysis (T561)."""

    reason: CacheBreakReason
    old_hash: str
    new_hash: str
    old_content: str
    new_content: str
    diff_lines: list[str] = field(default_factory=lambda: [])
    timestamp: float = field(default_factory=time.time)

    def generate_diff(self, context_lines: int = 3) -> None:
        """Generate a unified diff between old and new content."""
        old_lines = self.old_content.splitlines(keepends=True)
        new_lines = self.new_content.splitlines(keepends=True)
        diff = _difflib.unified_diff(
            old_lines,
            new_lines,
            fromfile="cached",
            tofile="current",
            n=context_lines,
        )
        self.diff_lines = list(diff)


def detect_cache_break(
    old_content: str,
    new_content: str,
    *,
    reason: CacheBreakReason | None = None,
) -> CacheBreak | None:
    """Detect and analyze a cache break between two prompt contents (T561).

    Args:
        old_content: Previously cached content.
        new_content: Current content.
        reason: Optional classification of the break.

    Returns:
        CacheBreak object if a break is detected, None otherwise.
    """
    old_hash = hashlib.sha256(old_content.encode()).hexdigest()
    new_hash = hashlib.sha256(new_content.encode()).hexdigest()

    if old_hash == new_hash:
        return None

    break_obj = CacheBreak(
        reason=reason or CacheBreakReason.UNKNOWN,
        old_hash=old_hash,
        new_hash=new_hash,
        old_content=old_content,
        new_content=new_content,
    )
    break_obj.generate_diff()
    return break_obj


# ---------------------------------------------------------------------------
# Expected drop notifications for cache baselines (T564)
# ---------------------------------------------------------------------------


@dataclass
class CacheBaselineAlert:
    """Alert for cache baseline drops."""

    baseline_name: str
    previous_value: float
    current_value: float
    drop_percentage: float
    threshold: float
    timestamp: datetime = field(default_factory=lambda: datetime.now(UTC))
    metadata: dict[str, Any] = field(default_factory=lambda: {})


class CacheBaselineMonitor:
    """Monitors cache baseline metrics and alerts on significant drops."""

    def __init__(self, alert_threshold: float = 0.1):  # 10% drop threshold
        self.alert_threshold = alert_threshold
        self.baselines: dict[str, float] = {}
        self.alert_handlers: list[Callable[[CacheBaselineAlert], None]] = []
        self._lock = asyncio.Lock()

    async def update_baseline(self, name: str, current_value: float) -> CacheBaselineAlert | None:
        """Update baseline and return alert if significant drop detected."""
        async with self._lock:
            previous = self.baselines.get(name)
            self.baselines[name] = current_value

            if previous is not None and previous > 0:
                drop_pct = (previous - current_value) / previous

                if drop_pct >= self.alert_threshold:
                    alert = CacheBaselineAlert(
                        baseline_name=name,
                        previous_value=previous,
                        current_value=current_value,
                        drop_percentage=drop_pct,
                        threshold=self.alert_threshold,
                    )

                    # Notify all handlers
                    for handler in self.alert_handlers:
                        try:
                            handler(alert)
                        except Exception as e:
                            logger.warning(f"Alert handler failed: {e}")

                    return alert
            return None

    def add_alert_handler(self, handler: Callable[[CacheBaselineAlert], None]) -> None:
        """Add a handler for baseline drop alerts."""
        self.alert_handlers.append(handler)

    def get_baseline(self, name: str) -> float | None:
        """Get current baseline value."""
        return self.baselines.get(name)


# Global monitor instance
_baseline_monitor = CacheBaselineMonitor()


def monitor_cache_baseline(name: str, current_value: float) -> CacheBaselineAlert | None:
    """Monitor cache baseline and return alert if significant drop detected."""
    return asyncio.run(_baseline_monitor.update_baseline(name, current_value))


def on_baseline_drop(alert: CacheBaselineAlert) -> None:
    """Default handler for baseline drop alerts."""
    logger.warning(
        f"Cache baseline drop detected: {alert.baseline_name} "
        f"dropped by {alert.drop_percentage:.1%} "
        f"({alert.previous_value:.2f} → {alert.current_value:.2f})"
    )


# Register default handler
_baseline_monitor.add_alert_handler(on_baseline_drop)
# ---------------------------------------------------------------------------
# Per-model cache read/write pricing tiers (T569)
# ---------------------------------------------------------------------------


def calculate_cache_cost_savings(provider: str, model: str, tokens: int, operation: str = "read") -> dict[str, Any]:
    """Calculate cache cost savings using pricing tiers (T569)."""
    from bernstein.core.cost import calculate_cache_operation_savings, get_cache_pricing_tier

    tier = get_cache_pricing_tier(provider, model)
    if not tier:
        return {"savings_usd": 0.0, "savings_percentage": 0.0, "tier_available": False, "tokens": tokens}

    savings = calculate_cache_operation_savings(provider, model, tokens, operation)

    return {
        "savings_usd": savings,
        "savings_percentage": tier.savings_percentage,
        "tier_available": True,
        "provider": provider,
        "model": model,
        "operation": operation,
        "tokens": tokens,
        "cache_read_price_per_1m": tier.cache_read_usd_per_1m,
        "cache_write_price_per_1m": tier.cache_write_usd_per_1m,
        "standard_read_price_per_1m": tier.standard_read_usd_per_1m,
        "standard_write_price_per_1m": tier.standard_write_usd_per_1m,
    }


def record_cache_cost_metrics(
    provider: str, model: str, tokens: int, operation: str = "read", cache_hit: bool = True
) -> None:
    """Record cache cost metrics for analytics (T569)."""
    if not cache_hit:
        return

    savings_data = calculate_cache_cost_savings(provider, model, tokens, operation)

    if savings_data["tier_available"] and savings_data["savings_usd"] > 0:
        logger.info(
            f"Cache {operation} savings: ${savings_data['savings_usd']:.6f} "
            f"({savings_data['savings_percentage']:.1%}) for {tokens} tokens "
            f"({provider}:{model})"
        )
