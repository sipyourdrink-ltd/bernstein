"""Cloudflare Vectorize semantic cache for LLM responses.

Caches LLM completions by embedding similarity -- if a new prompt is
semantically similar to a cached prompt, returns the cached response
instead of making another API call.  Uses Cloudflare Vectorize for
vector storage and Workers AI for embeddings.
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_PREVIEW_LEN = 200


@dataclass(frozen=True)
class VectorizeConfig:
    """Configuration for Vectorize semantic cache."""

    account_id: str
    api_token: str
    index_name: str = "bernstein-cache"
    embedding_model: str = "@cf/baai/bge-base-en-v1.5"
    similarity_threshold: float = 0.92
    max_cache_entries: int = 10_000
    ttl_seconds: int = 86_400  # 24 hours
    dimensions: int = 768  # bge-base-en-v1.5


@dataclass(frozen=True)
class CacheEntry:
    """A cached LLM response with metadata."""

    cache_id: str
    prompt_hash: str
    prompt_preview: str
    response: str
    model: str
    created_at: float
    hit_count: int = 0
    tokens_saved: int = 0


@dataclass(frozen=True)
class CacheLookupResult:
    """Result of a cache lookup."""

    hit: bool
    entry: CacheEntry | None = None
    similarity: float = 0.0
    lookup_ms: float = 0.0


@dataclass
class CacheStats:
    """Runtime cache statistics."""

    lookups: int = 0
    hits: int = 0
    misses: int = 0
    stores: int = 0
    total_tokens_saved: int = 0
    total_lookup_ms: float = 0.0

    @property
    def hit_rate(self) -> float:
        """Fraction of lookups that were cache hits."""
        return self.hits / self.lookups if self.lookups > 0 else 0.0

    @property
    def avg_lookup_ms(self) -> float:
        """Average lookup latency in milliseconds."""
        return self.total_lookup_ms / self.lookups if self.lookups > 0 else 0.0


def _prompt_hash(prompt: str) -> str:
    """Deterministic SHA-256 hash of a prompt string."""
    return hashlib.sha256(prompt.encode()).hexdigest()


class VectorizeSemanticCache:
    """Semantic cache using Cloudflare Vectorize + Workers AI embeddings.

    Usage::

        cache = VectorizeSemanticCache(VectorizeConfig(
            account_id="...", api_token="..."
        ))

        # Check cache before LLM call
        result = await cache.lookup(prompt="Decompose: add auth to API")
        if result.hit:
            return result.entry.response

        # After LLM call, store result
        await cache.store(prompt="...", response="...", model="opus")
    """

    def __init__(self, config: VectorizeConfig) -> None:
        self._config = config
        self._stats = CacheStats()

    @property
    def stats(self) -> CacheStats:
        """Return cache hit/miss statistics."""
        return self._stats

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def lookup(self, prompt: str) -> CacheLookupResult:
        """Look up a prompt in the semantic cache.

        Embeds the prompt, queries Vectorize for similar vectors, and
        returns the cached response if similarity exceeds the configured
        threshold.
        """
        start = time.monotonic()
        try:
            embedding = await self._embed(prompt)
            matches = await self._query_vectors(embedding, top_k=3)

            elapsed_ms = (time.monotonic() - start) * 1000
            self._stats.lookups += 1
            self._stats.total_lookup_ms += elapsed_ms

            if not matches:
                self._stats.misses += 1
                return CacheLookupResult(hit=False, lookup_ms=elapsed_ms)

            best = matches[0]
            score: float = best.get("score", 0.0)
            if score < self._config.similarity_threshold:
                self._stats.misses += 1
                return CacheLookupResult(hit=False, similarity=score, lookup_ms=elapsed_ms)

            meta: dict[str, Any] = best.get("metadata", {})
            tokens_saved = int(meta.get("tokens_used", 0))
            entry = CacheEntry(
                cache_id=str(best.get("id", "")),
                prompt_hash=str(meta.get("prompt_hash", "")),
                prompt_preview=str(meta.get("prompt_preview", "")),
                response=str(meta.get("response", "")),
                model=str(meta.get("model", "")),
                created_at=float(meta.get("created_at", 0.0)),
                hit_count=int(meta.get("hit_count", 0)) + 1,
                tokens_saved=tokens_saved,
            )
            self._stats.hits += 1
            self._stats.total_tokens_saved += tokens_saved
            return CacheLookupResult(hit=True, entry=entry, similarity=score, lookup_ms=elapsed_ms)
        except Exception:
            elapsed_ms = (time.monotonic() - start) * 1000
            self._stats.lookups += 1
            self._stats.misses += 1
            self._stats.total_lookup_ms += elapsed_ms
            logger.exception("Vectorize cache lookup failed")
            return CacheLookupResult(hit=False, lookup_ms=elapsed_ms)

    async def store(
        self,
        prompt: str,
        response: str,
        model: str = "",
        tokens_used: int = 0,
    ) -> str:
        """Store a prompt/response pair in the cache.

        Returns the ``cache_id`` for the stored entry.
        """
        phash = _prompt_hash(prompt)
        cache_id = phash[:16]
        embedding = await self._embed(prompt)
        now = time.time()

        metadata: dict[str, Any] = {
            "prompt_hash": phash,
            "prompt_preview": prompt[:_PREVIEW_LEN],
            "response": response,
            "model": model,
            "created_at": now,
            "hit_count": 0,
            "tokens_used": tokens_used,
        }

        await self._upsert_vector(cache_id, embedding, metadata)
        self._stats.stores += 1
        return cache_id

    async def invalidate(self, cache_id: str) -> bool:
        """Remove a specific cache entry.

        Returns ``True`` if the deletion request succeeded.
        """
        try:
            await self._delete_vector(cache_id)
            return True
        except Exception:
            logger.exception("Failed to invalidate cache entry %s", cache_id)
            return False

    async def clear(self) -> int:
        """Clear all cache entries.

        Returns the count of deleted entries (from the Vectorize
        ``mutationId`` response -- actual deletion is async on the
        Cloudflare side).
        """
        url = (
            f"https://api.cloudflare.com/client/v4/accounts/"
            f"{self._config.account_id}/vectorize/v2/indexes/"
            f"{self._config.index_name}/clear"
        )
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                headers=self._headers(),
                timeout=30.0,
            )
            resp.raise_for_status()
            body = resp.json()
            return int(body.get("result", {}).get("count", 0))

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self._config.api_token}",
            "Content-Type": "application/json",
        }

    async def _embed(self, text: str) -> list[float]:
        """Generate an embedding vector via Workers AI."""
        url = (
            f"https://api.cloudflare.com/client/v4/accounts/"
            f"{self._config.account_id}/ai/run/{self._config.embedding_model}"
        )
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                headers=self._headers(),
                json={"text": [text]},
                timeout=30.0,
            )
            resp.raise_for_status()
            body = resp.json()
            data: list[list[float]] = body.get("result", {}).get("data", [])
            if not data:
                msg = f"Workers AI returned no embeddings: {body}"
                raise ValueError(msg)
            return data[0]

    async def _query_vectors(self, vector: list[float], top_k: int = 3) -> list[dict[str, Any]]:
        """Query Vectorize index for nearest neighbours."""
        url = (
            f"https://api.cloudflare.com/client/v4/accounts/"
            f"{self._config.account_id}/vectorize/v2/indexes/"
            f"{self._config.index_name}/query"
        )
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                headers=self._headers(),
                json={"vector": vector, "topK": top_k, "returnMetadata": "all"},
                timeout=30.0,
            )
            resp.raise_for_status()
            body = resp.json()
            matches: list[dict[str, Any]] = body.get("result", {}).get("matches", [])
            return matches

    async def _upsert_vector(
        self,
        vector_id: str,
        vector: list[float],
        metadata: dict[str, Any],
    ) -> None:
        """Upsert a single vector with metadata to the Vectorize index."""
        url = (
            f"https://api.cloudflare.com/client/v4/accounts/"
            f"{self._config.account_id}/vectorize/v2/indexes/"
            f"{self._config.index_name}/upsert"
        )
        payload = {"vectors": [{"id": vector_id, "values": vector, "metadata": metadata}]}
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                headers=self._headers(),
                json=payload,
                timeout=30.0,
            )
            resp.raise_for_status()

    async def _delete_vector(self, vector_id: str) -> None:
        """Delete a vector from the Vectorize index."""
        url = (
            f"https://api.cloudflare.com/client/v4/accounts/"
            f"{self._config.account_id}/vectorize/v2/indexes/"
            f"{self._config.index_name}/delete"
        )
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                url,
                headers=self._headers(),
                json={"ids": [vector_id]},
                timeout=30.0,
            )
            resp.raise_for_status()
