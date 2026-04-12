"""CLAUDE-006: Prompt caching optimization based on shared context analysis."""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

MIN_CACHEABLE_TOKENS: int = 1024
_CHARS_PER_TOKEN: int = 4
_SAVINGS_PER_MTOK: float = 2.70


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // _CHARS_PER_TOKEN)


def _hash_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class CacheableSegment:
    segment_id: str
    content: str
    token_estimate: int
    shared_by: frozenset[str]
    cache_type: str
    savings_per_hit_usd: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "segment_id": self.segment_id,
            "token_estimate": self.token_estimate,
            "shared_by": sorted(self.shared_by),
            "cache_type": self.cache_type,
            "savings_per_hit_usd": round(self.savings_per_hit_usd, 6),
        }


@dataclass(frozen=True, slots=True)
class CacheOptimizationPlan:
    segments: list[CacheableSegment]
    total_cacheable_tokens: int
    estimated_savings_per_run_usd: float
    cache_hit_rate: float

    def to_dict(self) -> dict[str, Any]:
        return {
            "segments": [s.to_dict() for s in self.segments],
            "total_cacheable_tokens": self.total_cacheable_tokens,
            "estimated_savings_per_run_usd": round(self.estimated_savings_per_run_usd, 6),
            "cache_hit_rate": round(self.cache_hit_rate, 3),
        }


@dataclass
class PromptCacheOptimizer:
    agent_prompts: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    segments: list[CacheableSegment] = field(default_factory=list[CacheableSegment])

    def add_agent_prompt(self, role: str, parts: list[str]) -> None:
        self.agent_prompts[role] = parts

    def analyze(self) -> CacheOptimizationPlan:
        content_map: dict[str, tuple[str, set[str]]] = {}
        for role, parts in self.agent_prompts.items():
            for part in parts:
                h = _hash_text(part)
                if h in content_map:
                    content_map[h][1].add(role)
                else:
                    content_map[h] = (part, {role})
        self.segments = []
        for h, (content, roles) in content_map.items():
            tokens = _estimate_tokens(content)
            if tokens < MIN_CACHEABLE_TOKENS:
                continue
            savings = (tokens / 1_000_000) * _SAVINGS_PER_MTOK * max(len(roles) - 1, 0)
            cache_type = "system" if len(roles) > 1 else "context"
            self.segments.append(
                CacheableSegment(
                    segment_id=h[:16],
                    content=content,
                    token_estimate=tokens,
                    shared_by=frozenset(roles),
                    cache_type=cache_type,
                    savings_per_hit_usd=savings,
                )
            )
        self.segments.sort(key=lambda s: s.savings_per_hit_usd, reverse=True)
        total_tokens = sum(s.token_estimate for s in self.segments)
        total_savings = sum(s.savings_per_hit_usd for s in self.segments)
        if self.segments:
            shared_tokens = sum(s.token_estimate for s in self.segments if len(s.shared_by) > 1)
            hit_rate = shared_tokens / total_tokens if total_tokens > 0 else 0.0
        else:
            hit_rate = 0.0
        return CacheOptimizationPlan(
            segments=self.segments,
            total_cacheable_tokens=total_tokens,
            estimated_savings_per_run_usd=total_savings,
            cache_hit_rate=hit_rate,
        )

    def recommend_prefix_order(self) -> list[str]:
        sorted_segments = sorted(self.segments, key=lambda s: (len(s.shared_by), s.token_estimate), reverse=True)
        return [s.segment_id for s in sorted_segments]
