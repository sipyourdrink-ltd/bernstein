"""Grafana dashboard JSON template generator for Bernstein metrics."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path


def generate_grafana_dashboard(datasource: str = "Prometheus") -> dict[str, Any]:
    """Generate Grafana dashboard JSON template for Bernstein metrics.

    Args:
        datasource: Prometheus datasource name.

    Returns:
        Grafana dashboard JSON as dictionary.
    """
    return {
        "dashboard": {
            "id": None,
            "uid": "bernstein-metrics",
            "title": "Bernstein Orchestration Metrics",
            "tags": ["bernstein", "orchestration"],
            "timezone": "browser",
            "schemaVersion": 38,
            "version": 1,
            "refresh": "30s",
            "panels": [
                _create_task_completion_panel(datasource),
                _create_agent_status_panel(datasource),
                _create_cost_tracking_panel(datasource),
                _create_quality_gates_panel(datasource),
                _create_token_usage_panel(datasource),
                _create_error_rate_panel(datasource),
            ],
            "time": {"from": "now-6h", "to": "now"},
            "templating": {
                "list": [
                    {
                        "name": "role",
                        "type": "query",
                        "datasource": datasource,
                        "query": "label_values(task_duration_seconds, role)",
                        "refresh": 2,
                        "multi": True,
                        "includeAll": True,
                    }
                ]
            },
        }
    }


def _create_task_completion_panel(datasource: str) -> dict[str, Any]:
    """Create task completion rate panel."""
    return {
        "id": 1,
        "type": "graph",
        "title": "Task Completion Rate",
        "gridPos": {"h": 8, "w": 12, "x": 0, "y": 0},
        "targets": [
            {
                "expr": "rate(bernstein_tasks_completed_total[5m])",
                "legendFormat": "Completed",
                "refId": "A",
            },
            {
                "expr": "rate(bernstein_tasks_failed_total[5m])",
                "legendFormat": "Failed",
                "refId": "B",
            },
        ],
        "yaxes": [
            {"label": "Tasks/min", "show": True},
            {"show": False},
        ],
    }


def _create_agent_status_panel(datasource: str) -> dict[str, Any]:
    """Create agent status panel."""
    return {
        "id": 2,
        "type": "stat",
        "title": "Active Agents",
        "gridPos": {"h": 4, "w": 6, "x": 12, "y": 0},
        "targets": [
            {
                "expr": "sum(bernstein_agents_active)",
                "legendFormat": "Active",
                "refId": "A",
            }
        ],
        "colorMode": "value",
        "graphMode": "area",
        "mappings": [],
        "thresholds": {
            "mode": "absolute",
            "steps": [
                {"color": "green", "value": None},
                {"color": "yellow", "value": 5},
                {"color": "red", "value": 10},
            ],
        },
    }


def _create_cost_tracking_panel(datasource: str) -> dict[str, Any]:
    """Create cost tracking panel."""
    return {
        "id": 3,
        "type": "graph",
        "title": "Cost Tracking (USD)",
        "gridPos": {"h": 8, "w": 12, "x": 0, "y": 8},
        "targets": [
            {
                "expr": "bernstein_cost_usd_total",
                "legendFormat": "Total Cost",
                "refId": "A",
            },
            {
                "expr": "rate(bernstein_cost_usd_total[1h])",
                "legendFormat": "Cost/Hour",
                "refId": "B",
            },
        ],
        "yaxes": [
            {"label": "USD", "show": True},
            {"show": False},
        ],
    }


def _create_quality_gates_panel(datasource: str) -> dict[str, Any]:
    """Create quality gates pass rate panel."""
    return {
        "id": 4,
        "type": "gauge",
        "title": "Quality Gate Pass Rate",
        "gridPos": {"h": 6, "w": 6, "x": 12, "y": 4},
        "targets": [
            {
                "expr": "bernstein_quality_gate_pass_rate * 100",
                "legendFormat": "Pass Rate %",
                "refId": "A",
            }
        ],
        "fieldConfig": {
            "defaults": {
                "min": 0,
                "max": 100,
                "unit": "percent",
                "thresholds": {
                    "mode": "absolute",
                    "steps": [
                        {"color": "red", "value": None},
                        {"color": "yellow", "value": 70},
                        {"color": "green", "value": 90},
                    ],
                },
            }
        },
    }


def _create_token_usage_panel(datasource: str) -> dict[str, Any]:
    """Create token usage panel."""
    return {
        "id": 5,
        "type": "graph",
        "title": "Token Usage",
        "gridPos": {"h": 8, "w": 12, "x": 0, "y": 16},
        "targets": [
            {
                "expr": "rate(bernstein_tokens_total[5m])",
                "legendFormat": "Tokens/min",
                "refId": "A",
            },
            {
                "expr": "rate(bernstein_tokens_prompt[5m])",
                "legendFormat": "Prompt",
                "refId": "B",
            },
            {
                "expr": "rate(bernstein_tokens_completion[5m])",
                "legendFormat": "Completion",
                "refId": "C",
            },
        ],
        "yaxes": [
            {"label": "Tokens/min", "show": True},
            {"show": False},
        ],
    }


def _create_error_rate_panel(datasource: str) -> dict[str, Any]:
    """Create error rate panel."""
    return {
        "id": 6,
        "type": "graph",
        "title": "Error Rate",
        "gridPos": {"h": 8, "w": 12, "x": 12, "y": 10},
        "targets": [
            {
                "expr": "rate(bernstein_errors_total[5m])",
                "legendFormat": "Errors/min",
                "refId": "A",
            }
        ],
        "yaxes": [
            {"label": "Errors/min", "show": True},
            {"show": False},
        ],
        "alert": {
            "name": "High Error Rate",
            "conditions": [
                {
                    "evaluator": {"params": [0.1], "type": "gt"},
                    "operator": {"type": "and"},
                    "query": {"params": ["A", "5m", "now"]},
                    "reducer": {"type": "avg"},
                }
            ],
            "executionErrorState": "alerting",
            "frequency": "1m",
            "handler": 1,
            "message": "Error rate is above threshold",
            "noDataState": "no_data",
            "notifications": [],
        },
    }


def save_dashboard(output_path: Path, datasource: str = "Prometheus") -> Path:
    """Save Grafana dashboard JSON to file.

    Args:
        output_path: Path to save dashboard JSON.
        datasource: Prometheus datasource name.

    Returns:
        Path to saved file.
    """
    dashboard = generate_grafana_dashboard(datasource)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(dashboard, indent=2))
    return output_path
