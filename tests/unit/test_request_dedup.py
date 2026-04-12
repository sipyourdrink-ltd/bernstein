"""Tests for task server request deduplication."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from bernstein.core.request_dedup import (
    CachedResponse,
    DeduplicationConfig,
    RequestDeduplicator,
    extract_request_id,
    generate_request_id,
)

# ---------------------------------------------------------------------------
# CachedResponse
# ---------------------------------------------------------------------------


class TestCachedResponse:
    """Frozen dataclass smoke tests."""

    def test_fields_are_accessible(self) -> None:
        entry = CachedResponse(
            request_id="abc",
            status_code=200,
            body={"ok": True},
            created_at=1.0,
            ttl_s=60.0,
        )
        assert entry.request_id == "abc"
        assert entry.status_code == 200
        assert entry.body == {"ok": True}
        assert entry.created_at == pytest.approx(1.0)
        assert entry.ttl_s == pytest.approx(60.0)

    def test_is_frozen(self) -> None:
        entry = CachedResponse(
            request_id="abc",
            status_code=200,
            body={},
            created_at=1.0,
            ttl_s=60.0,
        )
        with pytest.raises(AttributeError):
            entry.status_code = 404  # type: ignore[misc]


# ---------------------------------------------------------------------------
# DeduplicationConfig
# ---------------------------------------------------------------------------


class TestDeduplicationConfig:
    """Config defaults and immutability."""

    def test_defaults(self) -> None:
        cfg = DeduplicationConfig()
        assert cfg.max_cache_size == 10_000
        assert cfg.default_ttl_s == pytest.approx(300.0)
        assert cfg.enabled is True

    def test_custom_values(self) -> None:
        cfg = DeduplicationConfig(max_cache_size=5, default_ttl_s=10.0, enabled=False)
        assert cfg.max_cache_size == 5
        assert cfg.default_ttl_s == pytest.approx(10.0)
        assert cfg.enabled is False

    def test_is_frozen(self) -> None:
        cfg = DeduplicationConfig()
        with pytest.raises(AttributeError):
            cfg.enabled = False  # type: ignore[misc]


# ---------------------------------------------------------------------------
# RequestDeduplicator
# ---------------------------------------------------------------------------


class TestRequestDeduplicator:
    """Core deduplicator behaviour."""

    def test_check_returns_none_on_miss(self) -> None:
        dedup = RequestDeduplicator()
        assert dedup.check("unknown") is None

    def test_store_then_check_returns_entry(self) -> None:
        dedup = RequestDeduplicator()
        dedup.store("req-1", 201, {"id": 42})
        cached = dedup.check("req-1")
        assert cached is not None
        assert cached.status_code == 201
        assert cached.body == {"id": 42}

    def test_is_duplicate_true_after_store(self) -> None:
        dedup = RequestDeduplicator()
        dedup.store("req-1", 200, {})
        assert dedup.is_duplicate("req-1") is True

    def test_is_duplicate_false_for_unknown(self) -> None:
        dedup = RequestDeduplicator()
        assert dedup.is_duplicate("nope") is False

    def test_ttl_expiry_on_check(self) -> None:
        dedup = RequestDeduplicator()
        with patch("bernstein.core.server.request_dedup.time") as mock_time:
            mock_time.time.return_value = 1000.0
            dedup.store("req-1", 200, {}, ttl_s=60.0)

            # Not expired yet.
            mock_time.time.return_value = 1059.0
            assert dedup.check("req-1") is not None

            # Expired.
            mock_time.time.return_value = 1060.0
            assert dedup.check("req-1") is None

    def test_zero_ttl_never_expires(self) -> None:
        dedup = RequestDeduplicator()
        with patch("bernstein.core.server.request_dedup.time") as mock_time:
            mock_time.time.return_value = 1000.0
            dedup.store("req-1", 200, {}, ttl_s=0)

            mock_time.time.return_value = 999_999.0
            assert dedup.check("req-1") is not None

    def test_evict_expired_removes_old_entries(self) -> None:
        dedup = RequestDeduplicator()
        with patch("bernstein.core.server.request_dedup.time") as mock_time:
            mock_time.time.return_value = 1000.0
            dedup.store("a", 200, {}, ttl_s=10.0)
            dedup.store("b", 200, {}, ttl_s=100.0)

            removed = dedup.evict_expired(now=1015.0)
            assert removed == 1
            assert dedup.check("a") is None
            # "b" still alive at now=1015.
            mock_time.time.return_value = 1015.0
            assert dedup.check("b") is not None

    def test_max_cache_size_evicts_oldest(self) -> None:
        cfg = DeduplicationConfig(max_cache_size=3, default_ttl_s=300.0)
        dedup = RequestDeduplicator(config=cfg)
        with patch("bernstein.core.server.request_dedup.time") as mock_time:
            mock_time.time.return_value = 100.0
            dedup.store("a", 200, {})
            mock_time.time.return_value = 200.0
            dedup.store("b", 200, {})
            mock_time.time.return_value = 300.0
            dedup.store("c", 200, {})

            # Cache full — adding "d" should evict "a" (oldest).
            mock_time.time.return_value = 400.0
            dedup.store("d", 200, {})

            assert dedup.check("a") is None  # evicted
            assert dedup.check("b") is not None
            assert dedup.check("c") is not None
            assert dedup.check("d") is not None

    def test_store_updates_existing_key_without_eviction(self) -> None:
        cfg = DeduplicationConfig(max_cache_size=2)
        dedup = RequestDeduplicator(config=cfg)
        dedup.store("x", 200, {"v": 1})
        dedup.store("y", 200, {"v": 2})

        # Overwrite "x" — should NOT evict "y".
        dedup.store("x", 201, {"v": 3})
        cached = dedup.check("x")
        assert cached is not None
        assert cached.status_code == 201
        assert dedup.check("y") is not None

    def test_stats_returns_expected_keys(self) -> None:
        dedup = RequestDeduplicator()
        dedup.store("r1", 200, {})
        dedup.check("r1")  # hit
        dedup.check("r2")  # miss

        s = dedup.stats()
        assert s["total"] == 1
        assert s["hits"] == 1
        assert s["misses"] == 1
        assert "expired" in s

    def test_clear_resets_everything(self) -> None:
        dedup = RequestDeduplicator()
        dedup.store("r1", 200, {})
        dedup.check("r1")
        dedup.clear()

        assert dedup.check("r1") is None
        s = dedup.stats()
        assert s["total"] == 0
        assert s["hits"] == 0
        assert s["misses"] == 1  # the check after clear counts

    def test_disabled_config_always_returns_none(self) -> None:
        cfg = DeduplicationConfig(enabled=False)
        dedup = RequestDeduplicator(config=cfg)
        entry = dedup.store("r1", 200, {"ok": True})
        assert entry.request_id == "r1"  # store returns the entry
        assert dedup.check("r1") is None  # but check always misses
        assert dedup.is_duplicate("r1") is False


# ---------------------------------------------------------------------------
# Header helpers
# ---------------------------------------------------------------------------


class TestExtractRequestId:
    """extract_request_id from header dicts."""

    def test_x_request_id(self) -> None:
        assert extract_request_id({"X-Request-ID": "abc"}) == "abc"

    def test_x_idempotency_key(self) -> None:
        assert extract_request_id({"X-Idempotency-Key": "def"}) == "def"

    def test_request_id_takes_precedence(self) -> None:
        headers = {"X-Request-ID": "first", "X-Idempotency-Key": "second"}
        assert extract_request_id(headers) == "first"

    def test_case_insensitive(self) -> None:
        assert extract_request_id({"x-request-id": "low"}) == "low"
        assert extract_request_id({"X-REQUEST-ID": "up"}) == "up"

    def test_returns_none_when_absent(self) -> None:
        assert extract_request_id({}) is None
        assert extract_request_id({"Content-Type": "application/json"}) is None


class TestGenerateRequestId:
    """generate_request_id returns valid UUID-4 strings."""

    def test_returns_string(self) -> None:
        rid = generate_request_id()
        assert isinstance(rid, str)
        assert len(rid) == 36  # UUID-4 canonical form

    def test_unique_each_call(self) -> None:
        ids = {generate_request_id() for _ in range(50)}
        assert len(ids) == 50
