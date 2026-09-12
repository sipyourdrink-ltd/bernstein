"""Tests for the chain-projected lane registry and content store (#5120)."""

from __future__ import annotations

import pytest

from bernstein.core.govern.lane_registry import (
    LaneRegistry,
    LaneStore,
    LaneStoreError,
    project_lane_registry,
)
from bernstein.core.govern.lanes import LaneManifest


def _lane(name: str, timeout: int = 900) -> LaneManifest:
    return LaneManifest(
        name=name,
        selector="team:payments",
        schedule="*/15 * * * *",
        log_destination="s3://logs/lanes",
        timeout_seconds=timeout,
    )


def _ev(event_type: str, name: str, lane_hash: str) -> dict:
    return {"event_type": event_type, "details": {"lane_name": name, "lane_hash": lane_hash}}


class TestProjection:
    def test_register_then_active(self):
        lane = _lane("a")
        active = project_lane_registry([_ev("lane.registered", "a", lane.lane_hash)])
        assert active == {"a": lane.lane_hash}

    def test_update_supersedes(self):
        l1, l2 = _lane("a", 900), _lane("a", 1800)
        events = [
            _ev("lane.registered", "a", l1.lane_hash),
            _ev("lane.updated", "a", l2.lane_hash),
        ]
        assert project_lane_registry(events) == {"a": l2.lane_hash}

    def test_retire_drops(self):
        lane = _lane("a")
        events = [
            _ev("lane.registered", "a", lane.lane_hash),
            _ev("lane.retired", "a", lane.lane_hash),
        ]
        assert project_lane_registry(events) == {}

    def test_non_lane_events_ignored(self):
        lane = _lane("a")
        events = [
            {"event_type": "cache.hit", "details": {"cache_key": "x"}},
            _ev("lane.registered", "a", lane.lane_hash),
        ]
        assert project_lane_registry(events) == {"a": lane.lane_hash}

    def test_projection_is_deterministic(self):
        l1, l2 = _lane("a"), _lane("b")
        events = [_ev("lane.registered", "a", l1.lane_hash), _ev("lane.registered", "b", l2.lane_hash)]
        assert project_lane_registry(events) == project_lane_registry(events)


class TestContentStore:
    def test_put_get_roundtrip(self, tmp_path):
        store = LaneStore(root=tmp_path)
        lane = _lane("a")
        store.put(lane)
        loaded = store.get(lane.lane_hash)
        assert loaded.lane_hash == lane.lane_hash

    def test_tampered_body_refused(self, tmp_path):
        store = LaneStore(root=tmp_path)
        lane = _lane("a")
        path = store.put(lane)
        text = path.read_text().replace('"timeout_seconds":900', '"timeout_seconds":1')
        assert '"timeout_seconds":1' in text
        path.write_text(text)
        with pytest.raises(LaneStoreError):
            store.get(lane.lane_hash)

    def test_missing_body_refused(self, tmp_path):
        store = LaneStore(root=tmp_path)
        with pytest.raises(LaneStoreError):
            store.get("a" * 64)

    def test_non_canonical_hash_refused(self, tmp_path):
        store = LaneStore(root=tmp_path)
        with pytest.raises(LaneStoreError):
            store.get("../etc/passwd")


class TestRegistry:
    def test_registry_resolves_active_body(self, tmp_path):
        store = LaneStore(root=tmp_path)
        lane = _lane("a")
        store.put(lane)
        registry = LaneRegistry.from_events([_ev("lane.registered", "a", lane.lane_hash)], store)
        assert registry.names() == ["a"]
        assert registry.get("a").lane_hash == lane.lane_hash

    def test_retired_lane_returns_none(self, tmp_path):
        store = LaneStore(root=tmp_path)
        lane = _lane("a")
        store.put(lane)
        events = [_ev("lane.registered", "a", lane.lane_hash), _ev("lane.retired", "a", lane.lane_hash)]
        registry = LaneRegistry.from_events(events, store)
        assert registry.get("a") is None
