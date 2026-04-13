"""TUI capture tooling for generating realistic README screenshots.

Renders a Rich-based dashboard snapshot to SVG (or other formats) for use
in documentation.  The dashboard displays realistic demo data: agents with
various roles/models, tasks in different states, and cost/token/quality
metrics.

Example::

    from pathlib import Path
    from bernstein.core.observability.tui_capture import (
        capture_to_file,
        CaptureConfig,
    )

    capture_to_file(Path("docs/dashboard.svg"), CaptureConfig(title="Bernstein"))
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Configuration and result data models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CaptureConfig:
    """Configuration for a TUI capture session.

    Attributes:
        width: Terminal width in columns.
        height: Terminal height in rows.
        theme: Colour theme -- ``"dark"`` or ``"light"``.
        title: Optional title rendered in the SVG header.
        output_format: Target format for the capture.
    """

    width: int = 120
    height: int = 40
    theme: Literal["dark", "light"] = "dark"
    title: str | None = None
    output_format: Literal["svg", "png", "html"] = "svg"


@dataclass(frozen=True)
class CaptureResult:
    """Result of a TUI capture operation.

    Attributes:
        content: The rendered content (SVG markup, HTML, etc.).
        format: The output format that was used.
        width: Terminal width used during capture.
        height: Terminal height used during capture.
        timestamp: Unix timestamp when the capture was produced.
    """

    content: str
    format: str
    width: int
    height: int
    timestamp: float


# ---------------------------------------------------------------------------
# Demo-data models (frozen dataclasses, no dict soup)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DemoAgent:
    """A single agent shown in the demo dashboard.

    Attributes:
        name: Human-readable agent identifier.
        role: The agent's assigned role (backend, qa, etc.).
        model: LLM model used by this agent.
        status: Current lifecycle status.
        current_task: Short description of what it is doing.
    """

    name: str
    role: str
    model: str
    status: Literal["running", "idle", "done", "failed"]
    current_task: str


@dataclass(frozen=True)
class DemoTask:
    """A single task shown in the demo dashboard.

    Attributes:
        task_id: Short identifier.
        title: Human-readable summary.
        status: Current state.
        assigned_to: Agent name or empty string if unassigned.
        priority: Priority level.
    """

    task_id: str
    title: str
    status: Literal["open", "in_progress", "done", "failed", "blocked"]
    assigned_to: str
    priority: Literal["high", "medium", "low"]


@dataclass(frozen=True)
class DemoMetrics:
    """Aggregate metrics shown in the demo dashboard.

    Attributes:
        total_cost_usd: Cumulative cost.
        total_tokens: Cumulative token usage.
        quality_score: Quality gate pass rate (0.0-1.0).
        tasks_completed: Number of completed tasks.
        tasks_total: Total number of tasks.
    """

    total_cost_usd: float
    total_tokens: int
    quality_score: float
    tasks_completed: int
    tasks_total: int


@dataclass(frozen=True)
class DemoDashboardData:
    """Complete set of demo data for a dashboard capture.

    Attributes:
        agents: Tuple of demo agents.
        tasks: Tuple of demo tasks.
        metrics: Aggregate metrics.
    """

    agents: tuple[DemoAgent, ...]
    tasks: tuple[DemoTask, ...]
    metrics: DemoMetrics


# ---------------------------------------------------------------------------
# Demo data generation
# ---------------------------------------------------------------------------


def generate_demo_data() -> DemoDashboardData:
    """Create realistic demo data for a dashboard capture.

    Returns:
        A ``DemoDashboardData`` with 5 agents, 12 tasks, and aggregate
        metrics.
    """
    agents = (
        DemoAgent(
            name="agent-01",
            role="backend",
            model="claude-sonnet-4-20250514",
            status="running",
            current_task="Implement REST endpoint for /tasks",
        ),
        DemoAgent(
            name="agent-02",
            role="frontend",
            model="gpt-4o",
            status="running",
            current_task="Build React dashboard component",
        ),
        DemoAgent(
            name="agent-03",
            role="qa",
            model="claude-haiku-4-20250414",
            status="idle",
            current_task="",
        ),
        DemoAgent(
            name="agent-04",
            role="devops",
            model="gemini-2.5-pro",
            status="done",
            current_task="",
        ),
        DemoAgent(
            name="agent-05",
            role="security",
            model="claude-sonnet-4-20250514",
            status="failed",
            current_task="",
        ),
    )

    tasks = (
        DemoTask("TSK-001", "Set up project scaffolding", "done", "agent-01", "high"),
        DemoTask("TSK-002", "Implement task server API", "done", "agent-01", "high"),
        DemoTask("TSK-003", "Add authentication middleware", "done", "agent-04", "high"),
        DemoTask("TSK-004", "Build agent spawner", "done", "agent-01", "medium"),
        DemoTask("TSK-005", "Create React dashboard", "in_progress", "agent-02", "high"),
        DemoTask("TSK-006", "Write integration tests", "in_progress", "agent-03", "medium"),
        DemoTask("TSK-007", "Add CI/CD pipeline", "done", "agent-04", "medium"),
        DemoTask("TSK-008", "Security audit scan", "failed", "agent-05", "high"),
        DemoTask("TSK-009", "Add WebSocket support", "open", "", "medium"),
        DemoTask("TSK-010", "Write API documentation", "open", "", "low"),
        DemoTask("TSK-011", "Performance benchmarks", "blocked", "", "low"),
        DemoTask("TSK-012", "Deploy to staging", "open", "", "medium"),
    )

    metrics = DemoMetrics(
        total_cost_usd=4.37,
        total_tokens=1_284_500,
        quality_score=0.87,
        tasks_completed=5,
        tasks_total=12,
    )

    return DemoDashboardData(agents=agents, tasks=tasks, metrics=metrics)


# ---------------------------------------------------------------------------
# Rich rendering helpers
# ---------------------------------------------------------------------------

# Status colour mapping for consistent styling.
_STATUS_STYLES: dict[str, str] = {
    "running": "bold green",
    "idle": "yellow",
    "done": "dim green",
    "failed": "bold red",
    "open": "white",
    "in_progress": "bold cyan",
    "blocked": "bold magenta",
}

_PRIORITY_STYLES: dict[str, str] = {
    "high": "bold red",
    "medium": "yellow",
    "low": "dim white",
}


def build_status_table(data: DemoDashboardData) -> Table:
    """Build a Rich Table showing task status columns.

    Args:
        data: Dashboard demo data containing tasks to display.

    Returns:
        A ``rich.table.Table`` with task ID, title, status, assignee,
        and priority columns.
    """
    table = Table(
        title="Tasks",
        expand=True,
        show_lines=False,
        border_style="bright_blue",
    )
    table.add_column("ID", style="bold", no_wrap=True, width=8)
    table.add_column("Title", ratio=3)
    table.add_column("Status", no_wrap=True, width=13)
    table.add_column("Agent", no_wrap=True, width=10)
    table.add_column("Priority", no_wrap=True, width=8)

    for task in data.tasks:
        status_style = _STATUS_STYLES.get(task.status, "white")
        priority_style = _PRIORITY_STYLES.get(task.priority, "white")
        table.add_row(
            task.task_id,
            task.title,
            Text(task.status, style=status_style),
            task.assigned_to or "-",
            Text(task.priority, style=priority_style),
        )

    return table


def build_agent_panel(data: DemoDashboardData) -> Panel:
    """Build a Rich Panel showing agent status.

    Args:
        data: Dashboard demo data containing agents to display.

    Returns:
        A ``rich.panel.Panel`` with a table of agents, their roles,
        models, and current statuses.
    """
    table = Table(expand=True, show_header=True, show_lines=False)
    table.add_column("Agent", style="bold", no_wrap=True)
    table.add_column("Role", no_wrap=True)
    table.add_column("Model", no_wrap=True)
    table.add_column("Status", no_wrap=True)
    table.add_column("Current Task")

    for agent in data.agents:
        status_style = _STATUS_STYLES.get(agent.status, "white")
        table.add_row(
            agent.name,
            agent.role,
            agent.model,
            Text(agent.status, style=status_style),
            agent.current_task or "-",
        )

    return Panel(
        table,
        title="[bold]Agents[/bold]",
        border_style="bright_blue",
        expand=True,
    )


def build_metrics_bar(data: DemoDashboardData) -> Panel:
    """Build a Rich Panel with progress bars for cost/token/quality metrics.

    Args:
        data: Dashboard demo data containing aggregate metrics.

    Returns:
        A ``rich.panel.Panel`` containing textual progress bars and
        metric labels.
    """
    m = data.metrics

    # Build text-based progress visualisation.
    progress_width = 30

    # Task completion bar
    task_ratio = m.tasks_completed / m.tasks_total if m.tasks_total > 0 else 0.0
    task_filled = int(task_ratio * progress_width)
    task_bar = (
        f"[bold]Tasks:[/bold]  [green]{'=' * task_filled}[/green]"
        f"[dim]{'.' * (progress_width - task_filled)}[/dim]"
        f"  {m.tasks_completed}/{m.tasks_total}"
    )

    # Quality bar
    quality_filled = int(m.quality_score * progress_width)
    quality_bar = (
        f"[bold]Quality:[/bold] [cyan]{'=' * quality_filled}[/cyan]"
        f"[dim]{'.' * (progress_width - quality_filled)}[/dim]"
        f"  {m.quality_score:.0%}"
    )

    # Cost and token summaries
    cost_line = f"[bold]Cost:[/bold]   ${m.total_cost_usd:.2f} USD"
    token_line = f"[bold]Tokens:[/bold]  {m.total_tokens:,}"

    content = Text.from_markup(f"{task_bar}\n{quality_bar}\n{cost_line}\n{token_line}")

    return Panel(
        content,
        title="[bold]Metrics[/bold]",
        border_style="bright_blue",
        expand=True,
    )


def compose_dashboard(
    data: DemoDashboardData,
    _config: CaptureConfig,
) -> Layout:
    """Compose all panels into a single Rich Layout.

    The layout has three sections stacked vertically:

    1. Metrics bar (compact, top).
    2. Agent panel (middle).
    3. Task status table (bottom, largest).

    Args:
        data: Dashboard demo data.
        _config: Capture configuration (part of interface).

    Returns:
        A ``rich.layout.Layout`` ready to be rendered by a Console.
    """
    layout = Layout()

    layout.split_column(
        Layout(name="metrics", size=8),
        Layout(name="agents", size=11),
        Layout(name="tasks"),
    )

    layout["metrics"].update(build_metrics_bar(data))
    layout["agents"].update(build_agent_panel(data))
    layout["tasks"].update(
        Panel(
            build_status_table(data),
            border_style="bright_blue",
            expand=True,
        )
    )

    return layout


# ---------------------------------------------------------------------------
# Rendering / capture
# ---------------------------------------------------------------------------


def render_tui_snapshot(
    data: DemoDashboardData,
    config: CaptureConfig,
) -> CaptureResult:
    """Render a Rich-based dashboard as an exportable string.

    Uses ``rich.console.Console(record=True)`` to capture the rendered
    output and export it in the requested format.

    Args:
        data: Dashboard demo data to render.
        config: Capture configuration.

    Returns:
        A ``CaptureResult`` containing the exported content.

    Raises:
        ValueError: If ``config.output_format`` is ``"png"`` (not
            supported by Rich's built-in export).
    """
    if config.output_format == "png":
        msg = "PNG export is not supported by Rich's built-in export. Use SVG and convert externally."
        raise ValueError(msg)

    console = Console(
        record=True,
        width=config.width,
        height=config.height,
        force_terminal=True,
        color_system="truecolor" if config.theme == "dark" else "standard",
    )

    dashboard = compose_dashboard(data, config)
    console.print(dashboard)

    if config.output_format == "html":
        content = console.export_html()
    else:
        title = config.title or "Bernstein Dashboard"
        content = console.export_svg(title=title)

    return CaptureResult(
        content=content,
        format=config.output_format,
        width=config.width,
        height=config.height,
        timestamp=time.time(),
    )


def capture_to_file(
    output_path: Path,
    config: CaptureConfig | None = None,
) -> CaptureResult:
    """Generate a dashboard snapshot and write it to a file.

    Combines ``generate_demo_data`` + ``render_tui_snapshot`` and persists
    the result to *output_path*.

    Args:
        output_path: Destination file path.
        config: Optional capture configuration; defaults are used when
            ``None``.

    Returns:
        The ``CaptureResult`` that was written.
    """
    if config is None:
        config = CaptureConfig()

    data = generate_demo_data()
    result = render_tui_snapshot(data, config)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(result.content, encoding="utf-8")

    return result
