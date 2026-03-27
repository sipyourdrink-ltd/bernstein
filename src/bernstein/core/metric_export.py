"""Metrics export and reporting functionality."""

from __future__ import annotations

import json
from datetime import datetime
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.metric_collector import MetricsCollector


def export_metrics(collector: MetricsCollector, output_path: Path) -> None:
    """Export all metrics to a JSON file.

    Args:
        collector: MetricsCollector instance.
        output_path: Path to write the export.
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
    }

    with output_path.open("w") as f:
        json.dump(data, f, indent=2)
