"""ENT-001: Multi-tenant task isolation.

Adds tenant_id scoping to task queries, WAL paths, and metrics directories.
Data paths follow the layout: ``.sdd/{tenant_id}/``.
Tenant-filtered queries ensure strict data isolation between tenants.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from bernstein.core.tenanting import (
    DEFAULT_TENANT_ID,
    TenantRegistry,
    ensure_tenant_layout,
    normalize_tenant_id,
    tenant_paths,
)

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class TenantDataPaths:
    """Complete set of tenant-scoped data paths.

    Extends the basic TenantPaths with WAL and audit directories.
    """

    root: Path
    backlog_dir: Path
    metrics_dir: Path
    wal_dir: Path
    audit_dir: Path
    runtime_dir: Path


def tenant_data_paths(sdd_dir: Path, tenant_id: str) -> TenantDataPaths:
    """Build fully-qualified tenant data paths.

    Args:
        sdd_dir: Root ``.sdd`` directory.
        tenant_id: Tenant identifier.

    Returns:
        TenantDataPaths with all scoped subdirectories.
    """
    base = tenant_paths(sdd_dir, tenant_id)
    return TenantDataPaths(
        root=base.root,
        backlog_dir=base.backlog_dir,
        metrics_dir=base.metrics_dir,
        wal_dir=base.root / "runtime" / "wal",
        audit_dir=base.root / "audit",
        runtime_dir=base.root / "runtime",
    )


def ensure_tenant_data_layout(sdd_dir: Path, tenant_id: str) -> TenantDataPaths:
    """Create all tenant-scoped directories on disk.

    Args:
        sdd_dir: Root ``.sdd`` directory.
        tenant_id: Tenant identifier.

    Returns:
        TenantDataPaths with directories created.
    """
    ensure_tenant_layout(sdd_dir, tenant_id)
    paths = tenant_data_paths(sdd_dir, tenant_id)
    paths.wal_dir.mkdir(parents=True, exist_ok=True)
    paths.audit_dir.mkdir(parents=True, exist_ok=True)
    paths.runtime_dir.mkdir(parents=True, exist_ok=True)
    return paths


@dataclass
class TenantQuota:
    """Resource quota for a tenant.

    Attributes:
        max_tasks: Maximum concurrent tasks.
        max_agents: Maximum concurrent agents.
        budget_usd: Maximum cost budget.
        max_wal_entries: Maximum WAL entries before rotation.
    """

    max_tasks: int = 100
    max_agents: int = 10
    budget_usd: float = 100.0
    max_wal_entries: int = 10000


@dataclass
class TenantIsolationContext:
    """Runtime context for tenant-scoped operations.

    Provides a scoped view of the system for a single tenant, ensuring
    that all queries, writes, and metrics are isolated.
    """

    tenant_id: str
    paths: TenantDataPaths
    quota: TenantQuota = field(default_factory=TenantQuota)

    @property
    def normalized_id(self) -> str:
        """Return the normalized tenant identifier."""
        return normalize_tenant_id(self.tenant_id)


class TenantIsolationManager:
    """Manages tenant isolation for task store, WAL, and metrics.

    Ensures that each tenant's data is stored in isolated directories
    and that queries are scoped to the requesting tenant.
    """

    def __init__(self, sdd_dir: Path, registry: TenantRegistry | None = None) -> None:
        self._sdd_dir = sdd_dir
        self._registry = registry or TenantRegistry()
        self._contexts: dict[str, TenantIsolationContext] = {}
        self._quotas: dict[str, TenantQuota] = {}

    @property
    def sdd_dir(self) -> Path:
        """Return the root ``.sdd`` directory."""
        return self._sdd_dir

    def register_quota(self, tenant_id: str, quota: TenantQuota) -> None:
        """Set a resource quota for a tenant.

        Args:
            tenant_id: Tenant identifier.
            quota: Resource limits.
        """
        normalized = normalize_tenant_id(tenant_id)
        self._quotas[normalized] = quota

    def get_context(self, tenant_id: str) -> TenantIsolationContext:
        """Get or create the isolation context for a tenant.

        Args:
            tenant_id: Tenant identifier.

        Returns:
            TenantIsolationContext for the given tenant.
        """
        normalized = normalize_tenant_id(tenant_id)
        if normalized not in self._contexts:
            paths = ensure_tenant_data_layout(self._sdd_dir, normalized)
            quota = self._quotas.get(normalized, TenantQuota())
            self._contexts[normalized] = TenantIsolationContext(
                tenant_id=normalized,
                paths=paths,
                quota=quota,
            )
        return self._contexts[normalized]

    def filter_tasks(
        self,
        tasks: dict[str, Any],
        tenant_id: str,
    ) -> dict[str, Any]:
        """Filter a task dict to only include tasks belonging to a tenant.

        Args:
            tasks: Mapping of task_id to task objects.
            tenant_id: Tenant to filter for.

        Returns:
            Filtered dict containing only the tenant's tasks.
        """
        normalized = normalize_tenant_id(tenant_id)
        return {tid: task for tid, task in tasks.items() if getattr(task, "tenant_id", DEFAULT_TENANT_ID) == normalized}

    def check_quota(self, tenant_id: str, current_task_count: int) -> tuple[bool, str]:
        """Check whether a tenant can create another task.

        Args:
            tenant_id: Tenant identifier.
            current_task_count: Number of active tasks the tenant already has.

        Returns:
            (allowed, reason) tuple.
        """
        ctx = self.get_context(tenant_id)
        if current_task_count >= ctx.quota.max_tasks:
            return False, f"Tenant {ctx.tenant_id} has reached max_tasks limit ({ctx.quota.max_tasks})"
        return True, ""

    def list_tenants(self) -> list[str]:
        """Return all known tenant IDs.

        Returns:
            Sorted list of tenant identifiers.
        """
        tenants: set[str] = set()
        for cfg in self._registry.tenants:
            tenants.add(cfg.id)
        for tid in self._contexts:
            tenants.add(tid)
        return sorted(tenants)

    def persist_state(self) -> None:
        """Persist tenant isolation state to disk."""
        state_dir = self._sdd_dir / "config"
        state_dir.mkdir(parents=True, exist_ok=True)
        state: dict[str, Any] = {
            "tenants": {
                tid: {
                    "quota": {
                        "max_tasks": ctx.quota.max_tasks,
                        "max_agents": ctx.quota.max_agents,
                        "budget_usd": ctx.quota.budget_usd,
                        "max_wal_entries": ctx.quota.max_wal_entries,
                    },
                }
                for tid, ctx in self._contexts.items()
            },
        }
        path = state_dir / "tenant_isolation.json"
        path.write_text(json.dumps(state, indent=2))

    def load_state(self) -> None:
        """Load persisted tenant isolation state from disk."""
        path = self._sdd_dir / "config" / "tenant_isolation.json"
        if not path.exists():
            return
        try:
            data = json.loads(path.read_text())
            for tid, info in data.get("tenants", {}).items():
                quota_raw = info.get("quota", {})
                quota = TenantQuota(
                    max_tasks=quota_raw.get("max_tasks", 100),
                    max_agents=quota_raw.get("max_agents", 10),
                    budget_usd=quota_raw.get("budget_usd", 100.0),
                    max_wal_entries=quota_raw.get("max_wal_entries", 10000),
                )
                self._quotas[tid] = quota
                # Ensure context is available
                self.get_context(tid)
        except (json.JSONDecodeError, KeyError) as exc:
            logger.warning("Failed to load tenant isolation state: %s", exc)
