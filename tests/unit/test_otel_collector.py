"""Tests for OpenTelemetry Collector config generator and Grafana dashboards."""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.core.observability.otel_collector import (
    CollectorPipeline,
    DashboardPanel,
    OTelConfig,
    export_dashboard_json,
    generate_collector_config,
    generate_grafana_dashboard,
    get_default_panels,
    render_config_yaml,
)

# ---------------------------------------------------------------------------
# OTelConfig dataclass
# ---------------------------------------------------------------------------


def test_otel_config_defaults() -> None:
    """OTelConfig should have sensible defaults."""
    cfg = OTelConfig()
    assert cfg.endpoint == "http://localhost:4317"
    assert cfg.protocol == "grpc"
    assert cfg.service_name == "bernstein"
    assert cfg.resource_attributes == {}
    assert cfg.export_interval_s == 30


def test_otel_config_custom_values() -> None:
    """OTelConfig should accept custom values."""
    cfg = OTelConfig(
        endpoint="http://collector:4318",
        protocol="http",
        service_name="bernstein-test",
        resource_attributes={"env": "staging"},
        export_interval_s=60,
    )
    assert cfg.endpoint == "http://collector:4318"
    assert cfg.protocol == "http"
    assert cfg.service_name == "bernstein-test"
    assert cfg.resource_attributes == {"env": "staging"}
    assert cfg.export_interval_s == 60


def test_otel_config_frozen() -> None:
    """OTelConfig should be immutable."""
    cfg = OTelConfig()
    try:
        cfg.endpoint = "http://other:4317"  # type: ignore[misc]
        raise AssertionError("Expected FrozenInstanceError")
    except AttributeError:
        pass


# ---------------------------------------------------------------------------
# CollectorPipeline dataclass
# ---------------------------------------------------------------------------


def test_collector_pipeline_defaults() -> None:
    """CollectorPipeline should have sensible defaults."""
    pipeline = CollectorPipeline()
    assert pipeline.receivers == ("otlp",)
    assert pipeline.processors == ("batch",)
    assert pipeline.exporters == ("prometheus",)


def test_collector_pipeline_custom() -> None:
    """CollectorPipeline should accept custom component lists."""
    pipeline = CollectorPipeline(
        receivers=("otlp", "prometheus"),
        processors=("batch", "filter"),
        exporters=("otlp", "logging"),
    )
    assert len(pipeline.receivers) == 2
    assert "filter" in pipeline.processors


def test_collector_pipeline_frozen() -> None:
    """CollectorPipeline should be immutable."""
    pipeline = CollectorPipeline()
    try:
        pipeline.receivers = ("jaeger",)  # type: ignore[misc]
        raise AssertionError("Expected FrozenInstanceError")
    except AttributeError:
        pass


# ---------------------------------------------------------------------------
# DashboardPanel dataclass
# ---------------------------------------------------------------------------


def test_dashboard_panel_required_fields() -> None:
    """DashboardPanel should require title and query."""
    panel = DashboardPanel(title="Test", query="up")
    assert panel.title == "Test"
    assert panel.query == "up"
    assert panel.panel_type == "graph"
    assert panel.description == ""


def test_dashboard_panel_all_types() -> None:
    """DashboardPanel should support all panel types."""
    for ptype in ("graph", "stat", "table"):
        panel = DashboardPanel(title="T", query="q", panel_type=ptype)  # type: ignore[arg-type]
        assert panel.panel_type == ptype


def test_dashboard_panel_frozen() -> None:
    """DashboardPanel should be immutable."""
    panel = DashboardPanel(title="T", query="q")
    try:
        panel.title = "Other"  # type: ignore[misc]
        raise AssertionError("Expected FrozenInstanceError")
    except AttributeError:
        pass


# ---------------------------------------------------------------------------
# generate_collector_config
# ---------------------------------------------------------------------------


def test_generate_collector_config_grpc() -> None:
    """gRPC config should use port 4317."""
    cfg = OTelConfig(protocol="grpc")
    result = generate_collector_config(cfg)
    protocols = result["receivers"]["otlp"]["protocols"]
    assert "grpc" in protocols
    assert "http" not in protocols


def test_generate_collector_config_http() -> None:
    """HTTP config should use port 4318."""
    cfg = OTelConfig(protocol="http")
    result = generate_collector_config(cfg)
    protocols = result["receivers"]["otlp"]["protocols"]
    assert "http" in protocols
    assert "grpc" not in protocols


def test_generate_collector_config_service_pipelines() -> None:
    """Generated config must include metrics and traces pipelines."""
    result = generate_collector_config(OTelConfig())
    pipelines = result["service"]["pipelines"]
    assert "metrics" in pipelines
    assert "traces" in pipelines
    assert "otlp" in pipelines["metrics"]["receivers"]
    assert "batch" in pipelines["metrics"]["processors"]


def test_generate_collector_config_resource_attributes() -> None:
    """Custom resource attributes should appear in the resource processor."""
    cfg = OTelConfig(resource_attributes={"deployment.environment": "prod", "team": "platform"})
    result = generate_collector_config(cfg)
    attrs = result["processors"]["resource"]["attributes"]
    keys = [a["key"] for a in attrs]
    assert "deployment.environment" in keys
    assert "team" in keys
    assert "service.name" in keys  # always present


def test_generate_collector_config_endpoint_propagation() -> None:
    """Custom endpoint should propagate to the otlp exporter."""
    cfg = OTelConfig(endpoint="http://my-collector:4317")
    result = generate_collector_config(cfg)
    assert result["exporters"]["otlp"]["endpoint"] == "http://my-collector:4317"


def test_generate_collector_config_has_extensions() -> None:
    """Generated config should include health_check and zpages extensions."""
    result = generate_collector_config(OTelConfig())
    assert "health_check" in result["extensions"]
    assert "zpages" in result["extensions"]


def test_generate_collector_config_has_prometheus_exporter() -> None:
    """Generated config should include a prometheus exporter with bernstein namespace."""
    result = generate_collector_config(OTelConfig())
    prom = result["exporters"]["prometheus"]
    assert prom["namespace"] == "bernstein"


def test_generate_collector_config_memory_limiter() -> None:
    """Generated config should include a memory_limiter processor."""
    result = generate_collector_config(OTelConfig())
    assert "memory_limiter" in result["processors"]
    limiter = result["processors"]["memory_limiter"]
    assert limiter["limit_mib"] > 0


def test_generate_collector_config_meta_export_interval() -> None:
    """_meta section should capture the export interval."""
    cfg = OTelConfig(export_interval_s=15)
    result = generate_collector_config(cfg)
    assert result["_meta"]["export_interval_s"] == 15


# ---------------------------------------------------------------------------
# generate_grafana_dashboard
# ---------------------------------------------------------------------------


def test_generate_grafana_dashboard_structure() -> None:
    """Dashboard JSON should have required top-level keys."""
    panels = (DashboardPanel(title="X", query="up"),)
    dash = generate_grafana_dashboard(panels)
    db = dash["dashboard"]
    assert db["uid"] == "bernstein-otel"
    assert db["schemaVersion"] == 39
    assert isinstance(db["panels"], list)
    assert len(db["panels"]) == 1


def test_generate_grafana_dashboard_panel_ids() -> None:
    """Panel IDs should be sequential starting at 1."""
    panels = tuple(DashboardPanel(title=f"P{i}", query="up") for i in range(5))
    dash = generate_grafana_dashboard(panels)
    ids = [p["id"] for p in dash["dashboard"]["panels"]]
    assert ids == [1, 2, 3, 4, 5]


def test_generate_grafana_dashboard_templating() -> None:
    """Dashboard should include a datasource template variable."""
    panels = (DashboardPanel(title="X", query="up"),)
    dash = generate_grafana_dashboard(panels)
    templates = dash["dashboard"]["templating"]["list"]
    assert any(t["name"] == "datasource" for t in templates)


def test_generate_grafana_dashboard_empty_panels() -> None:
    """Dashboard should accept an empty panel tuple."""
    dash = generate_grafana_dashboard(())
    assert dash["dashboard"]["panels"] == []


# ---------------------------------------------------------------------------
# get_default_panels
# ---------------------------------------------------------------------------


def test_get_default_panels_count() -> None:
    """Default panel set should contain 8-10 panels."""
    panels = get_default_panels()
    count = len(panels)
    assert count >= 8
    assert count <= 10


def test_get_default_panels_are_dashboard_panels() -> None:
    """All default panels should be DashboardPanel instances."""
    for panel in get_default_panels():
        assert isinstance(panel, DashboardPanel)


def test_get_default_panels_titles() -> None:
    """Default panels should cover key metrics areas."""
    titles = {p.title for p in get_default_panels()}
    assert "Agent Concurrency" in titles
    assert "Task Throughput" in titles
    assert "Cost Accumulation" in titles
    assert "Error Rate" in titles
    assert "Token Usage" in titles
    assert "Quality Gate Pass Rate" in titles
    assert "Active Runs" in titles


def test_get_default_panels_latency_percentiles() -> None:
    """Latency panel should reference p50, p95, and p99."""
    panels = get_default_panels()
    latency = [p for p in panels if "Latency" in p.title]
    assert len(latency) == 1
    assert "0.5" in latency[0].query
    assert "0.95" in latency[0].query
    assert "0.99" in latency[0].query


def test_get_default_panels_all_have_queries() -> None:
    """Every default panel should have a non-empty query."""
    for panel in get_default_panels():
        assert panel.query, f"Panel {panel.title!r} has empty query"


def test_get_default_panels_all_have_descriptions() -> None:
    """Every default panel should have a non-empty description."""
    for panel in get_default_panels():
        assert panel.description, f"Panel {panel.title!r} has empty description"


# ---------------------------------------------------------------------------
# render_config_yaml
# ---------------------------------------------------------------------------


def test_render_config_yaml_valid_yaml() -> None:
    """Rendered YAML should be parseable (basic structural check)."""
    cfg = generate_collector_config(OTelConfig())
    yaml_str = render_config_yaml(cfg)
    assert isinstance(yaml_str, str)
    assert yaml_str.endswith("\n")
    # Key sections must be present
    assert "receivers:" in yaml_str
    assert "processors:" in yaml_str
    assert "exporters:" in yaml_str
    assert "service:" in yaml_str


def test_render_config_yaml_contains_endpoint() -> None:
    """Rendered YAML should contain the configured endpoint."""
    cfg = OTelConfig(endpoint="http://custom:4317")
    yaml_str = render_config_yaml(generate_collector_config(cfg))
    assert "http://custom:4317" in yaml_str


def test_render_config_yaml_booleans() -> None:
    """Booleans should render as YAML true/false, not Python True/False."""
    cfg = generate_collector_config(OTelConfig())
    yaml_str = render_config_yaml(cfg)
    assert "true" in yaml_str.lower()
    assert "True" not in yaml_str
    assert "False" not in yaml_str


def test_render_config_yaml_list_items() -> None:
    """List items (resource attributes) should render with dash prefix."""
    cfg = OTelConfig(resource_attributes={"env": "prod"})
    yaml_str = render_config_yaml(generate_collector_config(cfg))
    assert "- key:" in yaml_str


# ---------------------------------------------------------------------------
# export_dashboard_json
# ---------------------------------------------------------------------------


def test_export_dashboard_json_writes_file(tmp_path: Path) -> None:
    """export_dashboard_json should write valid JSON to disk."""
    panels = get_default_panels()
    dash = generate_grafana_dashboard(panels)
    out = tmp_path / "dashboard.json"
    result = export_dashboard_json(dash, out)
    assert result == out
    assert out.exists()
    data = json.loads(out.read_text())
    assert "dashboard" in data


def test_export_dashboard_json_creates_parents(tmp_path: Path) -> None:
    """export_dashboard_json should create parent directories."""
    out = tmp_path / "nested" / "dir" / "dashboard.json"
    dash = generate_grafana_dashboard(())
    export_dashboard_json(dash, out)
    assert out.exists()


def test_export_dashboard_json_roundtrip(tmp_path: Path) -> None:
    """Dashboard JSON should survive a write-read roundtrip."""
    panels = get_default_panels()
    dash = generate_grafana_dashboard(panels)
    out = tmp_path / "roundtrip.json"
    export_dashboard_json(dash, out)
    loaded = json.loads(out.read_text())
    assert loaded["dashboard"]["uid"] == "bernstein-otel"
    assert len(loaded["dashboard"]["panels"]) == len(panels)
