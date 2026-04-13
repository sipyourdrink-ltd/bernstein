"""Tests for bernstein.core.policy_limits."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from bernstein.core.policy_limits import (
    ESSENTIAL_TRAFFIC_DENY_ON_MISS,
    PolicyLimitEntry,
    PolicyLimitsClient,
    PolicyLimitsSnapshot,
    _parse_payload,
    _read_cache,
    _write_cache,
    is_allowed_sync,
    managed_policy_limits,
)

# ---------------------------------------------------------------------------
# PolicyLimitEntry
# ---------------------------------------------------------------------------


class TestPolicyLimitEntry:
    def test_from_dict_minimal(self) -> None:
        entry = PolicyLimitEntry.from_dict({"feature": "some_feature", "enabled": True})
        assert entry.feature == "some_feature"
        assert entry.enabled is True
        assert entry.metadata == {}

    def test_from_dict_defaults_enabled_to_true(self) -> None:
        entry = PolicyLimitEntry.from_dict({"feature": "x"})
        assert entry.enabled is True

    def test_round_trip(self) -> None:
        entry = PolicyLimitEntry(feature="foo", enabled=False, metadata={"reason": "hipaa"})
        restored = PolicyLimitEntry.from_dict(entry.to_dict())
        assert restored == entry


# ---------------------------------------------------------------------------
# PolicyLimitsSnapshot
# ---------------------------------------------------------------------------


class TestPolicyLimitsSnapshot:
    def test_age_seconds_none_when_never_fetched(self) -> None:
        snapshot = PolicyLimitsSnapshot()
        assert snapshot.age_seconds is None

    def test_age_seconds_approximate(self) -> None:
        ts = datetime.now(UTC)
        snapshot = PolicyLimitsSnapshot(fetched_at=ts)
        age = snapshot.age_seconds
        assert age is not None
        assert 0 <= age < 2

    def test_round_trip(self) -> None:
        entry = PolicyLimitEntry(feature="f1", enabled=True)
        snapshot = PolicyLimitsSnapshot(
            limits={"f1": entry},
            etag='"abc123"',
            fetched_at=datetime(2025, 1, 1, 12, 0, 0, tzinfo=UTC),
        )
        restored = PolicyLimitsSnapshot.from_dict(snapshot.to_dict())
        assert restored.etag == '"abc123"'
        assert "f1" in restored.limits
        assert restored.limits["f1"].enabled is True
        assert restored.fetched_at == snapshot.fetched_at

    def test_from_dict_bad_fetched_at_is_none(self) -> None:
        data = {"etag": None, "fetched_at": "not-a-date", "limits": {}}
        snapshot = PolicyLimitsSnapshot.from_dict(data)
        assert snapshot.fetched_at is None

    def test_from_dict_missing_keys(self) -> None:
        snapshot = PolicyLimitsSnapshot.from_dict({})
        assert snapshot.limits == {}
        assert snapshot.etag is None


# ---------------------------------------------------------------------------
# Cache I/O
# ---------------------------------------------------------------------------


class TestCacheIO:
    def test_read_missing_returns_none(self, tmp_path: Path) -> None:
        result = _read_cache(tmp_path / "nonexistent.json")
        assert result is None

    def test_write_then_read(self, tmp_path: Path) -> None:
        cache_path = tmp_path / "policy-limits.json"
        snapshot = PolicyLimitsSnapshot(
            limits={"feat": PolicyLimitEntry(feature="feat", enabled=False)},
            etag='"etag1"',
            fetched_at=datetime(2025, 6, 15, tzinfo=UTC),
        )
        _write_cache(cache_path, snapshot)
        restored = _read_cache(cache_path)
        assert restored is not None
        assert restored.etag == '"etag1"'
        assert "feat" in restored.limits
        assert restored.limits["feat"].enabled is False

    def test_read_invalid_json_returns_none(self, tmp_path: Path) -> None:
        cache_path = tmp_path / "bad.json"
        cache_path.write_text("not-json", encoding="utf-8")
        assert _read_cache(cache_path) is None

    def test_read_wrong_type_returns_none(self, tmp_path: Path) -> None:
        cache_path = tmp_path / "list.json"
        cache_path.write_text("[1, 2, 3]", encoding="utf-8")
        assert _read_cache(cache_path) is None

    def test_write_creates_parent_dirs(self, tmp_path: Path) -> None:
        nested = tmp_path / "a" / "b" / "c" / "policy-limits.json"
        _write_cache(nested, PolicyLimitsSnapshot())
        assert nested.exists()


# ---------------------------------------------------------------------------
# _parse_payload
# ---------------------------------------------------------------------------


class TestParsePayload:
    def test_parses_limits_list(self) -> None:
        payload: dict[str, Any] = {
            "limits": [
                {"feature": "allow_product_feedback", "enabled": False},
                {"feature": "allow_telemetry", "enabled": True},
            ]
        }
        result = _parse_payload(payload)
        assert result["allow_product_feedback"].enabled is False
        assert result["allow_telemetry"].enabled is True

    def test_empty_limits(self) -> None:
        assert _parse_payload({"limits": []}) == {}

    def test_missing_limits_key(self) -> None:
        assert _parse_payload({}) == {}

    def test_skips_malformed_entries(self) -> None:
        payload: dict[str, Any] = {
            "limits": [
                "not-a-dict",
                {"feature": "good", "enabled": True},
            ]
        }
        result = _parse_payload(payload)
        assert list(result.keys()) == ["good"]


# ---------------------------------------------------------------------------
# is_allowed_sync
# ---------------------------------------------------------------------------


class TestIsAllowedSync:
    def test_none_snapshot_fail_open(self) -> None:
        assert is_allowed_sync("some_feature", snapshot=None) is True

    def test_none_snapshot_deny_on_miss_feature(self) -> None:
        assert is_allowed_sync("allow_product_feedback", snapshot=None) is False

    def test_feature_present_enabled(self) -> None:
        snapshot = PolicyLimitsSnapshot(limits={"f": PolicyLimitEntry(feature="f", enabled=True)})
        assert is_allowed_sync("f", snapshot=snapshot) is True

    def test_feature_present_disabled(self) -> None:
        snapshot = PolicyLimitsSnapshot(limits={"f": PolicyLimitEntry(feature="f", enabled=False)})
        assert is_allowed_sync("f", snapshot=snapshot) is False

    def test_feature_absent_fail_open(self) -> None:
        snapshot = PolicyLimitsSnapshot(limits={})
        assert is_allowed_sync("unknown_feature", snapshot=snapshot) is True

    def test_feature_absent_deny_on_miss(self) -> None:
        snapshot = PolicyLimitsSnapshot(limits={})
        assert is_allowed_sync("allow_product_feedback", snapshot=snapshot) is False


# ---------------------------------------------------------------------------
# ESSENTIAL_TRAFFIC_DENY_ON_MISS
# ---------------------------------------------------------------------------


class TestDenyOnMissConstant:
    def test_contains_allow_product_feedback(self) -> None:
        assert "allow_product_feedback" in ESSENTIAL_TRAFFIC_DENY_ON_MISS

    def test_is_frozenset(self) -> None:
        assert isinstance(ESSENTIAL_TRAFFIC_DENY_ON_MISS, frozenset)


# ---------------------------------------------------------------------------
# PolicyLimitsClient
# ---------------------------------------------------------------------------


class TestPolicyLimitsClient:
    def test_is_allowed_fail_open_before_init(self) -> None:
        client = PolicyLimitsClient()
        assert client.is_allowed("some_arbitrary_feature") is True

    def test_is_allowed_deny_on_miss_before_init(self) -> None:
        client = PolicyLimitsClient()
        assert client.is_allowed("allow_product_feedback") is False

    def test_is_allowed_returns_policy_value(self) -> None:
        client = PolicyLimitsClient()
        client._snapshot = PolicyLimitsSnapshot(
            limits={"allow_product_feedback": PolicyLimitEntry(feature="allow_product_feedback", enabled=True)}
        )
        assert client.is_allowed("allow_product_feedback") is True

    def test_is_allowed_respects_disabled_entry(self) -> None:
        client = PolicyLimitsClient()
        client._snapshot = PolicyLimitsSnapshot(limits={"x": PolicyLimitEntry(feature="x", enabled=False)})
        assert client.is_allowed("x") is False

    @pytest.mark.asyncio
    async def test_initialize_loads_from_cache(self, tmp_path: Path) -> None:
        cache_path = tmp_path / "policy-limits.json"
        snapshot = PolicyLimitsSnapshot(
            limits={"cached_feature": PolicyLimitEntry(feature="cached_feature", enabled=False)},
            etag='"cached"',
            fetched_at=datetime.now(UTC),
        )
        _write_cache(cache_path, snapshot)

        # Patch _fetch_limits_from_api to return None (simulate unreachable)
        with patch(
            "bernstein.core.policy_limits._fetch_limits_from_api",
            new=AsyncMock(return_value=(None, None)),
        ):
            client = PolicyLimitsClient(cache_dir=tmp_path)
            await client.initialize()

        assert client.is_allowed("cached_feature") is False

    @pytest.mark.asyncio
    async def test_initialize_fetches_fresh_data(self, tmp_path: Path) -> None:
        payload: dict[str, Any] = {"limits": [{"feature": "new_feature", "enabled": False}]}
        with patch(
            "bernstein.core.policy_limits._fetch_limits_from_api",
            new=AsyncMock(return_value=(payload, '"fresh-etag"')),
        ):
            client = PolicyLimitsClient(cache_dir=tmp_path)
            await client.initialize()

        assert client.is_allowed("new_feature") is False
        assert client._snapshot.etag == '"fresh-etag"'

        # Cache should be written
        cache_file = tmp_path / "policy-limits.json"
        assert cache_file.exists()
        cached_data = json.loads(cache_file.read_text())
        assert cached_data["etag"] == '"fresh-etag"'

    @pytest.mark.asyncio
    async def test_initialize_timeout_falls_back_to_fail_open(self, tmp_path: Path) -> None:
        async def slow_fetch(*_: Any, **__: Any) -> tuple[None, None]:
            await asyncio.sleep(100)
            return None, None

        with patch(
            "bernstein.core.policy_limits._fetch_limits_from_api",
            new=slow_fetch,
        ):
            client = PolicyLimitsClient(cache_dir=tmp_path, init_timeout=0.05)
            await client.initialize()

        # Should still be initialized and fail-open
        assert client._initialized is True
        assert client.is_allowed("some_feature") is True

    @pytest.mark.asyncio
    async def test_initialize_network_error_uses_cache(self, tmp_path: Path) -> None:
        cache_path = tmp_path / "policy-limits.json"
        snapshot = PolicyLimitsSnapshot(
            limits={"f": PolicyLimitEntry(feature="f", enabled=False)},
            etag='"e1"',
            fetched_at=datetime.now(UTC),
        )
        _write_cache(cache_path, snapshot)

        with patch(
            "bernstein.core.policy_limits._fetch_limits_from_api",
            new=AsyncMock(side_effect=OSError("connection refused")),
        ):
            client = PolicyLimitsClient(cache_dir=tmp_path)
            await client.initialize()

        assert client.is_allowed("f") is False

    @pytest.mark.asyncio
    async def test_start_stop_background_polling(self, tmp_path: Path) -> None:
        with patch(
            "bernstein.core.policy_limits._fetch_limits_from_api",
            new=AsyncMock(return_value=(None, None)),
        ):
            client = PolicyLimitsClient(cache_dir=tmp_path, poll_interval=0.05)
            await client.initialize()
            client.start_background_polling()
            assert client._poll_task is not None
            assert not client._poll_task.done()

            # Second call is idempotent
            task_before = client._poll_task
            client.start_background_polling()
            assert client._poll_task is task_before

            client.stop_background_polling()
            assert client._poll_task is None

    @pytest.mark.asyncio
    async def test_background_polling_refreshes(self, tmp_path: Path) -> None:
        call_count = 0

        async def counted_fetch(*_: Any, **__: Any) -> tuple[dict[str, Any], str]:
            await asyncio.sleep(0)  # Async interface requirement
            nonlocal call_count
            call_count += 1
            return {"limits": [{"feature": "polled", "enabled": True}]}, '"e"'

        with patch(
            "bernstein.core.policy_limits._fetch_limits_from_api",
            new=counted_fetch,
        ):
            client = PolicyLimitsClient(cache_dir=tmp_path, poll_interval=0.05)
            await client.initialize()
            init_count = call_count

            client.start_background_polling()
            await asyncio.sleep(0.15)  # allow ~2 background refreshes
            client.stop_background_polling()

        assert call_count > init_count

    @pytest.mark.asyncio
    async def test_etag_sent_on_subsequent_fetch(self, tmp_path: Path) -> None:
        received_etags: list[str | None] = []

        async def capture_etag(url: str, etag: str | None = None, **_: Any) -> tuple[dict[str, Any], str]:
            await asyncio.sleep(0)  # Async interface requirement
            received_etags.append(etag)
            return {"limits": []}, '"new-etag"'

        with patch(
            "bernstein.core.policy_limits._fetch_limits_from_api",
            new=capture_etag,
        ):
            client = PolicyLimitsClient(cache_dir=tmp_path)
            # First fetch — no etag
            await client._refresh()
            # Second fetch — should send the etag from first
            await client._refresh()

        assert received_etags[0] is None
        assert received_etags[1] == '"new-etag"'

    def test_get_snapshot_returns_current(self) -> None:
        client = PolicyLimitsClient()
        snap = client.get_snapshot()
        assert isinstance(snap, PolicyLimitsSnapshot)

    @pytest.mark.asyncio
    async def test_start_polling_without_event_loop(self, tmp_path: Path) -> None:
        """stop_background_polling is safe when no task was started."""
        client = PolicyLimitsClient(cache_dir=tmp_path)
        # No loop running when this is called outside async context
        client.stop_background_polling()  # should not raise


# ---------------------------------------------------------------------------
# managed_policy_limits context manager
# ---------------------------------------------------------------------------


class TestManagedPolicyLimits:
    @pytest.mark.asyncio
    async def test_basic_usage(self, tmp_path: Path) -> None:
        payload: dict[str, Any] = {"limits": [{"feature": "managed_feat", "enabled": False}]}
        with patch(
            "bernstein.core.policy_limits._fetch_limits_from_api",
            new=AsyncMock(return_value=(payload, '"etag"')),
        ):
            async with managed_policy_limits(cache_dir=tmp_path, poll=False) as client:
                assert client.is_allowed("managed_feat") is False

    @pytest.mark.asyncio
    async def test_polling_stopped_on_exit(self, tmp_path: Path) -> None:
        with patch(
            "bernstein.core.policy_limits._fetch_limits_from_api",
            new=AsyncMock(return_value=({"limits": []}, '"e"')),
        ):
            async with managed_policy_limits(cache_dir=tmp_path, poll=True) as client:
                task = client._poll_task

        # After exit the client no longer holds a reference to the task.
        assert client._poll_task is None
        # Give the event loop one cycle to process the cancellation.
        await asyncio.sleep(0)
        if task is not None:
            assert task.cancelled() or task.done() or task.cancelling() > 0
