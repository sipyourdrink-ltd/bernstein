"""Tests for Cloudflare Vectorize semantic cache."""

from __future__ import annotations

import hashlib
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from bernstein.core.memory.vectorize_cache import (
    CacheEntry,
    CacheLookupResult,
    CacheStats,
    VectorizeConfig,
    VectorizeSemanticCache,
    _prompt_hash,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

FAKE_ACCOUNT = "test-account-id"
FAKE_TOKEN = "test-api-token"
FAKE_EMBEDDING = [0.1] * 768


@pytest.fixture()
def config() -> VectorizeConfig:
    return VectorizeConfig(account_id=FAKE_ACCOUNT, api_token=FAKE_TOKEN)


@pytest.fixture()
def cache(config: VectorizeConfig) -> VectorizeSemanticCache:
    return VectorizeSemanticCache(config)


def _ok_response(body: dict[str, Any], status: int = 200) -> httpx.Response:
    """Build a fake httpx.Response with JSON body."""
    return httpx.Response(
        status_code=status,
        json=body,
        request=httpx.Request("POST", "https://fake"),
    )


# ---------------------------------------------------------------------------
# VectorizeConfig
# ---------------------------------------------------------------------------


class TestVectorizeConfig:
    def test_defaults(self) -> None:
        cfg = VectorizeConfig(account_id="a", api_token="t")
        assert cfg.index_name == "bernstein-cache"
        assert cfg.embedding_model == "@cf/baai/bge-base-en-v1.5"
        assert cfg.similarity_threshold == 0.92
        assert cfg.max_cache_entries == 10_000
        assert cfg.ttl_seconds == 86_400
        assert cfg.dimensions == 768

    def test_custom_values(self) -> None:
        cfg = VectorizeConfig(
            account_id="x",
            api_token="y",
            index_name="custom",
            similarity_threshold=0.8,
            dimensions=1024,
        )
        assert cfg.index_name == "custom"
        assert cfg.similarity_threshold == 0.8
        assert cfg.dimensions == 1024


# ---------------------------------------------------------------------------
# CacheStats
# ---------------------------------------------------------------------------


class TestCacheStats:
    def test_hit_rate_zero_lookups(self) -> None:
        stats = CacheStats()
        assert stats.hit_rate == 0.0

    def test_hit_rate_nonzero(self) -> None:
        stats = CacheStats(lookups=10, hits=3)
        assert stats.hit_rate == pytest.approx(0.3)

    def test_avg_lookup_ms_zero(self) -> None:
        stats = CacheStats()
        assert stats.avg_lookup_ms == 0.0

    def test_avg_lookup_ms_nonzero(self) -> None:
        stats = CacheStats(lookups=4, total_lookup_ms=100.0)
        assert stats.avg_lookup_ms == pytest.approx(25.0)


# ---------------------------------------------------------------------------
# CacheEntry
# ---------------------------------------------------------------------------


class TestCacheEntry:
    def test_creation(self) -> None:
        entry = CacheEntry(
            cache_id="abc",
            prompt_hash="h1",
            prompt_preview="hello",
            response="world",
            model="opus",
            created_at=1.0,
        )
        assert entry.cache_id == "abc"
        assert entry.hit_count == 0
        assert entry.tokens_saved == 0

    def test_frozen(self) -> None:
        entry = CacheEntry(
            cache_id="x",
            prompt_hash="h",
            prompt_preview="p",
            response="r",
            model="m",
            created_at=0.0,
        )
        with pytest.raises(AttributeError):
            entry.cache_id = "y"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# _prompt_hash
# ---------------------------------------------------------------------------


class TestPromptHash:
    def test_deterministic(self) -> None:
        assert _prompt_hash("hello") == _prompt_hash("hello")

    def test_different_inputs(self) -> None:
        assert _prompt_hash("a") != _prompt_hash("b")

    def test_sha256(self) -> None:
        expected = hashlib.sha256(b"test").hexdigest()
        assert _prompt_hash("test") == expected


# ---------------------------------------------------------------------------
# Lookup
# ---------------------------------------------------------------------------


class TestLookup:
    @pytest.mark.asyncio()
    async def test_cache_miss_no_matches(self, cache: VectorizeSemanticCache) -> None:
        """No vectors returned => cache miss."""
        embed_resp = _ok_response({"result": {"data": [FAKE_EMBEDDING]}})
        query_resp = _ok_response({"result": {"matches": []}})

        with patch("httpx.AsyncClient") as mock_cls:
            client = AsyncMock()
            client.post = AsyncMock(side_effect=[embed_resp, query_resp])
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = client

            result = await cache.lookup("some prompt")

        assert result.hit is False
        assert result.entry is None
        assert cache.stats.lookups == 1
        assert cache.stats.misses == 1

    @pytest.mark.asyncio()
    async def test_cache_hit(self, cache: VectorizeSemanticCache) -> None:
        """Best match above threshold => cache hit."""
        meta = {
            "prompt_hash": "h123",
            "prompt_preview": "decompose",
            "response": "cached answer",
            "model": "opus",
            "created_at": 1000.0,
            "hit_count": 2,
            "tokens_used": 500,
        }
        embed_resp = _ok_response({"result": {"data": [FAKE_EMBEDDING]}})
        query_resp = _ok_response({"result": {"matches": [{"id": "v1", "score": 0.95, "metadata": meta}]}})

        with patch("httpx.AsyncClient") as mock_cls:
            client = AsyncMock()
            client.post = AsyncMock(side_effect=[embed_resp, query_resp])
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = client

            result = await cache.lookup("decompose task")

        assert result.hit is True
        assert result.entry is not None
        assert result.entry.response == "cached answer"
        assert result.entry.hit_count == 3  # incremented
        assert result.similarity == pytest.approx(0.95)
        assert cache.stats.hits == 1
        assert cache.stats.total_tokens_saved == 500

    @pytest.mark.asyncio()
    async def test_cache_near_miss(self, cache: VectorizeSemanticCache) -> None:
        """Best match below threshold => cache miss."""
        embed_resp = _ok_response({"result": {"data": [FAKE_EMBEDDING]}})
        query_resp = _ok_response(
            {"result": {"matches": [{"id": "v1", "score": 0.85, "metadata": {"response": "old"}}]}}
        )

        with patch("httpx.AsyncClient") as mock_cls:
            client = AsyncMock()
            client.post = AsyncMock(side_effect=[embed_resp, query_resp])
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = client

            result = await cache.lookup("near miss prompt")

        assert result.hit is False
        assert result.similarity == pytest.approx(0.85)
        assert cache.stats.misses == 1

    @pytest.mark.asyncio()
    async def test_lookup_api_error(self, cache: VectorizeSemanticCache) -> None:
        """API failure => graceful miss, not exception."""
        with patch("httpx.AsyncClient") as mock_cls:
            client = AsyncMock()
            client.post = AsyncMock(side_effect=httpx.ConnectError("down"))
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = client

            result = await cache.lookup("will fail")

        assert result.hit is False
        assert cache.stats.misses == 1


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class TestStore:
    @pytest.mark.asyncio()
    async def test_store_returns_cache_id(self, cache: VectorizeSemanticCache) -> None:
        embed_resp = _ok_response({"result": {"data": [FAKE_EMBEDDING]}})
        upsert_resp = _ok_response({"result": {"mutationId": "m1"}})

        with patch("httpx.AsyncClient") as mock_cls:
            client = AsyncMock()
            client.post = AsyncMock(side_effect=[embed_resp, upsert_resp])
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = client

            cache_id = await cache.store(
                prompt="hello world",
                response="answer",
                model="opus",
                tokens_used=42,
            )

        expected_hash = _prompt_hash("hello world")[:16]
        assert cache_id == expected_hash
        assert cache.stats.stores == 1

    @pytest.mark.asyncio()
    async def test_store_sends_metadata(self, cache: VectorizeSemanticCache) -> None:
        embed_resp = _ok_response({"result": {"data": [FAKE_EMBEDDING]}})
        upsert_resp = _ok_response({"result": {}})

        with patch("httpx.AsyncClient") as mock_cls:
            client = AsyncMock()
            client.post = AsyncMock(side_effect=[embed_resp, upsert_resp])
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = client

            await cache.store(prompt="p", response="r", model="haiku", tokens_used=10)

        # Second call is the upsert
        upsert_call = client.post.call_args_list[1]
        payload = upsert_call.kwargs.get("json") or upsert_call[1].get("json")
        vec = payload["vectors"][0]
        assert vec["metadata"]["model"] == "haiku"
        assert vec["metadata"]["tokens_used"] == 10
        assert vec["metadata"]["prompt_preview"] == "p"


# ---------------------------------------------------------------------------
# Invalidate
# ---------------------------------------------------------------------------


class TestInvalidate:
    @pytest.mark.asyncio()
    async def test_invalidate_success(self, cache: VectorizeSemanticCache) -> None:
        delete_resp = _ok_response({"result": {}})

        with patch("httpx.AsyncClient") as mock_cls:
            client = AsyncMock()
            client.post = AsyncMock(return_value=delete_resp)
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = client

            ok = await cache.invalidate("some-id")

        assert ok is True

    @pytest.mark.asyncio()
    async def test_invalidate_failure(self, cache: VectorizeSemanticCache) -> None:
        with patch("httpx.AsyncClient") as mock_cls:
            client = AsyncMock()
            client.post = AsyncMock(
                side_effect=httpx.HTTPStatusError(
                    "404", request=httpx.Request("POST", "https://x"), response=httpx.Response(404)
                )
            )
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = client

            ok = await cache.invalidate("bad-id")

        assert ok is False


# ---------------------------------------------------------------------------
# _embed
# ---------------------------------------------------------------------------


class TestEmbed:
    @pytest.mark.asyncio()
    async def test_embed_calls_workers_ai(self, cache: VectorizeSemanticCache) -> None:
        embed_resp = _ok_response({"result": {"data": [[0.5, 0.6]]}})

        with patch("httpx.AsyncClient") as mock_cls:
            client = AsyncMock()
            client.post = AsyncMock(return_value=embed_resp)
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = client

            result = await cache._embed("test text")

        assert result == [0.5, 0.6]
        call_kwargs = client.post.call_args
        assert FAKE_ACCOUNT in str(call_kwargs)
        assert call_kwargs.kwargs.get("json") == {"text": ["test text"]}

    @pytest.mark.asyncio()
    async def test_embed_no_data_raises(self, cache: VectorizeSemanticCache) -> None:
        empty_resp = _ok_response({"result": {"data": []}})

        with patch("httpx.AsyncClient") as mock_cls:
            client = AsyncMock()
            client.post = AsyncMock(return_value=empty_resp)
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = client

            with pytest.raises(ValueError, match="no embeddings"):
                await cache._embed("bad")


# ---------------------------------------------------------------------------
# _query_vectors
# ---------------------------------------------------------------------------


class TestQueryVectors:
    @pytest.mark.asyncio()
    async def test_query_returns_matches(self, cache: VectorizeSemanticCache) -> None:
        matches = [{"id": "a", "score": 0.9}, {"id": "b", "score": 0.8}]
        resp = _ok_response({"result": {"matches": matches}})

        with patch("httpx.AsyncClient") as mock_cls:
            client = AsyncMock()
            client.post = AsyncMock(return_value=resp)
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = client

            result = await cache._query_vectors([0.1, 0.2], top_k=2)

        assert len(result) == 2
        assert result[0]["id"] == "a"

    @pytest.mark.asyncio()
    async def test_query_sends_correct_payload(self, cache: VectorizeSemanticCache) -> None:
        resp = _ok_response({"result": {"matches": []}})

        with patch("httpx.AsyncClient") as mock_cls:
            client = AsyncMock()
            client.post = AsyncMock(return_value=resp)
            client.__aenter__ = AsyncMock(return_value=client)
            client.__aexit__ = AsyncMock(return_value=False)
            mock_cls.return_value = client

            await cache._query_vectors([1.0, 2.0], top_k=5)

        payload = client.post.call_args.kwargs.get("json")
        assert payload["vector"] == [1.0, 2.0]
        assert payload["topK"] == 5
        assert payload["returnMetadata"] == "all"


# ---------------------------------------------------------------------------
# CacheLookupResult
# ---------------------------------------------------------------------------


class TestCacheLookupResult:
    def test_miss_defaults(self) -> None:
        r = CacheLookupResult(hit=False)
        assert r.entry is None
        assert r.similarity == 0.0
        assert r.lookup_ms == 0.0

    def test_frozen(self) -> None:
        r = CacheLookupResult(hit=True)
        with pytest.raises(AttributeError):
            r.hit = False  # type: ignore[misc]
