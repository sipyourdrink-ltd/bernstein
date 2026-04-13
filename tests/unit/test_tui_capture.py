"""Tests for TUI capture tooling."""

from __future__ import annotations

from pathlib import Path

from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table

from bernstein.core.observability.tui_capture import (
    CaptureConfig,
    CaptureResult,
    DemoAgent,
    DemoDashboardData,
    DemoMetrics,
    DemoTask,
    build_agent_panel,
    build_metrics_bar,
    build_status_table,
    capture_to_file,
    compose_dashboard,
    generate_demo_data,
    render_tui_snapshot,
)


class TestCaptureConfig:
    """Tests for CaptureConfig dataclass."""

    def test_default_values(self) -> None:
        """Defaults are sensible terminal dimensions."""
        cfg = CaptureConfig()
        assert cfg.width == 120
        assert cfg.height == 40
        assert cfg.theme == "dark"
        assert cfg.title is None
        assert cfg.output_format == "svg"

    def test_custom_values(self) -> None:
        """Custom values override defaults."""
        cfg = CaptureConfig(
            width=80,
            height=24,
            theme="light",
            title="My Dashboard",
            output_format="html",
        )
        assert cfg.width == 80
        assert cfg.height == 24
        assert cfg.theme == "light"
        assert cfg.title == "My Dashboard"
        assert cfg.output_format == "html"

    def test_frozen(self) -> None:
        """Config is immutable."""
        cfg = CaptureConfig()
        try:
            cfg.width = 80  # type: ignore[misc]
            raise AssertionError("Expected FrozenInstanceError")
        except AttributeError:
            pass


class TestCaptureResult:
    """Tests for CaptureResult dataclass."""

    def test_fields(self) -> None:
        """All fields are stored correctly."""
        result = CaptureResult(
            content="<svg></svg>",
            format="svg",
            width=120,
            height=40,
            timestamp=1000.0,
        )
        assert result.content == "<svg></svg>"
        assert result.format == "svg"
        assert result.width == 120
        assert result.height == 40
        assert result.timestamp == 1000.0

    def test_frozen(self) -> None:
        """Result is immutable."""
        result = CaptureResult(
            content="x",
            format="svg",
            width=120,
            height=40,
            timestamp=0.0,
        )
        try:
            result.content = "y"  # type: ignore[misc]
            raise AssertionError("Expected FrozenInstanceError")
        except AttributeError:
            pass


class TestDemoDataModels:
    """Tests for demo data frozen dataclasses."""

    def test_demo_agent_frozen(self) -> None:
        """DemoAgent is immutable."""
        agent = DemoAgent(
            name="a1",
            role="backend",
            model="gpt-4o",
            status="running",
            current_task="coding",
        )
        assert agent.name == "a1"
        try:
            agent.name = "a2"  # type: ignore[misc]
            raise AssertionError("Expected FrozenInstanceError")
        except AttributeError:
            pass

    def test_demo_task_frozen(self) -> None:
        """DemoTask is immutable."""
        task = DemoTask(
            task_id="T-1",
            title="Do stuff",
            status="open",
            assigned_to="",
            priority="high",
        )
        assert task.task_id == "T-1"
        try:
            task.status = "done"  # type: ignore[misc]
            raise AssertionError("Expected FrozenInstanceError")
        except AttributeError:
            pass

    def test_demo_metrics_frozen(self) -> None:
        """DemoMetrics is immutable."""
        m = DemoMetrics(
            total_cost_usd=1.0,
            total_tokens=100,
            quality_score=0.9,
            tasks_completed=5,
            tasks_total=10,
        )
        assert m.quality_score == 0.9
        try:
            m.total_tokens = 200  # type: ignore[misc]
            raise AssertionError("Expected FrozenInstanceError")
        except AttributeError:
            pass


class TestGenerateDemoData:
    """Tests for generate_demo_data()."""

    def test_returns_dashboard_data(self) -> None:
        """Return type is DemoDashboardData."""
        data = generate_demo_data()
        assert isinstance(data, DemoDashboardData)

    def test_agent_count(self) -> None:
        """Generates exactly 5 agents."""
        data = generate_demo_data()
        assert len(data.agents) == 5

    def test_task_count(self) -> None:
        """Generates exactly 12 tasks."""
        data = generate_demo_data()
        assert len(data.tasks) == 12

    def test_agents_have_distinct_roles(self) -> None:
        """Each agent has a unique role."""
        data = generate_demo_data()
        roles = {a.role for a in data.agents}
        assert len(roles) == len(data.agents)

    def test_agents_have_distinct_models(self) -> None:
        """At least three distinct models are used across agents."""
        data = generate_demo_data()
        models = {a.model for a in data.agents}
        assert len(models) >= 3

    def test_tasks_cover_all_statuses(self) -> None:
        """Tasks include all possible statuses."""
        data = generate_demo_data()
        statuses = {t.status for t in data.tasks}
        assert statuses == {"open", "in_progress", "done", "failed", "blocked"}

    def test_metrics_consistency(self) -> None:
        """Completed count matches done-status tasks."""
        data = generate_demo_data()
        done_count = sum(1 for t in data.tasks if t.status == "done")
        assert data.metrics.tasks_completed == done_count

    def test_metrics_total_matches_task_count(self) -> None:
        """Total tasks metric matches actual task count."""
        data = generate_demo_data()
        assert data.metrics.tasks_total == len(data.tasks)


class TestBuildStatusTable:
    """Tests for build_status_table()."""

    def test_returns_table(self) -> None:
        """Returns a Rich Table."""
        data = generate_demo_data()
        table = build_status_table(data)
        assert isinstance(table, Table)

    def test_table_has_columns(self) -> None:
        """Table has the expected 5 columns."""
        data = generate_demo_data()
        table = build_status_table(data)
        assert len(table.columns) == 5

    def test_table_has_rows(self) -> None:
        """Table has one row per task."""
        data = generate_demo_data()
        table = build_status_table(data)
        assert table.row_count == len(data.tasks)


class TestBuildAgentPanel:
    """Tests for build_agent_panel()."""

    def test_returns_panel(self) -> None:
        """Returns a Rich Panel."""
        data = generate_demo_data()
        panel = build_agent_panel(data)
        assert isinstance(panel, Panel)

    def test_panel_title(self) -> None:
        """Panel title contains 'Agents'."""
        data = generate_demo_data()
        panel = build_agent_panel(data)
        assert panel.title is not None
        assert "Agents" in str(panel.title)


class TestBuildMetricsBar:
    """Tests for build_metrics_bar()."""

    def test_returns_panel(self) -> None:
        """Returns a Rich Panel."""
        data = generate_demo_data()
        panel = build_metrics_bar(data)
        assert isinstance(panel, Panel)

    def test_panel_title(self) -> None:
        """Panel title contains 'Metrics'."""
        data = generate_demo_data()
        panel = build_metrics_bar(data)
        assert panel.title is not None
        assert "Metrics" in str(panel.title)


class TestComposeDashboard:
    """Tests for compose_dashboard()."""

    def test_returns_layout(self) -> None:
        """Returns a Rich Layout."""
        data = generate_demo_data()
        config = CaptureConfig()
        layout = compose_dashboard(data, config)
        assert isinstance(layout, Layout)

    def test_layout_has_sections(self) -> None:
        """Layout has metrics, agents, and tasks sections."""
        data = generate_demo_data()
        config = CaptureConfig()
        layout = compose_dashboard(data, config)
        # Layout children are named sections.
        names = {child.name for child in layout.children}
        assert "metrics" in names
        assert "agents" in names
        assert "tasks" in names


class TestRenderTuiSnapshot:
    """Tests for render_tui_snapshot()."""

    def test_svg_output(self) -> None:
        """SVG output starts with <svg."""
        data = generate_demo_data()
        config = CaptureConfig(output_format="svg", title="Test")
        result = render_tui_snapshot(data, config)
        assert result.format == "svg"
        assert "<svg" in result.content

    def test_html_output(self) -> None:
        """HTML output contains expected tags."""
        data = generate_demo_data()
        config = CaptureConfig(output_format="html")
        result = render_tui_snapshot(data, config)
        assert result.format == "html"
        assert "<html" in result.content or "<pre" in result.content

    def test_png_raises(self) -> None:
        """PNG format raises ValueError."""
        data = generate_demo_data()
        config = CaptureConfig(output_format="png")
        try:
            render_tui_snapshot(data, config)
            raise AssertionError("Expected ValueError")
        except ValueError as exc:
            assert "PNG" in str(exc)

    def test_result_dimensions(self) -> None:
        """Result stores the dimensions from config."""
        data = generate_demo_data()
        config = CaptureConfig(width=80, height=24)
        result = render_tui_snapshot(data, config)
        assert result.width == 80
        assert result.height == 24

    def test_result_has_timestamp(self) -> None:
        """Result timestamp is a recent float."""
        data = generate_demo_data()
        config = CaptureConfig()
        result = render_tui_snapshot(data, config)
        assert result.timestamp > 0
        # Should be within the last few seconds.
        assert result.timestamp <= __import__("time").time()

    def test_svg_contains_title(self) -> None:
        """SVG output includes the configured title text."""
        data = generate_demo_data()
        config = CaptureConfig(output_format="svg", title="MyProject")
        result = render_tui_snapshot(data, config)
        assert "MyProject" in result.content

    def test_light_theme(self) -> None:
        """Light theme produces valid SVG output."""
        data = generate_demo_data()
        config = CaptureConfig(theme="light")
        result = render_tui_snapshot(data, config)
        assert "<svg" in result.content


class TestCaptureToFile:
    """Tests for capture_to_file()."""

    def test_writes_svg_file(self, tmp_path: Path) -> None:
        """Creates an SVG file at the specified path."""
        output = tmp_path / "dashboard.svg"
        result = capture_to_file(output)
        assert output.exists()
        assert output.read_text(encoding="utf-8") == result.content
        assert "<svg" in result.content

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        """Creates parent directories if they don't exist."""
        output = tmp_path / "nested" / "dir" / "dashboard.svg"
        result = capture_to_file(output)
        assert output.exists()
        assert "<svg" in result.content

    def test_custom_config(self, tmp_path: Path) -> None:
        """Respects custom config when writing."""
        output = tmp_path / "dashboard.html"
        config = CaptureConfig(output_format="html", width=80, height=24)
        result = capture_to_file(output, config)
        assert output.exists()
        assert result.format == "html"
        assert result.width == 80

    def test_default_config_when_none(self, tmp_path: Path) -> None:
        """Uses default config when None is passed."""
        output = tmp_path / "dashboard.svg"
        result = capture_to_file(output, None)
        assert result.width == 120
        assert result.height == 40
