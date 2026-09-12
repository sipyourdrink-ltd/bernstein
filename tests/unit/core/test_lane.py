"""Tests for LaneManifest and LaneStore (#5120)."""

from __future__ import annotations

import json

import pytest

from bernstein.core.governance.lane import (
    LANE_MANIFEST_SCHEMA_VERSION,
    LaneError,
    LaneManifest,
    LaneStore,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def minimal_manifest() -> LaneManifest:
    return LaneManifest(
        lane_id="urgent",
        priority=10,
        max_concurrency=2,
        target_class="drift-correction",
        created_at="2025-01-01T00:00:00Z",
    )


@pytest.fixture()
def store(tmp_path: pytest.TempdirFixture) -> LaneStore:
    return LaneStore(tmp_path / "lanes.json")


# ---------------------------------------------------------------------------
# LaneManifest -- construction and validation
# ---------------------------------------------------------------------------


class TestLaneManifest:
    """LaneManifest dataclass behaviour."""

    def test_valid_manifest_constructs(self, minimal_manifest: LaneManifest) -> None:
        assert minimal_manifest.lane_id == "urgent"
        assert minimal_manifest.priority == 10
        assert minimal_manifest.max_concurrency == 2

    def test_empty_lane_id_raises(self) -> None:
        with pytest.raises(LaneError, match="lane_id"):
            LaneManifest(lane_id="", priority=5, max_concurrency=1, target_class="", created_at="2025-01-01T00:00:00Z")

    def test_whitespace_lane_id_raises(self) -> None:
        with pytest.raises(LaneError, match="lane_id"):
            LaneManifest(lane_id="a b", priority=5, max_concurrency=1, target_class="", created_at="2025-01-01T00:00:00Z")

    def test_priority_below_zero_raises(self) -> None:
        with pytest.raises(LaneError, match="priority"):
            LaneManifest(lane_id="x", priority=-1, max_concurrency=1, target_class="", created_at="2025-01-01T00:00:00Z")

    def test_priority_above_100_raises(self) -> None:
        with pytest.raises(LaneError, match="priority"):
            LaneManifest(lane_id="x", priority=101, max_concurrency=1, target_class="", created_at="2025-01-01T00:00:00Z")

    def test_max_concurrency_zero_raises(self) -> None:
        with pytest.raises(LaneError, match="max_concurrency"):
            LaneManifest(lane_id="x", priority=0, max_concurrency=0, target_class="", created_at="2025-01-01T00:00:00Z")

    def test_max_concurrency_above_64_raises(self) -> None:
        with pytest.raises(LaneError, match="max_concurrency"):
            LaneManifest(lane_id="x", priority=0, max_concurrency=65, target_class="", created_at="2025-01-01T00:00:00Z")

    def test_empty_created_at_raises(self) -> None:
        with pytest.raises(LaneError, match="created_at"):
            LaneManifest(lane_id="x", priority=0, max_concurrency=1, target_class="", created_at="")

    def test_to_dict_includes_schema_version(self, minimal_manifest: LaneManifest) -> None:
        d = minimal_manifest.to_dict()
        assert d["schema_version"] == LANE_MANIFEST_SCHEMA_VERSION

    def test_to_dict_round_trips_via_from_dict(self, minimal_manifest: LaneManifest) -> None:
        restored = LaneManifest.from_dict(minimal_manifest.to_dict())
        assert restored == minimal_manifest

    def test_lane_hash_is_64_hex_chars(self, minimal_manifest: LaneManifest) -> None:
        h = minimal_manifest.lane_hash
        assert len(h) == 64
        assert all(c in "0123456789abcdef" for c in h)

    def test_lane_hash_is_stable(self, minimal_manifest: LaneManifest) -> None:
        assert minimal_manifest.lane_hash == minimal_manifest.lane_hash

    def test_different_manifests_have_different_hashes(self, minimal_manifest: LaneManifest) -> None:
        other = LaneManifest(
            lane_id="low",
            priority=1,
            max_concurrency=4,
            target_class="",
            created_at="2025-01-01T00:00:00Z",
        )
        assert minimal_manifest.lane_hash != other.lane_hash


# ---------------------------------------------------------------------------
# LaneStore -- CRUD
# ---------------------------------------------------------------------------


class TestLaneStore:
    """LaneStore CRUD behaviour."""

    def test_all_returns_empty_when_file_absent(self, store: LaneStore) -> None:
        assert store.all() == []

    def test_get_returns_none_when_absent(self, store: LaneStore) -> None:
        assert store.get("missing") is None

    def test_put_and_get_round_trip(self, store: LaneStore, minimal_manifest: LaneManifest) -> None:
        store.put(minimal_manifest)
        retrieved = store.get("urgent")
        assert retrieved == minimal_manifest

    def test_put_replaces_existing(self, store: LaneStore, minimal_manifest: LaneManifest) -> None:
        store.put(minimal_manifest)
        updated = LaneManifest(
            lane_id="urgent",
            priority=50,
            max_concurrency=8,
            target_class="updated",
            created_at="2025-06-01T00:00:00Z",
        )
        store.put(updated)
        assert store.get("urgent") == updated

    def test_all_returns_all_manifests(self, store: LaneStore) -> None:
        m1 = LaneManifest(lane_id="high", priority=80, max_concurrency=2, target_class="", created_at="2025-01-01T00:00:00Z")
        m2 = LaneManifest(lane_id="low", priority=10, max_concurrency=4, target_class="", created_at="2025-01-01T00:00:00Z")
        store.put(m1)
        store.put(m2)
        all_lanes = store.all()
        assert len(all_lanes) == 2

    def test_all_sorted_by_descending_priority(self, store: LaneStore) -> None:
        m_low = LaneManifest(lane_id="low", priority=1, max_concurrency=1, target_class="", created_at="2025-01-01T00:00:00Z")
        m_high = LaneManifest(lane_id="high", priority=99, max_concurrency=1, target_class="", created_at="2025-01-01T00:00:00Z")
        store.put(m_low)
        store.put(m_high)
        lanes = store.all()
        assert lanes[0].lane_id == "high"
        assert lanes[1].lane_id == "low"

    def test_delete_removes_lane(self, store: LaneStore, minimal_manifest: LaneManifest) -> None:
        store.put(minimal_manifest)
        result = store.delete("urgent")
        assert result is True
        assert store.get("urgent") is None

    def test_delete_returns_false_when_absent(self, store: LaneStore) -> None:
        assert store.delete("nonexistent") is False

    def test_store_file_is_valid_json(self, store: LaneStore, minimal_manifest: LaneManifest) -> None:
        store.put(minimal_manifest)
        raw = json.loads(store._path.read_text(encoding="utf-8"))
        assert isinstance(raw, list)
        assert len(raw) == 1

    def test_corrupted_file_returns_empty(self, store: LaneStore) -> None:
        store._path.parent.mkdir(parents=True, exist_ok=True)
        store._path.write_text("not json", encoding="utf-8")
        assert store.all() == []
