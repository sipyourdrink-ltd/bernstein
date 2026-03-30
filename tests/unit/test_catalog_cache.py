"""Tests for CatalogRegistry cache and auto-discovery (task 403)."""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING

from bernstein.agents.catalog import (
    _REMOTE_TTL,
    CachedAgentEntry,
    CatalogRegistry,
)

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fresh_entry(role: str = "backend", source: str = "remote") -> CachedAgentEntry:
    return CachedAgentEntry(
        role=role,
        description="Test agent.",
        model="sonnet",
        effort="high",
        source=source,
        fetched_at=time.time(),
        ttl_seconds=3600,
        metadata={},
    )


def _stale_entry(role: str = "backend") -> CachedAgentEntry:
    return CachedAgentEntry(
        role=role,
        description="Test agent.",
        model="sonnet",
        effort="high",
        source="remote",
        fetched_at=time.time() - 7200,  # 2 hours ago
        ttl_seconds=3600,
        metadata={},
    )


def _write_json_cache(path: Path, entries: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(entries))


# ---------------------------------------------------------------------------
# CachedAgentEntry.is_fresh
# ---------------------------------------------------------------------------


class TestCachedAgentEntryFreshness:
    def test_is_fresh_when_within_ttl(self):
        entry = _fresh_entry()
        assert entry.is_fresh is True

    def test_is_stale_when_past_ttl(self):
        entry = _stale_entry()
        assert entry.is_fresh is False

    def test_boundary_exactly_at_ttl_is_stale(self):
        entry = CachedAgentEntry(
            role="backend",
            description=".",
            model="sonnet",
            effort="high",
            source="remote",
            fetched_at=time.time() - 3600,
            ttl_seconds=3600,
        )
        # time.time() - fetched_at == ttl_seconds → NOT fresh (< is strict)
        assert entry.is_fresh is False


# ---------------------------------------------------------------------------
# CatalogRegistry.write_cache
# ---------------------------------------------------------------------------


class TestWriteCache:
    def test_creates_file_and_parent_dirs(self, tmp_path):
        cache_path = tmp_path / "deep" / "nested" / "catalog.json"
        registry = CatalogRegistry(_cache_path=cache_path)
        registry._cached_roles["backend"] = _fresh_entry()
        registry.write_cache()
        assert cache_path.exists()

    def test_serialises_all_fields(self, tmp_path):
        cache_path = tmp_path / "catalog.json"
        registry = CatalogRegistry(_cache_path=cache_path)
        now = time.time()
        entry = CachedAgentEntry(
            role="qa",
            description="QA agent.",
            model="sonnet",
            effort="normal",
            source="remote",
            fetched_at=now,
            ttl_seconds=_REMOTE_TTL,
            metadata={"extra": "value"},
        )
        registry._cached_roles["qa"] = entry
        registry.write_cache()

        data = json.loads(cache_path.read_text())
        assert len(data) == 1
        row = data[0]
        assert row["role"] == "qa"
        assert row["source"] == "remote"
        assert row["ttl_seconds"] == _REMOTE_TTL
        assert row["metadata"] == {"extra": "value"}
        assert abs(row["fetched_at"] - now) < 0.01

    def test_overwrites_existing_file(self, tmp_path):
        cache_path = tmp_path / "catalog.json"
        cache_path.write_text("old-content")
        registry = CatalogRegistry(_cache_path=cache_path)
        registry._cached_roles["backend"] = _fresh_entry()
        registry.write_cache()
        data = json.loads(cache_path.read_text())
        assert len(data) == 1

    def test_empty_cached_roles_writes_empty_list(self, tmp_path):
        cache_path = tmp_path / "catalog.json"
        registry = CatalogRegistry(_cache_path=cache_path)
        registry.write_cache()
        assert json.loads(cache_path.read_text()) == []


# ---------------------------------------------------------------------------
# CatalogRegistry.load_cache
# ---------------------------------------------------------------------------


class TestLoadCache:
    def test_returns_false_when_file_missing(self, tmp_path):
        registry = CatalogRegistry(_cache_path=tmp_path / "no_file.json")
        assert registry.load_cache() is False
        assert registry._cached_roles == {}

    def test_returns_false_when_all_stale(self, tmp_path):
        cache_path = tmp_path / "catalog.json"
        _write_json_cache(
            cache_path,
            [
                {
                    "role": "backend",
                    "description": ".",
                    "model": "sonnet",
                    "effort": "high",
                    "source": "remote",
                    "fetched_at": time.time() - 7200,
                    "ttl_seconds": 3600,
                    "metadata": {},
                }
            ],
        )
        registry = CatalogRegistry(_cache_path=cache_path)
        assert registry.load_cache() is False
        assert registry._cached_roles == {}

    def test_returns_true_and_populates_for_fresh_entries(self, tmp_path):
        cache_path = tmp_path / "catalog.json"
        now = time.time()
        _write_json_cache(
            cache_path,
            [
                {
                    "role": "backend",
                    "description": "Backend engineer.",
                    "model": "sonnet",
                    "effort": "high",
                    "source": "remote",
                    "fetched_at": now,
                    "ttl_seconds": 3600,
                    "metadata": {},
                }
            ],
        )
        registry = CatalogRegistry(_cache_path=cache_path)
        assert registry.load_cache() is True
        assert "backend" in registry._cached_roles
        assert registry._cached_roles["backend"].source == "remote"

    def test_loads_only_fresh_entries_when_mixed(self, tmp_path):
        cache_path = tmp_path / "catalog.json"
        now = time.time()
        _write_json_cache(
            cache_path,
            [
                {
                    "role": "backend",
                    "description": ".",
                    "model": "sonnet",
                    "effort": "high",
                    "source": "remote",
                    "fetched_at": now,
                    "ttl_seconds": 3600,
                    "metadata": {},
                },
                {
                    "role": "qa",
                    "description": ".",
                    "model": "sonnet",
                    "effort": "normal",
                    "source": "remote",
                    "fetched_at": now - 7200,
                    "ttl_seconds": 3600,
                    "metadata": {},
                },
            ],
        )
        registry = CatalogRegistry(_cache_path=cache_path)
        result = registry.load_cache()
        # True because at least one fresh entry was loaded
        assert result is True
        assert "backend" in registry._cached_roles
        assert "qa" not in registry._cached_roles

    def test_handles_corrupt_json_gracefully(self, tmp_path):
        cache_path = tmp_path / "catalog.json"
        cache_path.write_text("not-valid-json{{{")
        registry = CatalogRegistry(_cache_path=cache_path)
        assert registry.load_cache() is False

    def test_handles_missing_fields_gracefully(self, tmp_path):
        cache_path = tmp_path / "catalog.json"
        _write_json_cache(cache_path, [{"role": "backend"}])  # missing many fields
        registry = CatalogRegistry(_cache_path=cache_path)
        assert registry.load_cache() is False


# ---------------------------------------------------------------------------
# CatalogRegistry.discover
# ---------------------------------------------------------------------------


class TestDiscover:
    def test_falls_back_to_builtins_when_no_providers_and_no_cache(self, tmp_path):
        cache_path = tmp_path / "catalog.json"
        registry = CatalogRegistry(_cache_path=cache_path)
        registry.discover()
        assert len(registry._cached_roles) > 0
        for entry in registry._cached_roles.values():
            assert entry.source == "builtin"

    def test_builtin_roles_cover_expected_roles(self, tmp_path):
        cache_path = tmp_path / "catalog.json"
        registry = CatalogRegistry(_cache_path=cache_path)
        registry.discover()
        expected = {"backend", "frontend", "qa", "security", "manager"}
        assert expected <= set(registry._cached_roles.keys())

    def test_writes_cache_after_sync(self, tmp_path):
        cache_path = tmp_path / ".sdd" / "agents" / "catalog.json"
        registry = CatalogRegistry(_cache_path=cache_path)
        registry.discover()
        assert cache_path.exists()
        data = json.loads(cache_path.read_text())
        assert len(data) > 0

    def test_uses_fresh_cache_without_overwriting(self, tmp_path):
        cache_path = tmp_path / "catalog.json"
        now = time.time()
        _write_json_cache(
            cache_path,
            [
                {
                    "role": "custom-role",
                    "description": "Custom agent.",
                    "model": "opus",
                    "effort": "max",
                    "source": "remote",
                    "fetched_at": now,
                    "ttl_seconds": 3600,
                    "metadata": {},
                }
            ],
        )
        registry = CatalogRegistry(_cache_path=cache_path)
        registry.discover()
        # Fresh cache used — custom-role persists, no builtin overwrite
        assert "custom-role" in registry._cached_roles
        assert registry._cached_roles["custom-role"].source == "remote"

    def test_force_refresh_ignores_fresh_cache(self, tmp_path):
        cache_path = tmp_path / "catalog.json"
        now = time.time()
        _write_json_cache(
            cache_path,
            [
                {
                    "role": "custom-role",
                    "description": "Custom agent.",
                    "model": "opus",
                    "effort": "max",
                    "source": "remote",
                    "fetched_at": now,
                    "ttl_seconds": 3600,
                    "metadata": {},
                }
            ],
        )
        registry = CatalogRegistry(_cache_path=cache_path)
        registry.discover(force=True)
        # Forced refresh — custom-role gone, builtins loaded
        assert "custom-role" not in registry._cached_roles
        assert "backend" in registry._cached_roles

    def test_stale_cache_triggers_refresh_to_builtins(self, tmp_path):
        cache_path = tmp_path / "catalog.json"
        _write_json_cache(
            cache_path,
            [
                {
                    "role": "custom-role",
                    "description": "Custom agent.",
                    "model": "opus",
                    "effort": "max",
                    "source": "remote",
                    "fetched_at": time.time() - 7200,
                    "ttl_seconds": 3600,
                    "metadata": {},
                }
            ],
        )
        registry = CatalogRegistry(_cache_path=cache_path)
        registry.discover()
        # Stale cache → refresh → builtins
        assert "backend" in registry._cached_roles

    def test_higher_priority_provider_wins_on_role_conflict(self, tmp_path):
        """Higher-priority entry in _cached_roles overrides lower-priority for same role."""
        cache_path = tmp_path / "catalog.json"
        registry = CatalogRegistry(_cache_path=cache_path)
        # Pre-populate with two entries for same role, different sources
        # discover() merges: the one already present (higher priority) wins
        registry._cached_roles["backend"] = CachedAgentEntry(
            role="backend",
            description="High-priority backend.",
            model="opus",
            effort="max",
            source="high-priority",
            fetched_at=time.time(),
            ttl_seconds=3600,
        )
        registry.discover()  # builtins should NOT overwrite the higher-priority entry
        assert registry._cached_roles["backend"].source == "high-priority"
