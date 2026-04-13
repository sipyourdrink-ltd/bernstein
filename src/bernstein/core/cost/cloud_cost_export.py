"""Cloud cost management platform integration.

Exports Bernstein run costs to external cost management platforms
(CloudHealth, Kubecost, Spot.io) or as generic CSV.  This module
builds payloads only -- it never calls external APIs.

Archive records are read from ``.sdd/archive/tasks.jsonl`` and
transformed into platform-specific allocation structures for
downstream submission.

Example::

    from pathlib import Path
    from bernstein.core.cost.cloud_cost_export import (
        CostExporter,
        CostExportConfig,
        CostPlatform,
        aggregate_costs_by_role,
    )

    config = CostExportConfig(
        platform=CostPlatform.KUBECOST,
        cost_center="eng-platform",
        project_tag="bernstein-v2",
    )
    exporter = CostExporter()
    allocations = exporter.export_run_costs("run-42", Path(".sdd/archive/tasks.jsonl"), config)
"""

from __future__ import annotations

import csv
import io
import json
import logging
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class CostPlatform(StrEnum):
    """Supported cloud cost management platforms."""

    CLOUDHEALTH = "cloudhealth"
    KUBECOST = "kubecost"
    SPOT_IO = "spot_io"
    GENERIC = "generic"


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CostAllocation:
    """A single cost allocation record for export.

    Attributes:
        resource_id: Unique identifier for the resource (e.g. task ID).
        cost_usd: Cost in USD.
        currency: ISO 4217 currency code.
        period_start: ISO 8601 start of the billing period.
        period_end: ISO 8601 end of the billing period.
        labels: Arbitrary key-value labels for grouping / filtering.
        namespace: Optional Kubernetes namespace or organisational unit.
        account_id: Optional cloud account or billing account identifier.
    """

    resource_id: str
    cost_usd: float
    currency: str = "USD"
    period_start: str = ""
    period_end: str = ""
    labels: dict[str, str] = field(default_factory=lambda: {})
    namespace: str | None = None
    account_id: str | None = None


@dataclass(frozen=True)
class CostExportConfig:
    """Configuration for cost export to a specific platform.

    Attributes:
        platform: Target cost management platform.
        api_endpoint: Optional API endpoint URL for the platform.
        cost_center: Optional cost center tag for allocation.
        project_tag: Optional project tag for grouping.
        namespace: Optional default namespace for all allocations.
    """

    platform: CostPlatform
    api_endpoint: str | None = None
    cost_center: str | None = None
    project_tag: str | None = None
    namespace: str | None = None


# ---------------------------------------------------------------------------
# Archive reading (reuses the pattern from pareto_frontier)
# ---------------------------------------------------------------------------


def _read_archive(archive_path: Path) -> list[dict[str, object]]:
    """Read all records from an archive JSONL file.

    Args:
        archive_path: Path to ``.sdd/archive/tasks.jsonl``.

    Returns:
        List of parsed JSON dicts.  Malformed lines are silently skipped.
    """
    if not archive_path.exists():
        return []

    records: list[dict[str, object]] = []
    try:
        with archive_path.open(encoding="utf-8") as f:
            for line_num, raw_line in enumerate(f, 1):
                line = raw_line.strip()
                if not line:
                    continue
                try:
                    data: dict[str, object] = json.loads(line)
                    records.append(data)
                except json.JSONDecodeError:
                    logger.warning(
                        "Skipping malformed archive line %d in %s",
                        line_num,
                        archive_path,
                    )
    except OSError as exc:
        logger.warning("Cannot read archive at %s: %s", archive_path, exc)
    return records


def _extract_resource_id(record: dict[str, object]) -> str:
    """Extract resource_id from task_id or id fields."""
    for key in ("task_id", "id"):
        val = record.get(key)
        if isinstance(val, str) and val:
            return val
    return ""


def _build_allocation_labels(record: dict[str, object], config: CostExportConfig) -> dict[str, str]:
    """Build label dict from record fields and config defaults."""
    labels: dict[str, str] = {}
    for label_key in ("role", "model", "assigned_model", "status", "scope", "complexity"):
        val = record.get(label_key)
        if isinstance(val, str) and val:
            labels[label_key] = val
    if config.cost_center:
        labels["cost_center"] = config.cost_center
    if config.project_tag:
        labels["project"] = config.project_tag
    return labels


def _format_epoch_timestamp(raw: object) -> str:
    """Format a numeric epoch to ISO string, or empty string."""
    if isinstance(raw, (int, float)) and raw > 0:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(raw))
    return ""


def _record_to_allocation(
    record: dict[str, object],
    config: CostExportConfig,
) -> CostAllocation | None:
    """Convert a single archive record into a CostAllocation.

    Returns ``None`` when the record lacks the minimum required fields
    (``cost_usd`` and some form of identifier).

    Args:
        record: A single JSONL archive record.
        config: Export configuration (used for default labels).

    Returns:
        A :class:`CostAllocation` or ``None``.
    """
    cost_raw = record.get("cost_usd")
    if not isinstance(cost_raw, (int, float)) or cost_raw < 0:
        return None

    resource_id = _extract_resource_id(record)
    if not resource_id:
        return None

    period_start = _format_epoch_timestamp(record.get("timestamp"))
    period_end = _format_epoch_timestamp(record.get("completed_at")) or period_start

    return CostAllocation(
        resource_id=resource_id,
        cost_usd=float(cost_raw),
        currency="USD",
        period_start=period_start,
        period_end=period_end,
        labels=_build_allocation_labels(record, config),
        namespace=config.namespace,
        account_id=None,
    )


# ---------------------------------------------------------------------------
# CostExporter
# ---------------------------------------------------------------------------


class CostExporter:
    """Builds cost export payloads for cloud cost management platforms.

    All methods are pure transformations -- no HTTP calls are made.
    """

    def export_run_costs(
        self,
        run_id: str,
        archive_path: Path,
        config: CostExportConfig,
    ) -> list[CostAllocation]:
        """Read costs from the archive and return allocations for a run.

        Args:
            run_id: Identifier for the orchestration run.
            archive_path: Path to ``.sdd/archive/tasks.jsonl``.
            config: Export configuration.

        Returns:
            List of :class:`CostAllocation` records.
        """
        records = _read_archive(archive_path)
        allocations: list[CostAllocation] = []
        for rec in records:
            alloc = _record_to_allocation(rec, config)
            if alloc is not None:
                # Inject run_id into labels
                merged_labels = {**alloc.labels, "run_id": run_id}
                allocations.append(
                    CostAllocation(
                        resource_id=alloc.resource_id,
                        cost_usd=alloc.cost_usd,
                        currency=alloc.currency,
                        period_start=alloc.period_start,
                        period_end=alloc.period_end,
                        labels=merged_labels,
                        namespace=alloc.namespace,
                        account_id=alloc.account_id,
                    )
                )
        return allocations

    # ------------------------------------------------------------------
    # Platform-specific payload builders
    # ------------------------------------------------------------------

    def build_cloudhealth_payload(
        self,
        allocations: list[CostAllocation],
    ) -> dict[str, Any]:
        """Build a CloudHealth custom charges API payload.

        Produces the structure expected by the CloudHealth
        ``POST /custom_charges`` endpoint.

        Args:
            allocations: Cost allocation records.

        Returns:
            JSON-serialisable dict matching the CloudHealth API schema.
        """
        line_items: list[dict[str, Any]] = []
        for alloc in allocations:
            item: dict[str, Any] = {
                "resource_id": alloc.resource_id,
                "charge": alloc.cost_usd,
                "currency": alloc.currency,
                "time_interval": {
                    "start": alloc.period_start,
                    "end": alloc.period_end,
                },
                "tags": alloc.labels,
            }
            if alloc.account_id:
                item["account_id"] = alloc.account_id
            line_items.append(item)

        return {
            "custom_charges": line_items,
            "total": sum(a.cost_usd for a in allocations),
            "currency": "USD",
        }

    def build_kubecost_payload(
        self,
        allocations: list[CostAllocation],
    ) -> dict[str, Any]:
        """Build a Kubecost allocation API payload.

        Produces the structure consumed by the Kubecost allocation
        import endpoint.

        Args:
            allocations: Cost allocation records.

        Returns:
            JSON-serialisable dict matching the Kubecost schema.
        """
        items: list[dict[str, Any]] = []
        for alloc in allocations:
            item: dict[str, Any] = {
                "name": alloc.resource_id,
                "properties": {
                    "namespace": alloc.namespace or "default",
                    "labels": alloc.labels,
                },
                "window": {
                    "start": alloc.period_start,
                    "end": alloc.period_end,
                },
                "totalCost": alloc.cost_usd,
            }
            items.append(item)

        return {
            "code": 200,
            "data": items,
            "totalCost": sum(a.cost_usd for a in allocations),
        }

    def build_spotio_payload(
        self,
        allocations: list[CostAllocation],
    ) -> dict[str, Any]:
        """Build a Spot.io billing events payload.

        Produces the structure expected by the Spot.io billing events
        import API.

        Args:
            allocations: Cost allocation records.

        Returns:
            JSON-serialisable dict matching the Spot.io schema.
        """
        events: list[dict[str, Any]] = []
        for alloc in allocations:
            event: dict[str, Any] = {
                "eventType": "cost_allocation",
                "resourceId": alloc.resource_id,
                "cost": alloc.cost_usd,
                "currency": alloc.currency,
                "startTime": alloc.period_start,
                "endTime": alloc.period_end,
                "metadata": alloc.labels,
            }
            if alloc.namespace:
                event["namespace"] = alloc.namespace
            if alloc.account_id:
                event["accountId"] = alloc.account_id
            events.append(event)

        return {
            "events": events,
            "count": len(events),
            "totalCost": sum(a.cost_usd for a in allocations),
        }

    def build_generic_csv(
        self,
        allocations: list[CostAllocation],
    ) -> str:
        """Build a generic CSV export string.

        Produces a CSV with one row per allocation, suitable for import
        into any cost management platform or spreadsheet.

        Args:
            allocations: Cost allocation records.

        Returns:
            CSV string with header row.
        """
        fieldnames = [
            "resource_id",
            "cost_usd",
            "currency",
            "period_start",
            "period_end",
            "namespace",
            "account_id",
            "labels",
        ]
        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=fieldnames)
        writer.writeheader()
        for alloc in allocations:
            writer.writerow(
                {
                    "resource_id": alloc.resource_id,
                    "cost_usd": f"{alloc.cost_usd:.6f}",
                    "currency": alloc.currency,
                    "period_start": alloc.period_start,
                    "period_end": alloc.period_end,
                    "namespace": alloc.namespace or "",
                    "account_id": alloc.account_id or "",
                    "labels": json.dumps(alloc.labels, sort_keys=True),
                }
            )
        return output.getvalue()

    def get_headers(self, config: CostExportConfig) -> dict[str, str]:
        """Return auth / content-type headers for a platform.

        Provides the baseline headers each platform API expects.  The
        actual API key must be injected by the caller (e.g. from env).

        Args:
            config: Export configuration with platform info.

        Returns:
            Dict of HTTP header name -> value.
        """
        base: dict[str, str] = {"Content-Type": "application/json"}

        if config.platform == CostPlatform.CLOUDHEALTH:
            base["Authorization"] = "Bearer <CLOUDHEALTH_API_KEY>"
        elif config.platform == CostPlatform.KUBECOST:
            # Kubecost typically uses no auth or basic auth
            pass
        elif config.platform == CostPlatform.SPOT_IO:
            base["Authorization"] = "Bearer <SPOTINST_TOKEN>"
        elif config.platform == CostPlatform.GENERIC:
            # Generic export is file-based; no HTTP headers needed
            base.clear()

        return base


# ---------------------------------------------------------------------------
# Aggregation helpers
# ---------------------------------------------------------------------------


def aggregate_costs_by_role(archive_path: Path) -> dict[str, float]:
    """Aggregate costs from the archive grouped by task role.

    Args:
        archive_path: Path to ``.sdd/archive/tasks.jsonl``.

    Returns:
        Dict mapping role name to total cost in USD.
    """
    records = _read_archive(archive_path)
    rollup: dict[str, float] = {}
    for rec in records:
        role = rec.get("role")
        cost = rec.get("cost_usd")
        if isinstance(role, str) and role and isinstance(cost, (int, float)) and cost >= 0:
            rollup[role] = rollup.get(role, 0.0) + float(cost)
    return dict(sorted(rollup.items()))


def aggregate_costs_by_model(archive_path: Path) -> dict[str, float]:
    """Aggregate costs from the archive grouped by model.

    Args:
        archive_path: Path to ``.sdd/archive/tasks.jsonl``.

    Returns:
        Dict mapping model name to total cost in USD.
    """
    records = _read_archive(archive_path)
    rollup: dict[str, float] = {}
    for rec in records:
        model: str | None = None
        for key in ("model", "assigned_model"):
            val = rec.get(key)
            if isinstance(val, str) and val:
                model = val
                break
        cost = rec.get("cost_usd")
        if model and isinstance(cost, (int, float)) and cost >= 0:
            rollup[model] = rollup.get(model, 0.0) + float(cost)
    return dict(sorted(rollup.items()))


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _group_allocations_by_label(
    allocations: list[CostAllocation],
    label_key: str,
    fallback_key: str | None = None,
) -> dict[str, float]:
    """Group allocations by a label key and sum costs."""
    totals: dict[str, float] = {}
    for alloc in allocations:
        value = alloc.labels.get(label_key)
        if value is None and fallback_key is not None:
            value = alloc.labels.get(fallback_key)
        value = value or "unknown"
        totals[value] = totals.get(value, 0.0) + alloc.cost_usd
    return totals


def _render_grouped_table(
    header: str,
    column_name: str,
    totals: dict[str, float],
    grand_total: float,
) -> list[str]:
    """Render a grouped summary table section."""
    if not totals:
        return []
    lines = [
        f"### {header}",
        "",
        f"| {column_name} | Cost (USD) | % of Total |",
        f"|{'---' * (len(column_name) // 3 + 1)}|-----------|------------|",
    ]
    for name, cost in sorted(totals.items(), key=lambda x: -x[1]):
        pct = (cost / grand_total * 100) if grand_total > 0 else 0.0
        lines.append(f"| {name} | ${cost:.4f} | {pct:.1f}% |")
    return lines


def render_cost_allocation_report(allocations: list[CostAllocation]) -> str:
    """Render a Markdown breakdown of cost allocations.

    Produces a table listing each allocation and a summary section with
    totals grouped by label values.

    Args:
        allocations: Cost allocation records.

    Returns:
        Markdown-formatted report string.
    """
    lines: list[str] = ["## Cost Allocation Report", ""]

    if not allocations:
        lines.append("No allocations to report.")
        return "\n".join(lines)

    lines.extend(
        [
            "### Allocations",
            "",
            "| Resource | Cost (USD) | Period Start | Role | Model |",
            "|----------|-----------|--------------|------|-------|",
        ]
    )

    total = 0.0
    for alloc in allocations:
        role = alloc.labels.get("role", "-")
        model = alloc.labels.get("model", alloc.labels.get("assigned_model", "-"))
        lines.append(
            f"| {alloc.resource_id} | ${alloc.cost_usd:.4f} | {alloc.period_start or '-'} | {role} | {model} |"
        )
        total += alloc.cost_usd

    lines.extend(["", f"**Total: ${total:.4f}**", ""])

    role_totals = _group_allocations_by_label(allocations, "role")
    lines.extend(_render_grouped_table("By Role", "Role", role_totals, total))

    model_totals = _group_allocations_by_label(allocations, "model", fallback_key="assigned_model")
    if model_totals:
        lines.append("")
    lines.extend(_render_grouped_table("By Model", "Model", model_totals, total))

    return "\n".join(lines)
