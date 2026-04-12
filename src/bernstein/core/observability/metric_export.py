"""Metrics export and reporting functionality."""

from __future__ import annotations

import json
from dataclasses import asdict, is_dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.differential_privacy import DPConfig
    from bernstein.core.metric_collector import MetricsCollector


def _serialize_residency_attestations(raw: object) -> list[dict[str, object]]:
    """Convert residency attestation objects into JSON-safe dictionaries."""
    if not isinstance(raw, list):
        return []

    serialized: list[dict[str, object]] = []
    for item in cast("list[object]", raw):
        if not isinstance(item, type) and is_dataclass(cast("Any", item)):
            serialized.append(cast("dict[str, object]", asdict(cast("Any", item))))
        elif isinstance(item, dict):
            typed_item = cast("dict[object, object]", item)
            serialized.append({str(key): value for key, value in typed_item.items()})
    return serialized


def export_metrics(
    collector: MetricsCollector,
    output_path: Path,
    dp_config: DPConfig | None = None,
) -> None:
    """Export all metrics to a JSON file.

    When *dp_config* is provided the numeric telemetry fields are privatised via
    the Gaussian mechanism before writing, preventing re-identification of
    developers or proprietary usage patterns from the exported data.

    Args:
        collector: MetricsCollector instance.
        output_path: Path to write the export.
        dp_config: Optional (ε, δ)-DP configuration.  Pass a
            :class:`~bernstein.core.differential_privacy.DPConfig` to apply
            differential privacy noise before export.  ``None`` (default) writes
            raw metrics.
    """
    data = {
        "exported_at": datetime.now().isoformat(),
        "summary": collector.get_metrics_summary(),
        "task_metrics": [
            {
                "task_id": t.task_id,
                "role": t.role,
                "model": t.model,
                "provider": t.provider,
                "duration_seconds": (t.end_time - t.start_time) if t.end_time else None,
                "success": t.success,
                "tokens_used": t.tokens_used,
                "cost_usd": t.cost_usd,
                "error": t.error,
            }
            for t in collector.task_metrics.values()
        ],
        "agent_metrics": [
            {
                "agent_id": a.agent_id,
                "role": a.role,
                "tasks_completed": a.tasks_completed,
                "tasks_failed": a.tasks_failed,
                "total_tokens": a.total_tokens,
                "total_cost_usd": a.total_cost_usd,
            }
            for a in collector.agent_metrics.values()
        ],
        "residency_attestations": _serialize_residency_attestations(
            getattr(collector, "residency_attestations", cast("Any", []))
        ),
    }

    if dp_config is not None:
        from bernstein.core.differential_privacy import apply_dp_to_export

        data = apply_dp_to_export(data, dp_config)

    with output_path.open("w") as f:
        json.dump(data, f, indent=2)
