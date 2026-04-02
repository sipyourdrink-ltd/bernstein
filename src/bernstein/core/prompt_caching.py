"""Prompt caching orchestration for token savings via prefix detection."""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


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
        self._load_manifest()

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

    def process_prompt(self, prompt: str) -> PromptProcessResult:
        """Process a prompt: extract prefix, check cache, update manifest.

        Args:
            prompt: Full prompt string.

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

        return PromptProcessResult(
            cache_key=cache_key,
            system_prefix=system_prefix,
            task_suffix=task_suffix,
            is_new_prefix=is_new,
            hit_count=hit_count,
            first_seen=self._manifest.entries[cache_key].first_seen_at if not is_new else time.time(),
            prefix_tokens=self._manifest.entries[cache_key].prefix_tokens,
            previous_cache_key=prev_key,
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
            estimated_savings_usd, and manifest_path.
        """
        return {
            "cache_entries": len(self._manifest.entries),
            "total_cached_requests": self._manifest.total_cached_requests,
            "total_cached_tokens": self._manifest.total_cached_tokens,
            "estimated_savings_usd": round(self._manifest.total_cached_tokens * CACHED_SAVINGS_PER_TOKEN, 6),
            "manifest_path": str(self._manifest_path),
        }
