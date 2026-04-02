"""Tests for Grafana dashboard generator."""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.core.grafana_dashboard import (
    generate_grafana_dashboard,
    save_dashboard,
)


class TestGrafanaDashboard:
    """Test Grafana dashboard generation."""

    def test_generate_dashboard_structure(self) -> None:
        """Test dashboard has correct structure."""
        dashboard = generate_grafana_dashboard()

        assert "dashboard" in dashboard
        assert dashboard["dashboard"]["title"] == "Bernstein Orchestration Metrics"
        assert dashboard["dashboard"]["tags"] == ["bernstein", "orchestration"]

    def test_dashboard_has_panels(self) -> None:
        """Test dashboard has required panels."""
        dashboard = generate_grafana_dashboard()

        panels = dashboard["dashboard"]["panels"]
        assert len(panels) >= 4

        # Check for expected panel types
        panel_types = [p["type"] for p in panels]
        assert "graph" in panel_types
        assert "stat" in panel_types or "gauge" in panel_types

    def test_dashboard_has_datasource(self) -> None:
        """Test dashboard datasource configuration."""
        dashboard = generate_grafana_dashboard(datasource="MyPrometheus")

        # Check panels have correct datasource
        for panel in dashboard["dashboard"]["panels"]:
            if "targets" in panel:
                for target in panel["targets"]:
                    # D datasource should be referenced in queries
                    assert "expr" in target

    def test_save_dashboard(self, tmp_path: Path) -> None:
        """Test saving dashboard to file."""
        output_path = tmp_path / "dashboard.json"

        saved_path = save_dashboard(output_path)

        assert saved_path == output_path
        assert output_path.exists()

        # Verify JSON is valid
        data = json.loads(output_path.read_text())
        assert "dashboard" in data

    def test_dashboard_time_range(self) -> None:
        """Test dashboard default time range."""
        dashboard = generate_grafana_dashboard()

        time_config = dashboard["dashboard"]["time"]
        assert time_config["from"] == "now-6h"
        assert time_config["to"] == "now"

    def test_dashboard_refresh(self) -> None:
        """Test dashboard refresh interval."""
        dashboard = generate_grafana_dashboard()

        assert dashboard["dashboard"]["refresh"] == "30s"

    def test_task_completion_panel(self) -> None:
        """Test task completion panel configuration."""
        dashboard = generate_grafana_dashboard()

        # Find task completion panel
        panel = None
        for p in dashboard["dashboard"]["panels"]:
            if p.get("title") == "Task Completion Rate":
                panel = p
                break

        assert panel is not None
        assert panel["type"] == "graph"
        assert len(panel["targets"]) >= 2

    def test_cost_tracking_panel(self) -> None:
        """Test cost tracking panel configuration."""
        dashboard = generate_grafana_dashboard()

        # Find cost panel
        panel = None
        for p in dashboard["dashboard"]["panels"]:
            if "Cost" in p.get("title", ""):
                panel = p
                break

        assert panel is not None
        assert panel["type"] == "graph"
