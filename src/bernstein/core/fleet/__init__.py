"""Fleet dashboard — supervise multiple Bernstein projects in one view.

The fleet module aggregates per-project state (status, bulletin, cost,
audit, Prometheus metrics, SSE events) from a list of locally-running
task servers configured in ``~/.config/bernstein/projects.toml``.

The aggregator is purely a fan-out reader plus dispatcher for bulk
actions; it does not own any orchestration state itself. This keeps the
deterministic single-project guarantees intact.

Public surface:
    * :class:`ProjectConfig` and :func:`load_projects_config`
    * :class:`FleetAggregator` and :class:`ProjectSnapshot`
    * :class:`FleetCostRollup`
    * :class:`AuditChainStatus` and :func:`check_audit_tail`
    * :func:`merge_prometheus_metrics`
    * :func:`build_fleet_app` (FastAPI factory)
    * :class:`FleetTUI`
"""

from __future__ import annotations

from bernstein.core.fleet.aggregator import (
    AggregatorEvent,
    FleetAggregator,
    ProjectSnapshot,
    ProjectState,
)
from bernstein.core.fleet.audit import (
    AuditChainStatus,
    AuditEntry,
    check_audit_tail,
    filter_audit_entries,
)
from bernstein.core.fleet.bulk import (
    BulkActionResult,
    bulk_cost_report,
    bulk_pause,
    bulk_resume,
    bulk_stop,
    select_projects,
)
from bernstein.core.fleet.config import (
    FleetConfig,
    FleetConfigError,
    ProjectConfig,
    default_projects_config_path,
    load_projects_config,
)
from bernstein.core.fleet.cost_rollup import (
    CostSparkline,
    FleetCostRollup,
    rollup_costs,
)
from bernstein.core.fleet.prometheus_proxy import merge_prometheus_metrics

__all__ = [
    "AggregatorEvent",
    "AuditChainStatus",
    "AuditEntry",
    "BulkActionResult",
    "CostSparkline",
    "FleetAggregator",
    "FleetConfig",
    "FleetConfigError",
    "FleetCostRollup",
    "ProjectConfig",
    "ProjectSnapshot",
    "ProjectState",
    "bulk_cost_report",
    "bulk_pause",
    "bulk_resume",
    "bulk_stop",
    "check_audit_tail",
    "default_projects_config_path",
    "filter_audit_entries",
    "load_projects_config",
    "merge_prometheus_metrics",
    "rollup_costs",
    "select_projects",
]
