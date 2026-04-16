"""Prompt caching support for reducing LLM API costs.

Marks static prompt sections (role templates, coding standards, project
rules) as cacheable so that adapters can use provider-specific caching
mechanisms.  Anthropic's prompt caching gives a 90% discount on cached
input tokens when the same prefix is reused within 5 minutes.

Usage:
    blocks = mark_cacheable_sections(role_template, project_context, task_instructions)
    # blocks = [
    #     {"content": "...", "cacheable": True},   # static prefix
    #     {"content": "...", "cacheable": False},   # task-specific
    # ]
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class PromptBlock:
    """A section of a prompt with caching metadata."""

    content: str
    cacheable: bool = False
    label: str = ""

    @property
    def token_estimate(self) -> int:
        """Rough token estimate (1 token ≈ 4 chars for English text)."""
        return len(self.content) // 4


@dataclass
class CacheStats:
    """Tracks prompt cache hit/miss statistics."""

    total_requests: int = 0
    cache_hits: int = 0
    cached_tokens: int = 0
    uncached_tokens: int = 0

    def record_request(self, cached_input_tokens: int, uncached_input_tokens: int) -> None:
        """Record a single API request's cache stats."""
        self.total_requests += 1
        if cached_input_tokens > 0:
            self.cache_hits += 1
        self.cached_tokens += cached_input_tokens
        self.uncached_tokens += uncached_input_tokens

    @property
    def hit_rate(self) -> float:
        """Fraction of requests with at least one cache hit."""
        if self.total_requests == 0:
            return 0.0
        return self.cache_hits / self.total_requests

    @property
    def estimated_savings_pct(self) -> float:
        """Estimated cost savings from caching (Anthropic: 90% discount)."""
        total = self.cached_tokens + self.uncached_tokens
        if total == 0:
            return 0.0
        # Cached tokens cost 10% of normal price
        effective_cost = self.uncached_tokens + (self.cached_tokens * 0.1)
        return 1.0 - (effective_cost / total)

    def to_dict(self) -> dict[str, Any]:
        """Serialize for cost reports."""
        return {
            "total_requests": self.total_requests,
            "cache_hits": self.cache_hits,
            "hit_rate": round(self.hit_rate, 3),
            "cached_tokens": self.cached_tokens,
            "uncached_tokens": self.uncached_tokens,
            "estimated_savings_pct": round(self.estimated_savings_pct, 3),
        }


def mark_cacheable_sections(
    role_template: str,
    project_context: str = "",
    task_instructions: str = "",
    extra_static: str = "",
) -> list[PromptBlock]:
    """Split a prompt into cacheable (static) and non-cacheable (dynamic) blocks.

    The static prefix (role template + project context + extra static content)
    is marked as cacheable.  Task-specific instructions are not cached because
    they change with every agent spawn.

    Args:
        role_template: The role-specific system prompt (from templates/roles/).
        project_context: Project-level context (.sdd/project.md, coding standards).
        task_instructions: Task-specific instructions (goals, file lists, completion commands).
        extra_static: Additional static content (bulletin summary, team context).

    Returns:
        List of PromptBlocks with cacheable flags set appropriately.
    """
    blocks: list[PromptBlock] = []

    # Static prefix — cacheable (same across agents of the same role)
    static_parts: list[str] = []
    if role_template:
        static_parts.append(role_template)
    if project_context:
        static_parts.append(project_context)
    if extra_static:
        static_parts.append(extra_static)

    if static_parts:
        blocks.append(PromptBlock(
            content="\n\n".join(static_parts),
            cacheable=True,
            label="system_prefix",
        ))

    # Dynamic suffix — NOT cacheable (changes per task)
    if task_instructions:
        blocks.append(PromptBlock(
            content=task_instructions,
            cacheable=False,
            label="task_instructions",
        ))

    return blocks


def compute_cache_key(content: str) -> str:
    """Compute a stable hash for cache key identification."""
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def blocks_to_prompt(blocks: list[PromptBlock]) -> str:
    """Flatten blocks back into a single prompt string.

    Used by adapters that don't support block-level caching.
    """
    return "\n\n".join(b.content for b in blocks if b.content)


def get_cacheable_prefix(blocks: list[PromptBlock]) -> str:
    """Extract the cacheable prefix from a list of blocks."""
    parts = [b.content for b in blocks if b.cacheable and b.content]
    return "\n\n".join(parts)
