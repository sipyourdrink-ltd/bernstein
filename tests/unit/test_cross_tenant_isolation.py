"""Cross-tenant data isolation verification tests.

Verifies that tenant boundaries are strictly enforced at every data layer:

- Task filtering: Tenant B cannot read or modify Tenant A's tasks.
- WAL paths: Tenant A's WAL directory is physically separate from Tenant B's.
- Metrics/cost data: Each tenant's metrics live in isolated directories.
- Path traversal: Tenant ID normalisation prevents namespace escapes.
- Quota independence: Tenant A's task count does not affect Tenant B's quota.

These run as part of CI for every release (tagged ``ci``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from bernstein.core.tenant_isolation import (
    TenantIsolationManager,
    TenantQuota,
    ensure_tenant_data_layout,
    tenant_data_paths,
)
from bernstein.core.tenanting import (
    DEFAULT_TENANT_ID,
    TenantConfig,
    TenantRegistry,
    normalize_tenant_id,
    resolve_tenant_scope,
    tenant_paths,
)

pytestmark = pytest.mark.ci


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@dataclass
class FakeTask:
    """Minimal task stand-in for isolation tests."""

    id: str
    tenant_id: str
    title: str = "test task"

    def __repr__(self) -> str:
        return f"FakeTask(id={self.id!r}, tenant_id={self.tenant_id!r})"


def _write_wal_entry(wal_dir: Path, tenant_id: str, content: dict[str, Any]) -> Path:
    """Write a mock WAL entry file for a tenant."""
    wal_dir.mkdir(parents=True, exist_ok=True)
    path = wal_dir / f"{tenant_id}-entry.jsonl"
    path.write_text(json.dumps(content) + "\n", encoding="utf-8")
    return path


def _write_metrics(metrics_dir: Path, filename: str, content: dict[str, Any]) -> Path:
    """Write a mock metrics file."""
    metrics_dir.mkdir(parents=True, exist_ok=True)
    path = metrics_dir / filename
    path.write_text(json.dumps(content) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sdd_dir(tmp_path: Path) -> Path:
    d = tmp_path / ".sdd"
    d.mkdir()
    return d


@pytest.fixture()
def manager(sdd_dir: Path) -> TenantIsolationManager:
    registry = TenantRegistry(
        tenants=(
            TenantConfig(id="tenant-a"),
            TenantConfig(id="tenant-b"),
        )
    )
    return TenantIsolationManager(sdd_dir, registry=registry)


# ---------------------------------------------------------------------------
# Task filtering isolation
# ---------------------------------------------------------------------------


class TestTaskFilteringIsolation:
    """Tenant B cannot see Tenant A's tasks and vice versa."""

    def test_tenant_a_tasks_invisible_to_tenant_b(self, manager: TenantIsolationManager) -> None:
        tasks: dict[str, Any] = {
            "task-1": FakeTask(id="task-1", tenant_id="tenant-a"),
            "task-2": FakeTask(id="task-2", tenant_id="tenant-a"),
            "task-3": FakeTask(id="task-3", tenant_id="tenant-b"),
        }
        result = manager.filter_tasks(tasks, "tenant-b")
        assert "task-1" not in result
        assert "task-2" not in result
        assert "task-3" in result

    def test_tenant_b_tasks_invisible_to_tenant_a(self, manager: TenantIsolationManager) -> None:
        tasks: dict[str, Any] = {
            "task-x": FakeTask(id="task-x", tenant_id="tenant-b"),
            "task-y": FakeTask(id="task-y", tenant_id="tenant-a"),
        }
        result = manager.filter_tasks(tasks, "tenant-a")
        assert "task-x" not in result
        assert "task-y" in result

    def test_filter_returns_empty_when_no_matching_tasks(self, manager: TenantIsolationManager) -> None:
        tasks: dict[str, Any] = {
            "task-1": FakeTask(id="task-1", tenant_id="tenant-a"),
        }
        result = manager.filter_tasks(tasks, "tenant-b")
        assert result == {}

    def test_filter_returns_all_when_all_match(self, manager: TenantIsolationManager) -> None:
        tasks: dict[str, Any] = {
            "t1": FakeTask(id="t1", tenant_id="tenant-a"),
            "t2": FakeTask(id="t2", tenant_id="tenant-a"),
        }
        result = manager.filter_tasks(tasks, "tenant-a")
        assert set(result.keys()) == {"t1", "t2"}

    def test_default_tenant_isolation_from_named_tenant(self, manager: TenantIsolationManager) -> None:
        tasks: dict[str, Any] = {
            "def-task": FakeTask(id="def-task", tenant_id=DEFAULT_TENANT_ID),
            "a-task": FakeTask(id="a-task", tenant_id="tenant-a"),
        }
        result = manager.filter_tasks(tasks, DEFAULT_TENANT_ID)
        assert "def-task" in result
        assert "a-task" not in result

    def test_tenant_b_cannot_modify_tenant_a_task(self, manager: TenantIsolationManager) -> None:
        """Modifying a filtered-out task is silently impossible — it's not returned."""
        tasks: dict[str, Any] = {
            "shared-id": FakeTask(id="shared-id", tenant_id="tenant-a"),
        }
        tenant_b_view = manager.filter_tasks(tasks, "tenant-b")
        assert "shared-id" not in tenant_b_view
        # Original dict is unchanged (filter doesn't mutate)
        assert "shared-id" in tasks


# ---------------------------------------------------------------------------
# WAL path isolation
# ---------------------------------------------------------------------------


class TestWALPathIsolation:
    """Tenant A's WAL is physically separate from Tenant B's WAL."""

    def test_wal_paths_are_distinct(self, sdd_dir: Path) -> None:
        paths_a = tenant_data_paths(sdd_dir, "tenant-a")
        paths_b = tenant_data_paths(sdd_dir, "tenant-b")
        assert paths_a.wal_dir != paths_b.wal_dir

    def test_wal_dirs_do_not_overlap(self, sdd_dir: Path) -> None:
        paths_a = tenant_data_paths(sdd_dir, "tenant-a")
        paths_b = tenant_data_paths(sdd_dir, "tenant-b")
        # Neither WAL path is a parent of the other
        assert not paths_a.wal_dir.is_relative_to(paths_b.wal_dir)
        assert not paths_b.wal_dir.is_relative_to(paths_a.wal_dir)

    def test_tenant_a_wal_entry_not_visible_in_tenant_b_dir(self, sdd_dir: Path) -> None:
        ensure_tenant_data_layout(sdd_dir, "tenant-a")
        ensure_tenant_data_layout(sdd_dir, "tenant-b")

        paths_a = tenant_data_paths(sdd_dir, "tenant-a")
        paths_b = tenant_data_paths(sdd_dir, "tenant-b")

        _write_wal_entry(paths_a.wal_dir, "tenant-a", {"decision": "spawn", "tenant": "tenant-a"})

        # Tenant B's WAL directory should contain no entries from tenant A
        b_wal_files = list(paths_b.wal_dir.glob("*.jsonl"))
        assert len(b_wal_files) == 0

    def test_wal_entries_are_isolated_per_tenant(self, sdd_dir: Path) -> None:
        ensure_tenant_data_layout(sdd_dir, "tenant-a")
        ensure_tenant_data_layout(sdd_dir, "tenant-b")

        paths_a = tenant_data_paths(sdd_dir, "tenant-a")
        paths_b = tenant_data_paths(sdd_dir, "tenant-b")

        entry_a = _write_wal_entry(paths_a.wal_dir, "run-001", {"actor": "tenant-a"})
        entry_b = _write_wal_entry(paths_b.wal_dir, "run-002", {"actor": "tenant-b"})

        # Confirm each entry lives in its own directory
        assert entry_a.parent == paths_a.wal_dir
        assert entry_b.parent == paths_b.wal_dir
        assert entry_a.parent != entry_b.parent

    def test_wal_paths_rooted_at_tenant_namespace(self, sdd_dir: Path) -> None:
        paths = tenant_data_paths(sdd_dir, "tenant-a")
        # WAL must be inside the tenant's root directory
        assert paths.wal_dir.is_relative_to(paths.root)


# ---------------------------------------------------------------------------
# Metrics / cost data isolation
# ---------------------------------------------------------------------------


class TestMetricsIsolation:
    """Cost and metrics data are strictly partitioned per tenant."""

    def test_metrics_dirs_are_distinct(self, sdd_dir: Path) -> None:
        paths_a = tenant_data_paths(sdd_dir, "tenant-a")
        paths_b = tenant_data_paths(sdd_dir, "tenant-b")
        assert paths_a.metrics_dir != paths_b.metrics_dir

    def test_tenant_a_metrics_not_visible_to_tenant_b(self, sdd_dir: Path) -> None:
        ensure_tenant_data_layout(sdd_dir, "tenant-a")
        ensure_tenant_data_layout(sdd_dir, "tenant-b")

        paths_a = tenant_data_paths(sdd_dir, "tenant-a")
        paths_b = tenant_data_paths(sdd_dir, "tenant-b")

        _write_metrics(paths_a.metrics_dir, "cost.jsonl", {"cost_usd": 1.23, "tenant": "tenant-a"})

        b_metrics_files = list(paths_b.metrics_dir.glob("*.jsonl"))
        assert len(b_metrics_files) == 0

    def test_metrics_data_content_isolated(self, sdd_dir: Path) -> None:
        ensure_tenant_data_layout(sdd_dir, "tenant-a")
        ensure_tenant_data_layout(sdd_dir, "tenant-b")

        paths_a = tenant_data_paths(sdd_dir, "tenant-a")
        paths_b = tenant_data_paths(sdd_dir, "tenant-b")

        _write_metrics(paths_a.metrics_dir, "cost.jsonl", {"cost_usd": 42.0})
        _write_metrics(paths_b.metrics_dir, "cost.jsonl", {"cost_usd": 7.5})

        data_a = json.loads((paths_a.metrics_dir / "cost.jsonl").read_text())
        data_b = json.loads((paths_b.metrics_dir / "cost.jsonl").read_text())

        assert data_a["cost_usd"] == pytest.approx(42.0)
        assert data_b["cost_usd"] == pytest.approx(7.5)
        # Each tenant only sees its own data
        assert data_a != data_b

    def test_metrics_dirs_rooted_at_tenant_namespace(self, sdd_dir: Path) -> None:
        paths = tenant_data_paths(sdd_dir, "tenant-a")
        assert paths.metrics_dir.is_relative_to(paths.root)


# ---------------------------------------------------------------------------
# Audit directory isolation
# ---------------------------------------------------------------------------


class TestAuditIsolation:
    """Audit logs are scoped per-tenant."""

    def test_audit_dirs_are_distinct(self, sdd_dir: Path) -> None:
        paths_a = tenant_data_paths(sdd_dir, "tenant-a")
        paths_b = tenant_data_paths(sdd_dir, "tenant-b")
        assert paths_a.audit_dir != paths_b.audit_dir

    def test_audit_dir_inside_tenant_root(self, sdd_dir: Path) -> None:
        paths = tenant_data_paths(sdd_dir, "tenant-a")
        assert paths.audit_dir.is_relative_to(paths.root)

    def test_tenant_a_audit_entry_absent_from_tenant_b(self, sdd_dir: Path) -> None:
        ensure_tenant_data_layout(sdd_dir, "tenant-a")
        ensure_tenant_data_layout(sdd_dir, "tenant-b")

        paths_a = tenant_data_paths(sdd_dir, "tenant-a")
        paths_b = tenant_data_paths(sdd_dir, "tenant-b")

        audit_entry = {"event": "task_created", "tenant": "tenant-a"}
        (paths_a.audit_dir / "audit.jsonl").write_text(json.dumps(audit_entry), encoding="utf-8")

        b_audit_files = list(paths_b.audit_dir.glob("*.jsonl"))
        assert len(b_audit_files) == 0


# ---------------------------------------------------------------------------
# Quota independence
# ---------------------------------------------------------------------------


class TestQuotaIndependence:
    """Tenant A's usage does not exhaust Tenant B's quota."""

    def test_tenant_a_quota_does_not_affect_tenant_b(self, manager: TenantIsolationManager) -> None:
        manager.register_quota("tenant-a", TenantQuota(max_tasks=2))
        manager.register_quota("tenant-b", TenantQuota(max_tasks=5))

        # Tenant A has hit its limit
        ok_a, _ = manager.check_quota("tenant-a", 2)
        assert not ok_a

        # Tenant B is unaffected
        ok_b, _ = manager.check_quota("tenant-b", 2)
        assert ok_b

    def test_quota_checked_per_tenant(self, manager: TenantIsolationManager) -> None:
        manager.register_quota("tenant-a", TenantQuota(max_tasks=3))
        manager.register_quota("tenant-b", TenantQuota(max_tasks=10))

        ok_a, _ = manager.check_quota("tenant-a", 4)
        assert not ok_a

        ok_b, _ = manager.check_quota("tenant-b", 4)
        assert ok_b


# ---------------------------------------------------------------------------
# Namespace / path traversal prevention
# ---------------------------------------------------------------------------


class TestNamespaceIsolation:
    """Tenant ID normalisation prevents namespace escapes."""

    def test_empty_tenant_id_maps_to_default(self) -> None:
        assert normalize_tenant_id("") == DEFAULT_TENANT_ID
        assert normalize_tenant_id(None) == DEFAULT_TENANT_ID  # type: ignore[arg-type]
        assert normalize_tenant_id("   ") == DEFAULT_TENANT_ID

    def test_whitespace_stripped_from_tenant_id(self) -> None:
        assert normalize_tenant_id("  acme  ") == "acme"

    def test_different_tenant_ids_produce_different_paths(self, sdd_dir: Path) -> None:
        paths_a = tenant_paths(sdd_dir, "tenant-a")
        paths_b = tenant_paths(sdd_dir, "tenant-b")
        assert paths_a.root != paths_b.root

    def test_resolve_scope_rejects_cross_tenant_access(self) -> None:
        """A non-default tenant cannot request another tenant's scope."""
        with pytest.raises(PermissionError):
            resolve_tenant_scope(
                bound_tenant="tenant-a",
                requested_tenant="tenant-b",
            )

    def test_resolve_scope_allows_own_tenant(self) -> None:
        result = resolve_tenant_scope(
            bound_tenant="tenant-a",
            requested_tenant="tenant-a",
        )
        assert result == "tenant-a"

    def test_resolve_scope_default_allows_any_tenant(self) -> None:
        """Default tenant (admin) can access named tenants."""
        result = resolve_tenant_scope(
            bound_tenant=DEFAULT_TENANT_ID,
            requested_tenant="tenant-x",
        )
        assert result == "tenant-x"

    def test_unknown_tenant_rejected_when_registry_configured(self) -> None:
        registry = TenantRegistry(tenants=(TenantConfig(id="acme"),))
        with pytest.raises(LookupError, match="unknown tenant"):
            resolve_tenant_scope(
                bound_tenant=DEFAULT_TENANT_ID,
                requested_tenant="not-registered",
                registry=registry,
            )

    def test_tenant_root_paths_do_not_overlap(self, sdd_dir: Path) -> None:
        paths_a = tenant_data_paths(sdd_dir, "tenant-a")
        paths_b = tenant_data_paths(sdd_dir, "tenant-b")
        # Neither root is an ancestor of the other
        assert not paths_a.root.is_relative_to(paths_b.root)
        assert not paths_b.root.is_relative_to(paths_a.root)


# ---------------------------------------------------------------------------
# End-to-end multi-tenant scenario
# ---------------------------------------------------------------------------


class TestMultiTenantScenario:
    """Full scenario: two tenants operate independently without data leakage."""

    def test_full_isolation_scenario(self, sdd_dir: Path) -> None:
        """Tenant A and Tenant B create data; neither can see the other's."""
        # Set up both tenants
        ensure_tenant_data_layout(sdd_dir, "tenant-a")
        ensure_tenant_data_layout(sdd_dir, "tenant-b")

        paths_a = tenant_data_paths(sdd_dir, "tenant-a")
        paths_b = tenant_data_paths(sdd_dir, "tenant-b")

        # Write WAL, metrics, and audit data for tenant-a
        _write_wal_entry(paths_a.wal_dir, "run-a", {"actor": "tenant-a", "decision": "spawn"})
        _write_metrics(paths_a.metrics_dir, "cost.jsonl", {"cost_usd": 5.0, "tenant": "tenant-a"})
        (paths_a.audit_dir / "audit.jsonl").write_text(
            json.dumps({"event": "created", "tenant": "tenant-a"}),
            encoding="utf-8",
        )

        # Write WAL, metrics, and audit data for tenant-b
        _write_wal_entry(paths_b.wal_dir, "run-b", {"actor": "tenant-b", "decision": "spawn"})
        _write_metrics(paths_b.metrics_dir, "cost.jsonl", {"cost_usd": 2.0, "tenant": "tenant-b"})
        (paths_b.audit_dir / "audit.jsonl").write_text(
            json.dumps({"event": "created", "tenant": "tenant-b"}),
            encoding="utf-8",
        )

        # Verify tenant-a cannot see tenant-b's data
        a_wal_files = {f.name for f in paths_a.wal_dir.glob("*.jsonl")}
        assert "run-b.jsonl" not in a_wal_files

        b_wal_files = {f.name for f in paths_b.wal_dir.glob("*.jsonl")}
        assert "run-a.jsonl" not in b_wal_files

        # Verify cost data is separately stored
        cost_a = json.loads((paths_a.metrics_dir / "cost.jsonl").read_text())
        cost_b = json.loads((paths_b.metrics_dir / "cost.jsonl").read_text())
        assert cost_a["cost_usd"] == pytest.approx(5.0)
        assert cost_b["cost_usd"] == pytest.approx(2.0)

    def test_task_filtering_in_mixed_store(self, manager: TenantIsolationManager) -> None:
        """Tasks from different tenants are correctly separated by filter_tasks."""
        tasks: dict[str, Any] = {}
        for i in range(5):
            tid = f"task-a-{i}"
            tasks[tid] = FakeTask(id=tid, tenant_id="tenant-a")
        for i in range(3):
            tid = f"task-b-{i}"
            tasks[tid] = FakeTask(id=tid, tenant_id="tenant-b")

        view_a = manager.filter_tasks(tasks, "tenant-a")
        view_b = manager.filter_tasks(tasks, "tenant-b")

        assert len(view_a) == 5
        assert len(view_b) == 3
        # No overlap
        assert set(view_a.keys()).isdisjoint(set(view_b.keys()))


# ---------------------------------------------------------------------------
# WAL content isolation
# ---------------------------------------------------------------------------


class TestWALContentIsolation:
    """Tenant A's WAL entries are never readable via Tenant B's WAL path."""

    def test_wal_content_not_cross_contaminated(self, sdd_dir: Path) -> None:
        """Writing entries for both tenants does not mix their WAL contents."""
        ensure_tenant_data_layout(sdd_dir, "tenant-a")
        ensure_tenant_data_layout(sdd_dir, "tenant-b")

        paths_a = tenant_data_paths(sdd_dir, "tenant-a")
        paths_b = tenant_data_paths(sdd_dir, "tenant-b")

        _write_wal_entry(paths_a.wal_dir, "entry-001", {"actor": "tenant-a", "seq": 1})
        _write_wal_entry(paths_b.wal_dir, "entry-001", {"actor": "tenant-b", "seq": 1})

        # Entry for tenant-a must live exclusively under tenant-a's WAL dir
        a_entries = list(paths_a.wal_dir.glob("*.jsonl"))
        b_entries = list(paths_b.wal_dir.glob("*.jsonl"))

        assert len(a_entries) == 1
        assert len(b_entries) == 1

        content_a = json.loads(a_entries[0].read_text())
        content_b = json.loads(b_entries[0].read_text())

        assert content_a["actor"] == "tenant-a"
        assert content_b["actor"] == "tenant-b"
        assert content_a["actor"] != content_b["actor"]

    def test_many_wal_entries_stay_isolated(self, sdd_dir: Path) -> None:
        """High volume of WAL entries across two tenants remains isolated."""
        ensure_tenant_data_layout(sdd_dir, "tenant-a")
        ensure_tenant_data_layout(sdd_dir, "tenant-b")

        paths_a = tenant_data_paths(sdd_dir, "tenant-a")
        paths_b = tenant_data_paths(sdd_dir, "tenant-b")

        for i in range(10):
            _write_wal_entry(paths_a.wal_dir, f"a-{i:03d}", {"seq": i, "tenant": "tenant-a"})
        for i in range(7):
            _write_wal_entry(paths_b.wal_dir, f"b-{i:03d}", {"seq": i, "tenant": "tenant-b"})

        a_entries = list(paths_a.wal_dir.glob("*.jsonl"))
        b_entries = list(paths_b.wal_dir.glob("*.jsonl"))

        assert len(a_entries) == 10
        assert len(b_entries) == 7

        # Verify no tenant-b files leaked into tenant-a directory
        for entry in a_entries:
            data = json.loads(entry.read_text())
            assert data.get("tenant") == "tenant-a", f"Unexpected tenant in {entry.name}: {data}"

        for entry in b_entries:
            data = json.loads(entry.read_text())
            assert data.get("tenant") == "tenant-b", f"Unexpected tenant in {entry.name}: {data}"


# ---------------------------------------------------------------------------
# Persist and load state isolation
# ---------------------------------------------------------------------------


class TestPersistLoadIsolation:
    """TenantIsolationManager persist/load state does not bleed between tenants."""

    def test_persist_and_load_state_preserves_quota_isolation(self, sdd_dir: Path) -> None:
        """Persisting and reloading state maintains per-tenant quotas."""
        manager_a = TenantIsolationManager(sdd_dir)
        manager_a.register_quota("tenant-a", TenantQuota(max_tasks=10, budget_usd=50.0))
        manager_a.register_quota("tenant-b", TenantQuota(max_tasks=5, budget_usd=20.0))
        manager_a.get_context("tenant-a")
        manager_a.get_context("tenant-b")
        manager_a.persist_state()

        # New manager instance loads state from disk
        manager_b = TenantIsolationManager(sdd_dir)
        manager_b.load_state()

        ctx_a = manager_b.get_context("tenant-a")
        ctx_b = manager_b.get_context("tenant-b")

        assert ctx_a.quota.max_tasks == 10
        assert ctx_a.quota.budget_usd == pytest.approx(50.0)
        assert ctx_b.quota.max_tasks == 5
        assert ctx_b.quota.budget_usd == pytest.approx(20.0)

        # Tenant A's quota must not equal Tenant B's quota
        assert ctx_a.quota.max_tasks != ctx_b.quota.max_tasks
        assert ctx_a.quota.budget_usd != ctx_b.quota.budget_usd

    def test_load_state_missing_file_is_safe(self, sdd_dir: Path) -> None:
        """Loading state when no persisted file exists does not raise."""
        manager = TenantIsolationManager(sdd_dir)
        manager.load_state()  # must not raise
        # Default quota should be returned
        ctx = manager.get_context("tenant-x")
        assert ctx.quota.max_tasks == TenantQuota().max_tasks

    def test_list_tenants_reflects_both_registry_and_contexts(self, manager: TenantIsolationManager) -> None:
        """list_tenants returns all tenants from registry plus any dynamically added."""
        # Registry has tenant-a and tenant-b; create context for tenant-c dynamically
        manager.get_context("tenant-c")
        tenants = manager.list_tenants()
        assert "tenant-a" in tenants
        assert "tenant-b" in tenants
        assert "tenant-c" in tenants

    def test_cost_data_isolated_after_reload(self, sdd_dir: Path) -> None:
        """Cost/metrics data written before a manager reload remains tenant-scoped."""
        ensure_tenant_data_layout(sdd_dir, "tenant-a")
        ensure_tenant_data_layout(sdd_dir, "tenant-b")

        paths_a = tenant_data_paths(sdd_dir, "tenant-a")
        paths_b = tenant_data_paths(sdd_dir, "tenant-b")

        _write_metrics(paths_a.metrics_dir, "cost.jsonl", {"cost_usd": 99.0, "tenant": "tenant-a"})
        _write_metrics(paths_b.metrics_dir, "cost.jsonl", {"cost_usd": 1.0, "tenant": "tenant-b"})

        # Reload paths from scratch and verify isolation persists
        paths_a_reload = tenant_data_paths(sdd_dir, "tenant-a")
        paths_b_reload = tenant_data_paths(sdd_dir, "tenant-b")

        cost_a = json.loads((paths_a_reload.metrics_dir / "cost.jsonl").read_text())
        cost_b = json.loads((paths_b_reload.metrics_dir / "cost.jsonl").read_text())

        assert cost_a["cost_usd"] == pytest.approx(99.0)
        assert cost_b["cost_usd"] == pytest.approx(1.0)
        assert cost_a["tenant"] == "tenant-a"
        assert cost_b["tenant"] == "tenant-b"
