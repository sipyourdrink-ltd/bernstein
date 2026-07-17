"""Tests for the chain-projected pool registry and content store (#2547)."""

from __future__ import annotations

import pytest

from bernstein.core.sandbox.backend import SandboxCapability
from bernstein.core.sandbox.pool import PoolManifest, PoolWorkspaceTemplate
from bernstein.core.sandbox.pool_registry import (
    PoolRegistry,
    PoolStore,
    PoolStoreError,
    project_pool_registry,
)


def _pool(name: str, timeout: int = 900) -> PoolManifest:
    return PoolManifest(
        name=name,
        backend_allowlist=("worktree",),
        template=PoolWorkspaceTemplate(timeout_seconds=timeout),
        exposed_fields=("env",),
        capability_ceiling=frozenset({SandboxCapability.FILE_RW, SandboxCapability.EXEC}),
    )


def _ev(event_type: str, name: str, pool_hash: str) -> dict:
    return {"event_type": event_type, "details": {"pool_name": name, "pool_hash": pool_hash}}


class TestProjection:
    def test_register_then_active(self):
        p = _pool("a")
        active = project_pool_registry([_ev("pool.registered", "a", p.pool_hash)])
        assert active == {"a": p.pool_hash}

    def test_update_supersedes(self):
        p1, p2 = _pool("a", 900), _pool("a", 1800)
        events = [
            _ev("pool.registered", "a", p1.pool_hash),
            _ev("pool.updated", "a", p2.pool_hash),
        ]
        assert project_pool_registry(events) == {"a": p2.pool_hash}

    def test_retire_drops(self):
        p = _pool("a")
        events = [
            _ev("pool.registered", "a", p.pool_hash),
            _ev("pool.retired", "a", p.pool_hash),
        ]
        assert project_pool_registry(events) == {}

    def test_non_pool_events_ignored(self):
        p = _pool("a")
        events = [
            {"event_type": "cache.hit", "details": {"cache_key": "x"}},
            _ev("pool.registered", "a", p.pool_hash),
        ]
        assert project_pool_registry(events) == {"a": p.pool_hash}

    def test_projection_is_deterministic(self):
        p1, p2 = _pool("a"), _pool("b")
        events = [_ev("pool.registered", "a", p1.pool_hash), _ev("pool.registered", "b", p2.pool_hash)]
        assert project_pool_registry(events) == project_pool_registry(events)


class TestContentStore:
    def test_put_get_roundtrip(self, tmp_path):
        store = PoolStore(root=tmp_path)
        p = _pool("a")
        store.put(p)
        loaded = store.get(p.pool_hash)
        assert loaded.pool_hash == p.pool_hash

    def test_tampered_body_refused(self, tmp_path):
        store = PoolStore(root=tmp_path)
        p = _pool("a")
        path = store.put(p)
        # Flip a byte inside the stored body without updating the filename hash.
        text = path.read_text().replace('"timeout_seconds":900', '"timeout_seconds":1')
        assert '"timeout_seconds":1' in text
        path.write_text(text)
        with pytest.raises(PoolStoreError):
            store.get(p.pool_hash)

    def test_missing_body_refused(self, tmp_path):
        store = PoolStore(root=tmp_path)
        with pytest.raises(PoolStoreError):
            store.get("a" * 64)

    def test_non_canonical_hash_refused(self, tmp_path):
        store = PoolStore(root=tmp_path)
        with pytest.raises(PoolStoreError):
            store.get("../etc/passwd")


class TestRegistry:
    def test_registry_resolves_active_body(self, tmp_path):
        store = PoolStore(root=tmp_path)
        p = _pool("a")
        store.put(p)
        registry = PoolRegistry.from_events([_ev("pool.registered", "a", p.pool_hash)], store)
        assert registry.names() == ["a"]
        assert registry.get("a").pool_hash == p.pool_hash

    def test_retired_pool_returns_none(self, tmp_path):
        store = PoolStore(root=tmp_path)
        p = _pool("a")
        store.put(p)
        events = [_ev("pool.registered", "a", p.pool_hash), _ev("pool.retired", "a", p.pool_hash)]
        registry = PoolRegistry.from_events(events, store)
        assert registry.get("a") is None
