"""TUI overlay showing graceful drain / shutdown progress.

Pushed onto the Textual app stack when the user presses ``q`` in the
dashboard.  Displays real-time phase progress, per-agent status, and a
final summary report once the drain completes.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import TYPE_CHECKING, Any, ClassVar

from textual.binding import Binding, BindingType
from textual.containers import Center, Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import ProgressBar, Static

from bernstein.core.drain import (
    AgentDrainStatus,
    DrainConfig,
    DrainCoordinator,
    DrainPhase,
    DrainReport,
)

if TYPE_CHECKING:
    from textual.app import ComposeResult
    from textual.events import Key

# Total number of drain phases (freeze, signal, wait, commit, merge, cleanup).
_TOTAL_PHASES = 6

SERVER_URL = "http://127.0.0.1:8052"


class DrainScreen(Screen[DrainReport | None]):
    """Full-screen overlay showing graceful drain progress."""

    DEFAULT_CSS = """
    DrainScreen {
        align: center middle;
    }

    #drain-container {
        width: 60;
        max-height: 80%;
        border: double $accent;
        padding: 1 2;
        background: $surface;
    }

    #drain-title {
        text-align: center;
        text-style: bold;
        margin-bottom: 1;
    }

    #phase-label {
        text-style: bold;
        margin-bottom: 1;
    }

    #drain-progress {
        margin-bottom: 1;
    }

    #agent-list {
        height: auto;
        max-height: 15;
        overflow-y: auto;
    }

    #report-container {
        width: 60;
        border: double $success;
        padding: 1 2;
        background: $surface;
    }

    #report-title {
        text-align: center;
        text-style: bold;
        margin-bottom: 1;
    }

    #report-body {
        height: auto;
    }

    #report-hint {
        text-align: center;
        margin-top: 1;
        text-style: dim;
    }
    """

    BINDINGS: ClassVar[list[BindingType]] = [
        Binding("escape", "cancel_drain", "Cancel", show=True),
        Binding("ctrl+c", "force_quit", "Force Quit", show=True),
    ]

    def __init__(
        self,
        workdir: Path | None = None,
        server_url: str = SERVER_URL,
        config: DrainConfig | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(**kwargs)
        self._workdir = workdir or Path.cwd()
        self._server_url = server_url
        self._config = config or DrainConfig()
        self._coordinator: DrainCoordinator | None = None
        self._drain_task: asyncio.Task[None] | None = None
        self._report: DrainReport | None = None
        self._report_shown = False

    # ------------------------------------------------------------------
    # Layout
    # ------------------------------------------------------------------

    def compose(self) -> ComposeResult:
        """Build the progress overlay layout."""
        with Center():
            with Vertical(id="drain-container"):
                yield Static("Shutting Down", id="drain-title")
                yield Static("Phase: Initialising...", id="phase-label")
                yield ProgressBar(
                    total=_TOTAL_PHASES,
                    show_eta=False,
                    id="drain-progress",
                )
                yield VerticalScroll(
                    Static("", id="agent-list-content"),
                    id="agent-list",
                )
            # Report container is hidden initially; shown when drain finishes.
            with Vertical(id="report-container"):
                yield Static("Run Complete", id="report-title")
                yield Static("", id="report-body")
                yield Static("Press any key to exit", id="report-hint")

    def on_mount(self) -> None:
        """Start the drain as a background asyncio task."""
        # Hide the report container until the drain finishes.
        report_container = self.query_one("#report-container", Vertical)
        report_container.display = False

        self._drain_task = asyncio.ensure_future(self._run_drain())

    # ------------------------------------------------------------------
    # Drain execution
    # ------------------------------------------------------------------

    async def _run_drain(self) -> None:
        """Create the coordinator and run all drain phases."""
        self._coordinator = DrainCoordinator(
            workdir=self._workdir,
            server_url=self._server_url,
            config=self._config,
        )

        try:
            report = await self._coordinator.run(
                callback=self._on_drain_update,
            )
        except Exception:
            # If the drain fails catastrophically, dismiss with no report.
            self.dismiss(None)
            return

        self._report = report
        self._show_report(report)

    def _on_drain_update(
        self,
        phase: DrainPhase,
        agents: list[AgentDrainStatus],
    ) -> None:
        """Called by DrainCoordinator on each state change.

        Because the coordinator runs in the same asyncio loop as Textual,
        widget updates are safe without ``call_from_thread``.
        """
        # Phase label.
        phase_text = f"Phase: {phase.detail or phase.name}  [{phase.number}/{_TOTAL_PHASES}]"
        self.query_one("#phase-label", Static).update(phase_text)

        # Progress bar — advance to the number of completed phases.
        bar = self.query_one("#drain-progress", ProgressBar)
        completed = phase.number - 1 if phase.status == "running" else phase.number
        bar.update(progress=float(completed))

        # Agent list.
        lines: list[str] = []
        for agent in agents:
            match agent.status:
                case "exited":
                    files_note = f" ({agent.committed_files} files committed)" if agent.committed_files else ""
                    lines.append(f"[green]\u2713 {agent.session_id} exited{files_note}[/green]")
                case "killed":
                    lines.append(f"[red]\u2717 {agent.session_id} killed[/red]")
                case _:
                    lines.append(f"[yellow]\u25c9 {agent.session_id} {agent.status}...[/yellow]")

        self.query_one("#agent-list-content", Static).update("\n".join(lines) if lines else "")

    # ------------------------------------------------------------------
    # Report view
    # ------------------------------------------------------------------

    def _show_report(self, report: DrainReport) -> None:
        """Replace the progress view with a summary report."""
        self._report_shown = True

        # Hide progress, show report.
        self.query_one("#drain-container", Vertical).display = False
        report_container = self.query_one("#report-container", Vertical)
        report_container.display = True

        # Build report text.
        duration = _format_duration(report.total_duration_s)
        lines: list[str] = []

        # Task stats.
        lines.append(
            f"Tasks: {report.tasks_done} done  \u00b7  "
            f"{report.tasks_partial} partial  \u00b7  "
            f"{report.tasks_failed} failed"
        )
        lines.append(f"Cost: ${report.cost_usd:.2f}  \u00b7  Duration: {duration}")

        # Merge results.
        merged = sum(1 for m in report.merges if m.action == "merged")
        skipped = sum(1 for m in report.merges if m.action != "merged")
        if report.merges:
            lines.append("")
            lines.append("Merge results (Opus):")
            if merged:
                lines.append(
                    f"[green]\u2713 {merged} branch{'es' if merged != 1 else ''} cherry-picked to main[/green]"
                )
            if skipped:
                lines.append(f"[dim]\u2298 {skipped} branch{'es' if skipped != 1 else ''} skipped[/dim]")

        # Cleanup stats.
        lines.append("")
        lines.append("Cleanup:")
        lines.append(
            f"[green]\u2713 {report.worktrees_removed} worktrees removed  "
            f"\u00b7  {report.branches_deleted} branches deleted[/green]"
        )
        if report.tasks_partial:
            lines.append(f"[green]\u2713 {report.tasks_partial} partial tickets annotated[/green]")

        self.query_one("#report-body", Static).update("\n".join(lines))

    # ------------------------------------------------------------------
    # Key bindings / actions
    # ------------------------------------------------------------------

    def action_cancel_drain(self) -> None:
        """Cancel the drain if still in a cancellable phase."""
        if self._report_shown:
            self.dismiss(self._report)
            return

        if self._coordinator is not None and self._coordinator.cancellable:
            cancel_task = asyncio.ensure_future(self._coordinator.cancel())
            cancel_task.add_done_callback(lambda _: None)  # prevent unhandled
            self.dismiss(None)
        else:
            self.notify(
                "Cannot cancel \u2014 drain in progress",
                severity="warning",
            )

    def action_force_quit(self) -> None:
        """Force-kill everything and exit immediately."""
        self.app.exit(return_code=1)  # type: ignore[reportUnknownMemberType]

    def on_key(self, event: Key) -> None:
        """Dismiss the report view on any keypress."""
        if self._report_shown:
            event.prevent_default()
            self.dismiss(self._report)

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def on_unmount(self) -> None:
        """Cancel the background task if the screen is removed early."""
        if self._drain_task is not None and not self._drain_task.done():
            self._drain_task.cancel()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _format_duration(seconds: float) -> str:
    """Format a duration in seconds to a human-readable string."""
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    minutes, secs = divmod(total, 60)
    if minutes < 60:
        return f"{minutes}m {secs}s"
    hours, mins = divmod(minutes, 60)
    return f"{hours}h {mins}m"
