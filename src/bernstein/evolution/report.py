"""Evolution observability — history table and static report generation.

Data sources:
  .sdd/metrics/evolve_cycles.jsonl   — per-cycle summary records
  .sdd/evolution/experiments.jsonl   — per-experiment results (optional)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class CycleRecord:
    """One row from evolve_cycles.jsonl."""

    cycle: int
    timestamp: float
    iso_time: str
    focus_area: str
    tasks_completed: int
    tasks_failed: int
    tests_passed: int
    tests_failed: int
    commits_made: int
    duration_s: float
    tick: int = 0
    backoff_factor: float = 1.0
    consecutive_empty: int = 0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CycleRecord:
        return cls(
            cycle=int(d.get("cycle", 0)),
            timestamp=float(d.get("timestamp", 0.0)),
            iso_time=str(d.get("iso_time", "")),
            focus_area=str(d.get("focus_area", "")),
            tasks_completed=int(d.get("tasks_completed", 0)),
            tasks_failed=int(d.get("tasks_failed", 0)),
            tests_passed=int(d.get("tests_passed", 0)),
            tests_failed=int(d.get("tests_failed", 0)),
            commits_made=int(d.get("commits_made", 0)),
            duration_s=float(d.get("duration_s", 0.0)),
            tick=int(d.get("tick", 0)),
            backoff_factor=float(d.get("backoff_factor", 1.0)),
            consecutive_empty=int(d.get("consecutive_empty", 0)),
        )

    @property
    def success_rate(self) -> float:
        total = self.tasks_completed + self.tasks_failed
        if total == 0:
            return 0.0
        return self.tasks_completed / total

    @property
    def test_pass_rate(self) -> float:
        total = self.tests_passed + self.tests_failed
        if total == 0:
            return 0.0
        return self.tests_passed / total


@dataclass
class ExperimentRecord:
    """One row from experiments.jsonl (optional, may be absent)."""

    proposal_id: str
    title: str
    risk_level: str
    accepted: bool
    delta: float
    cost_usd: float
    reason: str
    timestamp: float

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> ExperimentRecord:
        return cls(
            proposal_id=str(d.get("proposal_id", "")),
            title=str(d.get("title", "")),
            risk_level=str(d.get("risk_level", "")),
            accepted=bool(d.get("accepted", False)),
            delta=float(d.get("delta", 0.0)),
            cost_usd=float(d.get("cost_usd", 0.0)),
            reason=str(d.get("reason", "")),
            timestamp=float(d.get("timestamp", 0.0)),
        )


# ---------------------------------------------------------------------------
# Core report class
# ---------------------------------------------------------------------------


class EvolutionReport:
    """Load and summarise evolution history from .sdd/ data files.

    Usage::

        report = EvolutionReport(state_dir=Path(".sdd"))
        report.load()
        report.print_status()          # rich table to stdout
        report.export_markdown(path)   # static report file
    """

    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir
        self.cycles: list[CycleRecord] = []
        self.experiments: list[ExperimentRecord] = []

    # ------------------------------------------------------------------
    # Loading
    # ------------------------------------------------------------------

    def load(self) -> None:
        """Load cycles and experiments from JSONL files."""
        self.cycles = self._load_cycles()
        self.experiments = self._load_experiments()

    def _load_cycles(self) -> list[CycleRecord]:
        path = self.state_dir / "metrics" / "evolve_cycles.jsonl"
        if not path.exists():
            return []
        records: list[CycleRecord] = []
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(CycleRecord.from_dict(json.loads(line)))
            except (json.JSONDecodeError, KeyError):
                continue
        return records

    def _load_experiments(self) -> list[ExperimentRecord]:
        path = self.state_dir / "evolution" / "experiments.jsonl"
        if not path.exists():
            return []
        records: list[ExperimentRecord] = []
        for line in path.read_text().splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(ExperimentRecord.from_dict(json.loads(line)))
            except (json.JSONDecodeError, KeyError):
                continue
        return records

    # ------------------------------------------------------------------
    # Aggregated stats
    # ------------------------------------------------------------------

    @property
    def total_cycles(self) -> int:
        return len(self.cycles)

    @property
    def total_tasks_completed(self) -> int:
        return sum(c.tasks_completed for c in self.cycles)

    @property
    def total_tasks_failed(self) -> int:
        return sum(c.tasks_failed for c in self.cycles)

    @property
    def total_commits(self) -> int:
        return sum(c.commits_made for c in self.cycles)

    @property
    def experiments_accepted(self) -> int:
        return sum(1 for e in self.experiments if e.accepted)

    @property
    def experiments_rejected(self) -> int:
        return sum(1 for e in self.experiments if not e.accepted)

    @property
    def total_experiment_cost_usd(self) -> float:
        return sum(e.cost_usd for e in self.experiments)

    @property
    def first_tests_passed(self) -> int:
        for c in self.cycles:
            if c.tests_passed > 0:
                return c.tests_passed
        return 0

    @property
    def last_tests_passed(self) -> int:
        for c in reversed(self.cycles):
            if c.tests_passed > 0:
                return c.tests_passed
        return 0

    @property
    def test_delta(self) -> int:
        return self.last_tests_passed - self.first_tests_passed

    # ------------------------------------------------------------------
    # Sparkline
    # ------------------------------------------------------------------

    @staticmethod
    def _sparkline(values: list[float]) -> str:
        """Return an ASCII sparkline for a list of float values."""
        bars = " ▁▂▃▄▅▆▇█"
        if not values:
            return ""
        mn, mx = min(values), max(values)
        span = mx - mn or 1.0
        chars = [bars[min(8, int((v - mn) / span * 8))] for v in values]
        return "".join(chars)

    def _tests_sparkline(self) -> str:
        vals = [float(c.tests_passed) for c in self.cycles if c.tests_passed > 0]
        return self._sparkline(vals)

    def _success_rate_sparkline(self) -> str:
        vals = [c.success_rate for c in self.cycles]
        return self._sparkline(vals)

    # ------------------------------------------------------------------
    # Rich table output
    # ------------------------------------------------------------------

    def print_status(self) -> None:
        """Print the evolution history as a Rich table to stdout."""
        from rich.console import Console
        from rich.panel import Panel
        from rich.table import Table

        console = Console()

        if not self.cycles:
            console.print("[dim]No evolution cycles found in .sdd/metrics/evolve_cycles.jsonl[/dim]")
            return

        # Summary panel
        lines = [
            f"[bold]Cycles:[/bold] {self.total_cycles}  "
            f"[bold]Tasks completed:[/bold] {self.total_tasks_completed}  "
            f"[bold]Commits:[/bold] {self.total_commits}",
            f"[bold]Tests:[/bold] {self.first_tests_passed} → {self.last_tests_passed} "
            f"([green]+{self.test_delta}[/green])"
            if self.test_delta >= 0
            else f"[bold]Tests:[/bold] {self.first_tests_passed} → {self.last_tests_passed} "
            f"([red]{self.test_delta}[/red])",
            f"[bold]Test trajectory:[/bold] {self._tests_sparkline()}",
        ]
        if self.experiments:
            lines.append(
                f"[bold]Experiments:[/bold] {len(self.experiments)}  "
                f"accepted={self.experiments_accepted}  "
                f"rejected={self.experiments_rejected}  "
                f"cost=${self.total_experiment_cost_usd:.4f}"
            )

        console.print(Panel("\n".join(lines), title="Evolution Summary", border_style="blue"))

        # Per-cycle table
        table = Table(
            title="Evolution Cycle History",
            show_lines=False,
            header_style="bold cyan",
        )
        table.add_column("#", justify="right", style="dim", min_width=4)
        table.add_column("Time", min_width=16)
        table.add_column("Focus", min_width=14)
        table.add_column("Tasks ✓/✗", justify="right", min_width=10)
        table.add_column("Tests", justify="right", min_width=8)
        table.add_column("Success%", justify="right", min_width=9)
        table.add_column("Commits", justify="right", min_width=7)
        table.add_column("Duration", justify="right", min_width=9)

        for i, c in enumerate(self.cycles):
            dt = datetime.fromtimestamp(c.timestamp, tz=UTC)
            time_str = dt.strftime("%m-%d %H:%M")

            task_str = f"[green]{c.tasks_completed}[/green]/[red]{c.tasks_failed}[/red]"
            test_str = str(c.tests_passed) if c.tests_passed > 0 else "[dim]—[/dim]"
            success_pct = f"{c.success_rate:.0%}"
            dur = f"{c.duration_s:.0f}s"
            commits_str = f"[green]{c.commits_made}[/green]" if c.commits_made > 0 else "[dim]0[/dim]"

            # Highlight row if tests improved vs previous cycle
            row_style = ""
            if i > 0 and self.cycles[i - 1].tests_passed > 0 and c.tests_passed > self.cycles[i - 1].tests_passed:
                row_style = "bold"

            table.add_row(
                str(i + 1),
                time_str,
                c.focus_area,
                task_str,
                test_str,
                success_pct,
                commits_str,
                dur,
                style=row_style,
            )

        console.print(table)

        if self.experiments:
            self._print_experiments_table(console)

    def _print_experiments_table(self, console: Any) -> None:
        from rich.table import Table

        table = Table(
            title="Experiment Results",
            show_lines=False,
            header_style="bold magenta",
        )
        table.add_column("ID", style="dim", min_width=12)
        table.add_column("Title", min_width=30)
        table.add_column("Risk", min_width=8)
        table.add_column("Delta", justify="right", min_width=8)
        table.add_column("Cost", justify="right", min_width=8)
        table.add_column("Result", min_width=10)

        for e in self.experiments[-50:]:  # cap display at 50
            color = "green" if e.accepted else "red"
            delta_str = f"{e.delta:+.3f}" if e.delta != 0 else "—"
            table.add_row(
                e.proposal_id[:12],
                e.title[:40],
                e.risk_level,
                delta_str,
                f"${e.cost_usd:.4f}",
                f"[{color}]{'accepted' if e.accepted else 'rejected'}[/{color}]",
            )

        console.print(table)

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def export_markdown(self, output_path: Path) -> None:
        """Write a Markdown report to output_path."""
        lines: list[str] = []
        now = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M UTC")

        lines.append("# Bernstein Evolution Report")
        lines.append(f"\n_Generated {now}_\n")

        # Summary section
        lines.append("## Summary\n")
        lines.append("| Metric | Value |")
        lines.append("|--------|-------|")
        lines.append(f"| Total cycles | {self.total_cycles} |")
        lines.append(f"| Tasks completed | {self.total_tasks_completed} |")
        lines.append(f"| Tasks failed | {self.total_tasks_failed} |")
        lines.append(f"| Commits made | {self.total_commits} |")
        lines.append(
            f"| Tests (first -> last) | {self.first_tests_passed} -> {self.last_tests_passed} (+{self.test_delta}) |"
        )
        if self.experiments:
            lines.append(f"| Experiments run | {len(self.experiments)} |")
            lines.append(f"| Experiments accepted | {self.experiments_accepted} |")
            lines.append(f"| Experiments rejected | {self.experiments_rejected} |")
            lines.append(f"| Total evolution cost | ${self.total_experiment_cost_usd:.4f} |")

        # Trajectory sparkline
        lines.append("\n## Test Count Trajectory\n")
        lines.append(f"```\n{self._tests_sparkline()}\n```")

        # Cycle breakdown
        lines.append("\n## Cycle Breakdown\n")
        lines.append("| # | Time (UTC) | Focus | Tasks ✓ | Tasks ✗ | Tests | Success% | Commits | Duration |")
        lines.append("|---|-----------|-------|---------|---------|-------|----------|---------|----------|")

        for i, c in enumerate(self.cycles):
            dt = datetime.fromtimestamp(c.timestamp, tz=UTC)
            time_str = dt.strftime("%Y-%m-%d %H:%M")
            test_str = str(c.tests_passed) if c.tests_passed > 0 else "—"
            lines.append(
                f"| {i + 1} | {time_str} | {c.focus_area} | {c.tasks_completed} "
                f"| {c.tasks_failed} | {test_str} | {c.success_rate:.0%} "
                f"| {c.commits_made} | {c.duration_s:.0f}s |"
            )

        # Experiments section
        if self.experiments:
            lines.append("\n## Experiments\n")
            lines.append("| ID | Title | Risk | Delta | Cost | Result |")
            lines.append("|----|-------|------|-------|------|--------|")
            for e in self.experiments:
                delta_str = f"{e.delta:+.3f}" if e.delta != 0 else "—"
                result = "✓ accepted" if e.accepted else "✗ rejected"
                lines.append(
                    f"| {e.proposal_id[:12]} | {e.title[:40]} | {e.risk_level} "
                    f"| {delta_str} | ${e.cost_usd:.4f} | {result} |"
                )

        output_path.write_text("\n".join(lines) + "\n")

    def export_html(self, output_path: Path) -> None:
        """Write a static HTML report to output_path."""
        now = datetime.now(tz=UTC).strftime("%Y-%m-%d %H:%M UTC")
        sparkline = self._tests_sparkline()
        success_sparkline = self._success_rate_sparkline()

        # Build cycle rows
        cycle_rows: list[str] = []
        for i, c in enumerate(self.cycles):
            dt = datetime.fromtimestamp(c.timestamp, tz=UTC)
            time_str = dt.strftime("%Y-%m-%d %H:%M")
            test_str = str(c.tests_passed) if c.tests_passed > 0 else "—"
            row_class = ""
            if i > 0 and self.cycles[i - 1].tests_passed > 0 and c.tests_passed > self.cycles[i - 1].tests_passed:
                row_class = ' class="improved"'
            cycle_rows.append(
                f"<tr{row_class}>"
                f"<td>{i + 1}</td>"
                f"<td>{time_str}</td>"
                f"<td>{c.focus_area}</td>"
                f"<td class='good'>{c.tasks_completed}</td>"
                f"<td class='bad'>{c.tasks_failed}</td>"
                f"<td>{test_str}</td>"
                f"<td>{c.success_rate:.0%}</td>"
                f"<td>{c.commits_made}</td>"
                f"<td>{c.duration_s:.0f}s</td>"
                f"</tr>"
            )

        # Build experiment rows
        exp_rows: list[str] = []
        for e in self.experiments:
            delta_str = f"{e.delta:+.3f}" if e.delta != 0 else "—"
            result_class = "good" if e.accepted else "bad"
            result_txt = "accepted" if e.accepted else "rejected"
            exp_rows.append(
                f"<tr>"
                f"<td>{e.proposal_id[:12]}</td>"
                f"<td>{e.title[:50]}</td>"
                f"<td>{e.risk_level}</td>"
                f"<td>{delta_str}</td>"
                f"<td>${e.cost_usd:.4f}</td>"
                f"<td class='{result_class}'>{result_txt}</td>"
                f"</tr>"
            )

        experiments_section = ""
        if self.experiments:
            experiments_section = f"""
<h2>Experiments ({len(self.experiments)} total)</h2>
<p>Accepted: <strong>{self.experiments_accepted}</strong> |
   Rejected: <strong>{self.experiments_rejected}</strong> |
   Total cost: <strong>${self.total_experiment_cost_usd:.4f}</strong></p>
<table>
  <thead>
    <tr><th>ID</th><th>Title</th><th>Risk</th><th>Delta</th><th>Cost</th><th>Result</th></tr>
  </thead>
  <tbody>
    {"".join(exp_rows)}
  </tbody>
</table>
"""

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>Bernstein Evolution Report</title>
  <style>
    body {{ font-family: monospace; max-width: 1100px; margin: 2em auto; padding: 0 1em; background: #0d1117; color: #c9d1d9; }}
    h1 {{ color: #58a6ff; }}
    h2 {{ color: #79c0ff; border-bottom: 1px solid #30363d; padding-bottom: .3em; }}
    table {{ border-collapse: collapse; width: 100%; margin: 1em 0; }}
    th {{ background: #161b22; color: #79c0ff; padding: .5em 1em; text-align: left; }}
    td {{ padding: .4em 1em; border-bottom: 1px solid #21262d; }}
    tr:hover {{ background: #161b22; }}
    tr.improved {{ background: #0d2b0d; }}
    .good {{ color: #3fb950; }}
    .bad  {{ color: #f85149; }}
    .sparkline {{ font-size: 1.6em; letter-spacing: .1em; }}
    .summary-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(160px, 1fr)); gap: 1em; margin: 1em 0; }}
    .metric {{ background: #161b22; border: 1px solid #30363d; border-radius: 6px; padding: 1em; text-align: center; }}
    .metric .value {{ font-size: 2em; font-weight: bold; color: #58a6ff; }}
    .metric .label {{ color: #8b949e; font-size: .85em; margin-top: .3em; }}
    .generated {{ color: #8b949e; font-size: .85em; }}
  </style>
</head>
<body>
<h1>🎼 Bernstein Evolution Report</h1>
<p class="generated">Generated {now}</p>

<h2>Summary</h2>
<div class="summary-grid">
  <div class="metric"><div class="value">{self.total_cycles}</div><div class="label">Cycles</div></div>
  <div class="metric"><div class="value good">{self.total_tasks_completed}</div><div class="label">Tasks Completed</div></div>
  <div class="metric"><div class="value bad">{self.total_tasks_failed}</div><div class="label">Tasks Failed</div></div>
  <div class="metric"><div class="value">{self.total_commits}</div><div class="label">Commits</div></div>
  <div class="metric"><div class="value good">+{self.test_delta}</div><div class="label">Test Growth</div></div>
</div>

<h2>Test Count Trajectory</h2>
<p class="sparkline">{sparkline}</p>
<p>{self.first_tests_passed} → {self.last_tests_passed} tests</p>

<h2>Success Rate Trajectory</h2>
<p class="sparkline">{success_sparkline}</p>

<h2>Cycle Breakdown</h2>
<table>
  <thead>
    <tr><th>#</th><th>Time (UTC)</th><th>Focus</th><th>✓ Done</th><th>✗ Failed</th><th>Tests</th><th>Success%</th><th>Commits</th><th>Duration</th></tr>
  </thead>
  <tbody>
    {"".join(cycle_rows)}
  </tbody>
</table>

{experiments_section}
</body>
</html>
"""
        output_path.write_text(html)
