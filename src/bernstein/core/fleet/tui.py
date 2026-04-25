"""Textual TUI for the fleet dashboard.

This file is intentionally importable on systems without a terminal: the
heavy Textual import is wrapped so unit tests can exercise the data
helpers without requiring a TTY.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.fleet.aggregator import ProjectSnapshot
from bernstein.core.fleet.audit import check_audit_tail
from bernstein.core.fleet.cost_rollup import rollup_costs

if TYPE_CHECKING:
    from bernstein.core.fleet.aggregator import FleetAggregator
    from bernstein.core.fleet.config import FleetConfig

logger = logging.getLogger(__name__)


@dataclass(slots=True, frozen=True)
class FleetRow:
    """One renderable TUI row.

    Attributes:
        name: Project name.
        state: ``online`` / ``offline`` / ``paused`` / etc.
        run_state: Plain-language run state.
        agents: Number of live agents.
        approvals: Pending approvals count.
        last_sha: Last commit SHA.
        cost_usd: 7-day cost.
        sparkline: Pre-rendered Unicode sparkline.
        chain_ok: Whether the audit chain check passed.
    """

    name: str
    state: str
    run_state: str
    agents: int
    approvals: int
    last_sha: str
    cost_usd: float
    sparkline: str
    chain_ok: bool


def build_rows(
    aggregator: FleetAggregator,
) -> tuple[list[FleetRow], float]:
    """Snapshot the aggregator into renderable rows + fleet total.

    Args:
        aggregator: Started aggregator instance.

    Returns:
        ``(rows, fleet_total_usd)``.
    """
    snapshots = {s.name: s for s in aggregator.snapshots()}
    project_paths = {p.name: p.sdd_dir for p in aggregator.projects()}
    rollup = rollup_costs(project_paths, window_days=7)
    rows: list[FleetRow] = []
    for project in aggregator.projects():
        snap = snapshots.get(project.name) or ProjectSnapshot(name=project.name)
        cost_block = rollup.per_project.get(project.name, {})
        chain = check_audit_tail(project.name, project.sdd_dir)
        cost_total = float(cost_block.get("total_usd") or snap.cost_usd or 0.0)
        spark = str(cost_block.get("sparkline") or "")
        rows.append(
            FleetRow(
                name=project.name,
                state=snap.state.value,
                run_state=snap.run_state,
                agents=snap.agents,
                approvals=snap.pending_approvals,
                last_sha=snap.last_sha,
                cost_usd=cost_total,
                sparkline=spark,
                chain_ok=chain.ok,
            )
        )
    return rows, rollup.fleet_total_usd


def format_footer(config: FleetConfig, rows: list[FleetRow], fleet_total: float) -> str:
    """Render the dashboard footer.

    Reports validation errors verbatim so misconfiguration never crashes
    the TUI.
    """
    chunks: list[str] = []
    chunks.append(f"{len(rows)} project(s) — fleet 7d: ${fleet_total:.2f}")
    if config.errors:
        for err in config.errors:
            tag = "global" if err.index < 0 else f"project[{err.index}]"
            chunks.append(f"[ERR {tag}] {err.message}")
    broken = [row for row in rows if not row.chain_ok]
    if broken:
        chunks.append("audit-chain break: " + ", ".join(r.name for r in broken))
    offline = [row.name for row in rows if row.state == "offline"]
    if offline:
        chunks.append("offline: " + ", ".join(offline))
    return " | ".join(chunks)


# ---------------------------------------------------------------------------
# Textual app — guarded import so unit tests can reach the helpers without a TTY.
# ---------------------------------------------------------------------------


def build_textual_app(
    aggregator: FleetAggregator, config: FleetConfig
) -> Any:  # pragma: no cover - Textual UI is exercised manually
    """Build and return a :class:`textual.app.App` instance.

    Imported lazily so that ``import bernstein.core.fleet`` does not pay
    for the Textual dependency on systems where it is not installed.
    """
    from typing import ClassVar

    from textual.app import App, ComposeResult
    from textual.binding import Binding
    from textual.containers import Vertical
    from textual.widgets import DataTable, Footer, Header, Static

    class FleetApp(App[None]):
        """Textual application for ``bernstein fleet``."""

        TITLE = "Bernstein fleet"
        CSS = """
        Screen { background: $background; }
        DataTable { height: 1fr; }
        #footer { color: $accent; padding: 0 1; }
        """
        BINDINGS: ClassVar[list[Binding]] = [
            Binding("q", "quit", "Quit"),
            Binding("r", "refresh", "Refresh"),
            Binding("s", "bulk_stop", "Bulk stop"),
            Binding("p", "bulk_pause", "Bulk pause"),
            Binding("u", "bulk_resume", "Bulk resume"),
            Binding("c", "bulk_cost", "Bulk cost report"),
        ]

        def compose(self) -> ComposeResult:
            yield Header()
            with Vertical():
                yield DataTable(id="fleet-table")
                yield Static(id="footer")
            yield Footer()

        def on_mount(self) -> None:
            table = self.query_one("#fleet-table", DataTable)
            table.add_columns(
                "Project",
                "State",
                "Run",
                "Agents",
                "Approvals",
                "Last SHA",
                "Cost (7d)",
                "Spark",
                "Chain",
            )
            self.set_interval(1.0, self.action_refresh)
            self.action_refresh()

        def action_refresh(self) -> None:
            rows, total = build_rows(aggregator)
            table = self.query_one("#fleet-table", DataTable)
            table.clear()
            for row in rows:
                table.add_row(
                    row.name,
                    row.state,
                    row.run_state,
                    str(row.agents),
                    str(row.approvals),
                    row.last_sha,
                    f"${row.cost_usd:.2f}",
                    row.sparkline,
                    "ok" if row.chain_ok else "BROKEN",
                )
            footer = self.query_one("#footer", Static)
            footer.update(format_footer(config, rows, total))

        async def action_bulk_stop(self) -> None:
            from bernstein.core.fleet.bulk import bulk_stop

            await bulk_stop(aggregator.projects())
            self.action_refresh()

        async def action_bulk_pause(self) -> None:
            from bernstein.core.fleet.bulk import bulk_pause

            await bulk_pause(aggregator.projects())
            self.action_refresh()

        async def action_bulk_resume(self) -> None:
            from bernstein.core.fleet.bulk import bulk_resume

            await bulk_resume(aggregator.projects())
            self.action_refresh()

        async def action_bulk_cost(self) -> None:
            from bernstein.core.fleet.bulk import bulk_cost_report

            await bulk_cost_report(aggregator.projects())
            self.action_refresh()

    return FleetApp()
