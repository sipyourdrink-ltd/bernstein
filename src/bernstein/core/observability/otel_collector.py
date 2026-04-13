"""OpenTelemetry Collector config generator with pre-built Grafana dashboards.

Generates OTel Collector YAML configuration and Grafana dashboard JSON for
Bernstein metrics.  This module does NOT import the OpenTelemetry SDK; it
produces static configuration artifacts that operators deploy alongside
their collector and Grafana instances.

Usage::

    from bernstein.core.observability.otel_collector import (
        OTelConfig,
        generate_collector_config,
        generate_grafana_dashboard,
        get_default_panels,
        render_config_yaml,
        export_dashboard_json,
    )

    config = OTelConfig(endpoint="http://localhost:4317")
    collector_yaml = render_config_yaml(generate_collector_config(config))
    dashboard = generate_grafana_dashboard(get_default_panels())
    export_dashboard_json(dashboard, Path("dashboard.json"))
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from pathlib import Path

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------

PanelType = Literal["graph", "stat", "table"]
OTelProtocol = Literal["grpc", "http"]


@dataclass(frozen=True)
class OTelConfig:
    """OpenTelemetry Collector connection and export configuration.

    Attributes:
        endpoint: Collector OTLP endpoint URL.
        protocol: Transport protocol — ``grpc`` or ``http``.
        service_name: Logical service name attached to all telemetry.
        resource_attributes: Extra OTEL resource attributes to attach.
        export_interval_s: Metric export interval in seconds.
    """

    endpoint: str = "http://localhost:4317"
    protocol: OTelProtocol = "grpc"
    service_name: str = "bernstein"
    resource_attributes: dict[str, str] = field(default_factory=lambda: {})
    export_interval_s: int = 30


@dataclass(frozen=True)
class CollectorPipeline:
    """An OTel Collector pipeline definition.

    Attributes:
        receivers: Tuple of receiver names (e.g. ``("otlp",)``).
        processors: Tuple of processor names (e.g. ``("batch",)``).
        exporters: Tuple of exporter names (e.g. ``("prometheus",)``).
    """

    receivers: tuple[str, ...] = ("otlp",)
    processors: tuple[str, ...] = ("batch",)
    exporters: tuple[str, ...] = ("prometheus",)


@dataclass(frozen=True)
class DashboardPanel:
    """A single Grafana dashboard panel definition.

    Attributes:
        title: Human-readable panel title.
        query: PromQL query expression for the panel.
        panel_type: Grafana panel visualisation type.
        description: Brief explanation shown as panel tooltip.
    """

    title: str
    query: str
    panel_type: PanelType = "graph"
    description: str = ""


# ---------------------------------------------------------------------------
# Collector config generation
# ---------------------------------------------------------------------------

_DEFAULT_GRPC_PROTOCOLS: dict[str, Any] = {"grpc": {"endpoint": "0.0.0.0:4317"}}
_DEFAULT_HTTP_PROTOCOLS: dict[str, Any] = {"http": {"endpoint": "0.0.0.0:4318"}}


def generate_collector_config(config: OTelConfig) -> dict[str, Any]:
    """Generate an OTel Collector configuration dictionary.

    The returned dict mirrors the structure of ``otel-collector-config.yaml``
    and can be serialised with :func:`render_config_yaml`.

    Args:
        config: Collector connection settings.

    Returns:
        Collector configuration as a nested dictionary.
    """
    protocols: dict[str, Any] = _DEFAULT_GRPC_PROTOCOLS if config.protocol == "grpc" else _DEFAULT_HTTP_PROTOCOLS

    resource_attrs = {"service.name": config.service_name, **config.resource_attributes}
    resource_attr_str = ",".join(f"{k}={v}" for k, v in resource_attrs.items())

    return {
        "receivers": {
            "otlp": {
                "protocols": protocols,
            },
        },
        "processors": {
            "batch": {
                "timeout": "5s",
                "send_batch_size": 512,
            },
            "resource": {
                "attributes": [{"key": k, "value": v, "action": "upsert"} for k, v in resource_attrs.items()],
            },
            "memory_limiter": {
                "check_interval": "1s",
                "limit_mib": 512,
                "spike_limit_mib": 128,
            },
        },
        "exporters": {
            "prometheus": {
                "endpoint": "0.0.0.0:8889",
                "namespace": "bernstein",
            },
            "otlp": {
                "endpoint": config.endpoint,
                "tls": {"insecure": True},
            },
            "logging": {
                "loglevel": "info",
            },
        },
        "service": {
            "pipelines": {
                "metrics": {
                    "receivers": ["otlp"],
                    "processors": ["memory_limiter", "batch", "resource"],
                    "exporters": ["prometheus", "otlp"],
                },
                "traces": {
                    "receivers": ["otlp"],
                    "processors": ["memory_limiter", "batch", "resource"],
                    "exporters": ["otlp", "logging"],
                },
            },
            "telemetry": {
                "logs": {"level": "info"},
                "metrics": {"address": "0.0.0.0:8888"},
            },
        },
        "extensions": {
            "health_check": {"endpoint": "0.0.0.0:13133"},
            "zpages": {"endpoint": "0.0.0.0:55679"},
        },
        "_meta": {
            "resource_attributes": resource_attr_str,
            "export_interval_s": config.export_interval_s,
        },
    }


# ---------------------------------------------------------------------------
# Grafana dashboard generation
# ---------------------------------------------------------------------------

_GRID_WIDTH = 24
_PANEL_W_HALF = 12
_PANEL_W_THIRD = 8
_PANEL_H = 8
_STAT_H = 4


def _panel_grid_pos(index: int, panel_type: PanelType) -> dict[str, int]:
    """Compute Grafana grid position for a panel by its index.

    Uses a two-column layout for graphs, three-column for stats/tables.
    """
    if panel_type == "stat":
        cols = _GRID_WIDTH // _PANEL_W_THIRD
        col = index % cols
        row = index // cols
        return {"h": _STAT_H, "w": _PANEL_W_THIRD, "x": col * _PANEL_W_THIRD, "y": row * _STAT_H}

    if panel_type == "table":
        return {"h": _PANEL_H, "w": _GRID_WIDTH, "x": 0, "y": index * _PANEL_H}

    # graph — two-column
    cols = _GRID_WIDTH // _PANEL_W_HALF
    col = index % cols
    row = index // cols
    return {"h": _PANEL_H, "w": _PANEL_W_HALF, "x": col * _PANEL_W_HALF, "y": row * _PANEL_H}


def _build_panel_json(panel: DashboardPanel, panel_id: int) -> dict[str, Any]:
    """Build a Grafana panel JSON dict from a ``DashboardPanel``."""
    return {
        "id": panel_id,
        "type": panel.panel_type,
        "title": panel.title,
        "description": panel.description,
        "gridPos": _panel_grid_pos(panel_id - 1, panel.panel_type),
        "targets": [
            {
                "expr": panel.query,
                "legendFormat": panel.title,
                "refId": "A",
            },
        ],
        "fieldConfig": {
            "defaults": {
                "custom": {},
            },
        },
        "options": {},
    }


def generate_grafana_dashboard(panels: tuple[DashboardPanel, ...]) -> dict[str, Any]:
    """Generate a full Grafana dashboard JSON structure.

    Args:
        panels: Sequence of panel definitions to include.

    Returns:
        Grafana dashboard JSON as a nested dictionary, ready for import.
    """
    panel_list: list[dict[str, Any]] = [_build_panel_json(p, idx + 1) for idx, p in enumerate(panels)]

    return {
        "dashboard": {
            "id": None,
            "uid": "bernstein-otel",
            "title": "Bernstein OTel Metrics",
            "tags": ["bernstein", "opentelemetry", "otel"],
            "timezone": "browser",
            "schemaVersion": 39,
            "version": 1,
            "refresh": "30s",
            "panels": panel_list,
            "time": {"from": "now-6h", "to": "now"},
            "templating": {
                "list": [
                    {
                        "name": "datasource",
                        "type": "datasource",
                        "query": "prometheus",
                        "current": {"text": "Prometheus", "value": "Prometheus"},
                    },
                ],
            },
            "annotations": {
                "list": [
                    {
                        "builtIn": 1,
                        "datasource": "-- Grafana --",
                        "enable": True,
                        "hide": True,
                        "type": "dashboard",
                    },
                ],
            },
        },
    }


# ---------------------------------------------------------------------------
# Default panels
# ---------------------------------------------------------------------------


def get_default_panels() -> tuple[DashboardPanel, ...]:
    """Return the default set of Bernstein monitoring panels.

    Returns:
        Tuple of 10 ``DashboardPanel`` instances covering agent concurrency,
        task throughput, cost accumulation, error rate, latency percentiles,
        quality gate pass rate, token usage, active runs, spawn duration,
        and task queue depth.
    """
    return (
        DashboardPanel(
            title="Agent Concurrency",
            query="sum(bernstein_agents_active)",
            panel_type="stat",
            description="Number of concurrently active agents.",
        ),
        DashboardPanel(
            title="Task Throughput",
            query="rate(bernstein_tasks_completed_total[5m])",
            panel_type="graph",
            description="Tasks completed per second (5m rate).",
        ),
        DashboardPanel(
            title="Cost Accumulation",
            query="bernstein_cost_usd_total",
            panel_type="graph",
            description="Cumulative cost in USD.",
        ),
        DashboardPanel(
            title="Error Rate",
            query="rate(bernstein_errors_total[5m])",
            panel_type="graph",
            description="Errors per second (5m rate).",
        ),
        DashboardPanel(
            title="Latency p50 / p95 / p99",
            query=(
                "histogram_quantile(0.5, rate(bernstein_task_duration_seconds_bucket[5m])) "
                "or histogram_quantile(0.95, rate(bernstein_task_duration_seconds_bucket[5m])) "
                "or histogram_quantile(0.99, rate(bernstein_task_duration_seconds_bucket[5m]))"
            ),
            panel_type="graph",
            description="Task latency at p50, p95, and p99 percentiles.",
        ),
        DashboardPanel(
            title="Quality Gate Pass Rate",
            query="bernstein_quality_gate_pass_rate * 100",
            panel_type="stat",
            description="Percentage of tasks passing quality gates.",
        ),
        DashboardPanel(
            title="Token Usage",
            query="rate(bernstein_tokens_total[5m])",
            panel_type="graph",
            description="Token consumption rate (5m window).",
        ),
        DashboardPanel(
            title="Active Runs",
            query="bernstein_active_runs",
            panel_type="stat",
            description="Number of orchestration runs currently in progress.",
        ),
        DashboardPanel(
            title="Agent Spawn Duration",
            query="histogram_quantile(0.95, rate(bernstein_agent_spawn_duration_seconds_bucket[5m]))",
            panel_type="graph",
            description="p95 agent spawn latency.",
        ),
        DashboardPanel(
            title="Task Queue Depth",
            query="bernstein_task_queue_depth",
            panel_type="stat",
            description="Number of tasks waiting in the queue.",
        ),
    )


# ---------------------------------------------------------------------------
# Serialisation helpers
# ---------------------------------------------------------------------------


def render_config_yaml(config_dict: dict[str, Any]) -> str:
    """Render a collector config dictionary as a YAML string.

    Uses a simple recursive serialiser so that ``pyyaml`` is not required
    at runtime.

    Args:
        config_dict: Configuration dict (from :func:`generate_collector_config`).

    Returns:
        YAML-formatted string.
    """
    lines: list[str] = []
    _dict_to_yaml(config_dict, lines, indent=0)
    return "\n".join(lines) + "\n"


_YamlValue = str | int | float | bool | None | dict[str, Any] | list[Any]


def _dict_to_yaml_mapping(obj: dict[str, _YamlValue], lines: list[str], indent: int) -> None:
    """Serialise a dict to YAML lines."""
    prefix = "  " * indent
    for key, value in obj.items():
        if isinstance(value, (dict, list)):
            lines.append(f"{prefix}{key}:")
            _dict_to_yaml(value, lines, indent + 1)
        else:
            lines.append(f"{prefix}{key}: {_scalar_to_yaml(value)}")


def _dict_to_yaml_sequence(obj: list[_YamlValue], lines: list[str], indent: int) -> None:
    """Serialise a list to YAML lines."""
    prefix = "  " * indent
    for item in obj:
        if isinstance(item, dict):
            typed_item: dict[str, _YamlValue] = item
            items_iter = iter(typed_item.items())
            first_key, first_val = next(items_iter)
            lines.append(f"{prefix}- {first_key}: {_scalar_to_yaml(first_val)}")
            for k, v in items_iter:
                lines.append(f"{prefix}  {k}: {_scalar_to_yaml(v)}")
        else:
            lines.append(f"{prefix}- {_scalar_to_yaml(item)}")


def _dict_to_yaml(
    obj: dict[str, _YamlValue] | list[_YamlValue],
    lines: list[str],
    indent: int,
) -> None:
    """Recursively serialise a nested dict/list/scalar to YAML lines."""
    if isinstance(obj, dict):
        _dict_to_yaml_mapping(obj, lines, indent)
    else:
        _dict_to_yaml_sequence(obj, lines, indent)


def _scalar_to_yaml(value: Any) -> str:
    """Convert a Python scalar to its YAML string representation."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return str(value)
    if value is None:
        return "null"
    return str(value)


def export_dashboard_json(dashboard: dict[str, Any], output_path: Path) -> Path:
    """Write a Grafana dashboard dict to a JSON file.

    Args:
        dashboard: Dashboard dict (from :func:`generate_grafana_dashboard`).
        output_path: Destination file path.

    Returns:
        The *output_path* that was written to.
    """
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(dashboard, indent=2) + "\n")
    logger.info("Dashboard JSON written to %s", output_path)
    return output_path
