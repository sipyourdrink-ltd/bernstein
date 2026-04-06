"""Tests for ENT-001: Multi-tenant task isolation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from bernstein.core.tenant_isolation import (
    TenantIsolationContext,
    TenantIsolationManager,
    TenantQuota,
    ensure_tenant_data_layout,
    tenant_data_paths,
)
from bernstein.core.tenanting import (
    DEFAULT_TENANT_ID,
    TenantConfig,
    TenantRegistry,
)


@pytest.fixture()
def sdd_dir(tmp_path: Path) -> Path:
    """Create a temporary .sdd directory."""
    d = tmp_path / ".sdd"
    d.mkdir()
    return d


class TestTenantDataPaths:
    """Test tenant data path construction."""

    def test_default_tenant_paths(self, sdd_dir: Path) -> None:
        paths = tenant_data_paths(sdd_dir, DEFAULT_TENANT_ID)
        assert paths.root == sdd_dir / DEFAULT_TENANT_ID
        assert paths.wal_dir == sdd_dir / DEFAULT_TENANT_ID / "runtime" / "wal"
        assert paths.audit_dir == sdd_dir / DEFAULT_TENANT_ID / "audit"

    def test_custom_tenant_paths(self, sdd_dir: Path) -> None:
        paths = tenant_data_paths(sdd_dir, "acme-corp")
        assert paths.root == sdd_dir / "acme-corp"
        assert paths.backlog_dir == sdd_dir / "acme-corp" / "backlog"
        assert paths.metrics_dir == sdd_dir / "acme-corp" / "metrics"

    def test_normalize_empty_tenant(self, sdd_dir: Path) -> None:
        paths = tenant_data_paths(sdd_dir, "")
        assert paths.root == sdd_dir / DEFAULT_TENANT_ID


class TestEnsureTenantDataLayout:
    """Test directory creation for tenants."""

    def test_creates_all_directories(self, sdd_dir: Path) -> None:
        paths = ensure_tenant_data_layout(sdd_dir, "tenant-a")
        assert paths.backlog_dir.is_dir()
        assert paths.metrics_dir.is_dir()
        assert paths.wal_dir.is_dir()
        assert paths.audit_dir.is_dir()
        assert paths.runtime_dir.is_dir()

    def test_idempotent(self, sdd_dir: Path) -> None:
        paths1 = ensure_tenant_data_layout(sdd_dir, "tenant-a")
        paths2 = ensure_tenant_data_layout(sdd_dir, "tenant-a")
        assert paths1.root == paths2.root


class TestTenantQuota:
    """Test tenant quota defaults and values."""

    def test_defaults(self) -> None:
        q = TenantQuota()
        assert q.max_tasks == 100
        assert q.max_agents == 10
        assert q.budget_usd == pytest.approx(100.0)
        assert q.max_wal_entries == 10000

    def test_custom_values(self) -> None:
        q = TenantQuota(max_tasks=50, max_agents=5, budget_usd=50.0)
        assert q.max_tasks == 50
        assert q.max_agents == 5
        assert q.budget_usd == pytest.approx(50.0)


class TestTenantIsolationContext:
    """Test tenant isolation context."""

    def test_normalized_id(self, sdd_dir: Path) -> None:
        paths = tenant_data_paths(sdd_dir, "test")
        ctx = TenantIsolationContext(tenant_id="  test  ", paths=paths)
        assert ctx.normalized_id == "test"

    def test_default_quota(self, sdd_dir: Path) -> None:
        paths = tenant_data_paths(sdd_dir, "test")
        ctx = TenantIsolationContext(tenant_id="test", paths=paths)
        assert ctx.quota.max_tasks == 100


class TestTenantIsolationManager:
    """Test the isolation manager."""

    def test_get_context_creates_dirs(self, sdd_dir: Path) -> None:
        mgr = TenantIsolationManager(sdd_dir)
        ctx = mgr.get_context("tenant-x")
        assert ctx.paths.wal_dir.is_dir()
        assert ctx.paths.audit_dir.is_dir()

    def test_get_context_caching(self, sdd_dir: Path) -> None:
        mgr = TenantIsolationManager(sdd_dir)
        ctx1 = mgr.get_context("t1")
        ctx2 = mgr.get_context("t1")
        assert ctx1 is ctx2

    def test_register_and_check_quota(self, sdd_dir: Path) -> None:
        mgr = TenantIsolationManager(sdd_dir)
        mgr.register_quota("t1", TenantQuota(max_tasks=5))
        ctx = mgr.get_context("t1")
        assert ctx.quota.max_tasks == 5

        ok, _ = mgr.check_quota("t1", 3)
        assert ok

        ok, reason = mgr.check_quota("t1", 5)
        assert not ok
        assert "max_tasks" in reason

    def test_filter_tasks(self, sdd_dir: Path) -> None:
        mgr = TenantIsolationManager(sdd_dir)

        @dataclass
        class FakeTask:
            tenant_id: str = DEFAULT_TENANT_ID

        tasks: dict[str, Any] = {
            "t1": FakeTask(tenant_id="acme"),
            "t2": FakeTask(tenant_id="other"),
            "t3": FakeTask(tenant_id="acme"),
        }
        filtered = mgr.filter_tasks(tasks, "acme")
        assert set(filtered.keys()) == {"t1", "t3"}

    def test_list_tenants(self, sdd_dir: Path) -> None:
        registry = TenantRegistry(tenants=(TenantConfig(id="reg-tenant"),))
        mgr = TenantIsolationManager(sdd_dir, registry=registry)
        mgr.get_context("dynamic-tenant")
        tenants = mgr.list_tenants()
        assert "reg-tenant" in tenants
        assert "dynamic-tenant" in tenants

    def test_persist_and_load_state(self, sdd_dir: Path) -> None:
        mgr = TenantIsolationManager(sdd_dir)
        mgr.register_quota("t1", TenantQuota(max_tasks=42))
        mgr.get_context("t1")
        mgr.persist_state()

        mgr2 = TenantIsolationManager(sdd_dir)
        mgr2.load_state()
        ctx = mgr2.get_context("t1")
        assert ctx.quota.max_tasks == 42

    def test_load_state_missing_file(self, sdd_dir: Path) -> None:
        mgr = TenantIsolationManager(sdd_dir)
        mgr.load_state()  # Should not raise
        assert mgr.list_tenants() == []
