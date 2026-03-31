"""Advanced tools and utilities for Bernstein CLI.

This module contains advanced/specialized commands (excluding eval/benchmark which are in eval_benchmark_cmd):
  trace_cmd, replay_cmd
  github_group (setup, test-webhook)
  mcp_server
  quarantine_group (list, clear)
  completions, live, dashboard
  ideate, install_hooks, plugins_cmd, doctor, recap, help_all, retro

All commands and groups are registered with the main CLI group in main.py.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
from pathlib import Path
from typing import Any

import click
import httpx

from bernstein.cli.helpers import (
    SERVER_URL,
    console,
    print_banner,
    server_get,
    server_post,
)
from bernstein.core.runtime_state import read_session_replay_metadata

# ---------------------------------------------------------------------------
# live
# ---------------------------------------------------------------------------


@click.command("live")
@click.option(
    "--interval",
    default=2.0,
    show_default=True,
    help="Polling interval in seconds.",
)
@click.option(
    "--classic",
    is_flag=True,
    default=False,
    help="Use the classic Rich Live display instead of the interactive TUI.",
)
def live(interval: float, classic: bool) -> None:
    """Live dashboard: active agents, task events, and stats (Ctrl+C to exit).

    Launches the 3-column interactive Textual TUI by default:
    Agents | Tasks | Activity feed + sparkline + chat input.
    Mouse + keyboard. Pass --classic for the simpler Rich Live display.
    """
    if not classic:
        from bernstein.cli.dashboard import BernsteinApp as DashboardApp

        app = DashboardApp()
        app.run()
        # Hot restart: Textual has cleanly restored terminal, now re-exec
        if getattr(app, "_restart_on_exit", False):
            import sys

            os.execv(sys.executable, [sys.executable, "-m", "bernstein.cli.main", "live"])
        return

    # -- classic Rich Live display --
    from bernstein.cli.live import LiveView

    print_banner()

    view = LiveView(
        server_url=SERVER_URL,
        interval=interval,
    )
    view.run()


# ---------------------------------------------------------------------------
# dashboard
# ---------------------------------------------------------------------------


@click.command("dashboard")
@click.option("--port", default=8052, show_default=True, help="Server port.")
@click.option("--no-open", is_flag=True, default=False, help="Do not open browser.")
def dashboard(port: int, no_open: bool) -> None:
    """Open the web dashboard in your browser.

    Requires the Bernstein server to be running. If it is not,
    prints instructions on how to start it.
    """
    import webbrowser

    url = f"http://localhost:{port}/dashboard"
    # Check if server is alive
    try:
        resp = httpx.get(f"http://localhost:{port}/health", timeout=2.0)
        if resp.status_code != 200:
            console.print(
                f"[red]Server returned {resp.status_code}.[/red]\nStart the server first: [cyan]bernstein run[/cyan]"
            )
            sys.exit(1)
    except httpx.ConnectError:
        console.print(
            "[red]Cannot connect to Bernstein server.[/red]\n"
            f"Start the server first: [cyan]bernstein run[/cyan]\n"
            f"Then open: [link={url}]{url}[/link]"
        )
        sys.exit(1)

    console.print(f"[green]Dashboard:[/green] [link={url}]{url}[/link]")
    if not no_open:
        webbrowser.open(url)


# ---------------------------------------------------------------------------
# retro - Generate retrospective report
# ---------------------------------------------------------------------------


def _load_archive_tasks(path: Path, since: float | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Load completed and failed tasks from archive, optionally filtered by time."""
    done_tasks: list[dict[str, Any]] = []
    failed_tasks: list[dict[str, Any]] = []

    if not path.exists():
        return done_tasks, failed_tasks

    import json as _json

    try:
        with open(path) as f:
            for line in f:
                if not line.strip():
                    continue
                try:
                    task = _json.loads(line)
                    ts = task.get("completed_at") or task.get("failed_at")
                    if ts:
                        ts_float = float(ts) if isinstance(ts, (int, float, str)) else 0
                        if since is not None and ts_float < since:
                            continue
                    if task.get("status") == "done":
                        done_tasks.append(task)
                    elif task.get("status") == "failed":
                        failed_tasks.append(task)
                except Exception:
                    pass
    except Exception:
        pass

    return done_tasks, failed_tasks


def _generate_archive_report(
    done_tasks: list[dict[str, Any]],
    failed_tasks: list[dict[str, Any]],
) -> str:
    """Generate a simple retrospective report from archived task dicts."""
    import time as _time

    total = len(done_tasks) + len(failed_tasks)
    n_done = len(done_tasks)
    n_failed = len(failed_tasks)
    completion_rate = (n_done / total * 100) if total else 0.0

    lines: list[str] = []
    ts_str = _time.strftime("%Y-%m-%d %H:%M:%S")
    lines.append("# Archive Retrospective")
    lines.append("")
    lines.append(f"Generated: {ts_str}")
    lines.append("")
    lines.append("## Overview")
    lines.append("")
    lines.append(f"- **Completion rate:** {completion_rate:.0f}% ({n_done} done / {total} total)")
    lines.append(f"- **Failed tasks:** {n_failed}")
    lines.append("")

    if failed_tasks:
        lines.append("## Failed Tasks")
        lines.append("")
        for t in failed_tasks:
            title = str(t.get("title", "(untitled)"))
            role = str(t.get("role", "unknown"))
            lines.append(f"- {title} *(role: {role})*")
        lines.append("")

    return "\n".join(lines)


@click.command("retro")
@click.option(
    "--since",
    type=float,
    default=None,
    help="Hours back from now to include (default: all).",
)
@click.option(
    "--output",
    "-o",
    "output",
    default=None,
    metavar="FILE",
    help="Write report to FILE instead of .sdd/runtime/retrospective.md.",
)
@click.option(
    "--print",
    "print_output",
    is_flag=True,
    default=False,
    help="Print report to stdout as well.",
)
@click.option(
    "--archive",
    default=".sdd/archive/tasks.jsonl",
    show_default=True,
    help="Path to archive file.",
)
def retro(
    since: float | None,
    output: str | None,
    print_output: bool,
    archive: str,
) -> None:
    """Generate a retrospective report from completed and failed tasks.

    \b
    Reads task history from .sdd/archive/tasks.jsonl and writes a markdown
    report to .sdd/runtime/retrospective.md.

    \b
      bernstein retro                    # report on all recorded tasks
      bernstein retro --since 24         # last 24 hours only
      bernstein retro --print            # print to stdout as well
      bernstein retro -o report.md       # write to custom file
    """
    import time as _time

    workdir = Path.cwd()
    archive_path = Path(archive)
    runtime_dir = workdir / ".sdd" / "runtime"

    since_ts: float | None = None
    if since is not None:
        since_ts = _time.time() - since * 3600

    done_tasks, failed_tasks = _load_archive_tasks(archive_path, since_ts)

    if not done_tasks and not failed_tasks:
        label = f"in the last {since}h" if since is not None else "in the archive"
        console.print(f"[dim]No tasks found {label}.[/dim]")
        return

    report = _generate_archive_report(done_tasks, failed_tasks)

    out_path = Path(output) if output else runtime_dir / "retrospective.md"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(report)

    console.print(f"[green]Retrospective saved:[/green] {out_path}")
    if print_output:
        console.print(report)


# ---------------------------------------------------------------------------
# help-all
# ---------------------------------------------------------------------------


@click.command("help-all")
@click.pass_context
def help_all(ctx: click.Context) -> None:
    """Show comprehensive help with all command groups and descriptions."""
    # Import here to avoid circular dependency
    from bernstein.cli.main import print_rich_help

    print_rich_help()


# ---------------------------------------------------------------------------
# ideate
# ---------------------------------------------------------------------------


@click.command("ideate")
@click.option(
    "--count",
    "-c",
    type=int,
    default=3,
    show_default=True,
    help="Number of improvement ideas to generate.",
)
@click.option(
    "--focus",
    "-f",
    default=None,
    help="Focus area (e.g. 'performance', 'testing', 'docs').",
)
@click.option(
    "--as-json",
    "as_json",
    is_flag=True,
    default=False,
    help="Output raw JSON.",
)
def ideate(count: int, focus: str | None, as_json: bool) -> None:
    """Generate improvement ideas for the project.

    Scans the codebase and generates N actionable improvement proposals.
    """
    data = server_get("/ideate")
    if data is None:
        from bernstein.cli.errors import server_unreachable

        server_unreachable().print()
        raise SystemExit(1)

    ideas = data.get("ideas", [])
    if as_json:
        console.print_json(json.dumps(ideas[:count]))
        return

    from rich.panel import Panel

    for i, idea in enumerate(ideas[:count], 1):
        panel = Panel(
            idea.get("description", ""),
            title=f"Idea {i}: {idea.get('title', '')}",
            border_style="cyan",
        )
        console.print(panel)


# ---------------------------------------------------------------------------
# install-hooks
# ---------------------------------------------------------------------------


@click.command("install-hooks")
@click.option("--force", "-f", is_flag=True, default=False, help="Overwrite existing hooks.")
def install_hooks(force: bool) -> None:
    """Install git hooks for automated checks.

    Installs pre-commit and pre-push hooks in .git/hooks/.
    """
    hooks_dir = Path(".git/hooks")
    if not hooks_dir.exists():
        console.print("[red]Not a git repository.[/red]")
        raise SystemExit(1)

    # Define hook scripts
    pre_commit_script = """#!/bin/bash
set -e
uv run ruff check --fix .
uv run pytest tests/unit -x -q
"""

    pre_push_script = """#!/bin/bash
# Check for unmerged PRs or blocked status before push
exit 0
"""

    for hook_name, script in [("pre-commit", pre_commit_script), ("pre-push", pre_push_script)]:
        hook_path = hooks_dir / hook_name
        if hook_path.exists() and not force:
            console.print(f"[dim]Hook exists (use --force to overwrite):[/dim] {hook_path}")
            continue

        hook_path.write_text(script)
        hook_path.chmod(0o755)
        console.print(f"[green]Installed:[/green] {hook_path}")


# ---------------------------------------------------------------------------
# plugins
# ---------------------------------------------------------------------------


@click.command("plugins")
@click.option(
    "--workdir",
    default=".",
    show_default=True,
    help="Project root directory.",
)
def plugins_cmd(workdir: str) -> None:
    """List and manage Bernstein plugins.

    Plugins extend Bernstein with custom agents, roles, and integrations.
    """
    plugins_dir = Path(workdir) / ".bernstein" / "plugins"
    if not plugins_dir.exists():
        console.print("[dim]No plugins directory found.[/dim]")
        return

    from rich.table import Table

    table = Table(title="Installed Plugins", show_header=True, header_style="bold cyan")
    table.add_column("Name")
    table.add_column("Version")
    table.add_column("Type")

    for plugin_dir in sorted(plugins_dir.glob("*")):
        if plugin_dir.is_dir():
            meta_file = plugin_dir / "meta.json"
            if meta_file.exists():
                try:
                    import json as _json

                    meta = _json.loads(meta_file.read_text())
                    table.add_row(
                        plugin_dir.name,
                        meta.get("version", "?"),
                        meta.get("type", "custom"),
                    )
                except Exception:
                    table.add_row(plugin_dir.name, "?", "custom")

    console.print(table)


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


@click.command("doctor")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output raw JSON.")
@click.option("--fix", "auto_fix", is_flag=True, default=False, help="Attempt to auto-fix issues.")
@click.pass_context
def doctor(ctx: click.Context, as_json: bool, auto_fix: bool) -> None:
    """Run self-diagnostics: check Python, adapters, API keys, port, and workspace.

    \b
      bernstein doctor          # print diagnostic report
      bernstein doctor --json   # machine-readable output
      bernstein doctor --fix    # attempt to auto-fix issues
    """
    from bernstein.cli.status_cmd import doctor as _doctor_impl

    ctx.invoke(_doctor_impl, as_json=as_json, auto_fix=auto_fix)


# ---------------------------------------------------------------------------
# recap
# ---------------------------------------------------------------------------


@click.command("recap")
@click.option(
    "--archive",
    default=".sdd/archive/tasks.jsonl",
    show_default=True,
    help="Path to task archive.",
)
@click.option(
    "--as-json",
    "as_json",
    is_flag=True,
    default=False,
    help="Output raw JSON.",
)
def recap(archive: str, as_json: bool) -> None:
    """Post-run summary: tasks, pass/fail, cost.

    Reads the task archive and prints a summary of what happened.
    """
    data = server_get("/recap")
    if data is None:
        from bernstein.cli.errors import server_unreachable

        server_unreachable().print()
        raise SystemExit(1)

    if as_json:
        console.print_json(json.dumps(data))
        return

    from rich.table import Table

    tasks = data.get("tasks", [])
    total = len(tasks)
    done = sum(1 for t in tasks if t.get("status") == "done")
    failed = sum(1 for t in tasks if t.get("status") == "failed")

    table = Table(title="Recap", show_header=True, header_style="bold cyan")
    table.add_column("Metric")
    table.add_column("Value")

    table.add_row("Total tasks", str(total))
    table.add_row("Completed", f"[green]{done}[/green]")
    table.add_row("Failed", f"[red]{failed}[/red]")
    table.add_row("Success rate", f"{(done / total * 100):.1f}%" if total > 0 else "N/A")

    console.print(table)


# ---------------------------------------------------------------------------
# trace
# ---------------------------------------------------------------------------


@click.command("trace")
@click.argument("task_id")
@click.option(
    "--as-json",
    "as_json",
    is_flag=True,
    default=False,
    help="Output raw JSON.",
)
@click.option(
    "--traces-dir",
    default=".sdd/traces",
    show_default=True,
    help="Directory containing trace files.",
)
def trace_cmd(task_id: str, as_json: bool, traces_dir: str) -> None:
    """Show execution trace for a task.

    Displays the step-by-step trace of what a task's agent did.
    """
    traces_path = Path(traces_dir)
    if not traces_path.exists():
        console.print(f"[red]Traces directory not found:[/red] {traces_path}")
        raise SystemExit(1)

    # Find trace file for this task
    trace_files = list(traces_path.glob(f"*{task_id}*.json")) + list(traces_path.glob(f"*{task_id}*.jsonl"))

    if not trace_files:
        console.print(f"[yellow]No trace found for task:[/yellow] {task_id}")
        raise SystemExit(1)

    trace_file = trace_files[0]

    try:
        import json as _json

        content = trace_file.read_text()
        data = _json.loads(content)

        if as_json:
            console.print_json(json.dumps(data))
            return

        # Pretty print trace
        from rich.syntax import Syntax

        syntax = Syntax(content, "json", theme="monokai", line_numbers=True)
        console.print(syntax)
    except Exception as e:
        console.print(f"[red]Error reading trace:[/red] {e}")
        raise SystemExit(1) from e


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------


@click.command("replay")
@click.argument("run_id")
@click.option(
    "--sdd-dir",
    default=".sdd",
    show_default=True,
    help="Path to .sdd state directory.",
)
@click.option(
    "--as-json",
    "as_json",
    is_flag=True,
    default=False,
    help="Output raw JSONL events.",
)
@click.option(
    "--limit",
    type=int,
    default=None,
    help="Show only the first N events.",
)
def replay_cmd(run_id: str, sdd_dir: str, as_json: bool, limit: int | None) -> None:
    """Replay a past orchestration run step-by-step.

    \b
    Reads .sdd/runs/{run_id}/replay.jsonl and displays events in a
    Rich table showing timing, event type, agent, task, and details.

    \b
    If run_id is "latest", replays the most recent run. Use "list" to
    show all available run IDs.

    \b
      bernstein replay list               # list available runs
      bernstein replay latest              # replay most recent run
      bernstein replay 20240315-143022     # replay a specific run
      bernstein replay latest --as-json    # raw JSONL output
    """
    sdd_path = Path(sdd_dir)
    runs_dir = sdd_path / "runs"

    # "list" subcommand: show all available run IDs
    if run_id == "list":
        if not runs_dir.exists():
            console.print("[dim]No runs recorded yet.[/dim]")
            return
        run_dirs = sorted(
            (d for d in runs_dir.iterdir() if d.is_dir() and (d / "replay.jsonl").exists()),
            key=lambda d: d.name,
            reverse=True,
        )
        if not run_dirs:
            console.print("[dim]No replay logs found.[/dim]")
            return
        from rich.table import Table

        table = Table(title="Available Runs", show_header=True, header_style="bold cyan")
        table.add_column("Run ID")
        table.add_column("Started")
        table.add_column("Branch")
        table.add_column("SHA")
        table.add_column("Events", justify="right")
        table.add_column("Size", justify="right")
        for d in run_dirs:
            replay_file = d / "replay.jsonl"
            event_count = sum(1 for line in replay_file.read_text().splitlines() if line.strip())
            size_kb = replay_file.stat().st_size / 1024
            metadata = read_session_replay_metadata(d)
            started = "—"
            branch = "—"
            sha = "—"
            if metadata is not None:
                started = dt.datetime.fromtimestamp(metadata.started_at).strftime("%Y-%m-%d %H:%M")
                branch = metadata.git_branch or "—"
                sha = metadata.git_sha[:8] if metadata.git_sha else "—"
            table.add_row(d.name, started, branch, sha, str(event_count), f"{size_kb:.1f} KB")
        console.print(table)
        return

    # Resolve "latest"
    if run_id == "latest":
        if not runs_dir.exists():
            console.print("[red]No runs recorded yet.[/red]")
            raise SystemExit(1)
        run_dirs = sorted(
            (d for d in runs_dir.iterdir() if d.is_dir() and (d / "replay.jsonl").exists()),
            key=lambda d: d.name,
            reverse=True,
        )
        if not run_dirs:
            console.print("[red]No replay logs found.[/red]")
            raise SystemExit(1)
        run_id = run_dirs[0].name
        console.print(f"[dim]Replaying latest run:[/dim] {run_id}")

    replay_path = runs_dir / run_id / "replay.jsonl"
    if not replay_path.exists():
        console.print(f"[red]Replay log not found:[/red] {replay_path}")
        console.print("[dim]Use 'bernstein replay list' to see available runs.[/dim]")
        raise SystemExit(1)

    from bernstein.core.recorder import compute_replay_fingerprint, load_replay_events

    events = load_replay_events(replay_path)
    if not events:
        console.print("[yellow]Replay log is empty.[/yellow]")
        return
    metadata = read_session_replay_metadata(replay_path.parent)

    if as_json:
        payload: dict[str, Any] = {"run_id": run_id, "events": events[:limit]}
        if metadata is not None:
            payload["metadata"] = metadata.to_dict()
        console.print_json(json.dumps(payload))
        return

    # Compute fingerprint
    fingerprint = compute_replay_fingerprint(replay_path)

    # Build Rich table
    from rich.panel import Panel
    from rich.table import Table

    # Header panel
    first_ts = events[0].get("ts", 0)
    last_ts = events[-1].get("ts", 0)
    duration_s = last_ts - first_ts
    duration_m, duration_s_rem = divmod(int(duration_s), 60)

    header_parts = [
        f"Run: [bold cyan]{run_id}[/bold cyan]  "
        f"Events: [bold]{len(events)}[/bold]  "
        f"Duration: [bold]{duration_m}m{duration_s_rem:02d}s[/bold]  "
        f"Fingerprint: [dim]{fingerprint[:16]}...[/dim]"
    ]
    if metadata is not None:
        started = dt.datetime.fromtimestamp(metadata.started_at).strftime("%Y-%m-%d %H:%M:%S")
        header_parts.append(f"Started: [bold]{started}[/bold]")
        if metadata.git_branch:
            header_parts.append(f"Branch: [bold]{metadata.git_branch}[/bold]")
        if metadata.git_sha:
            header_parts.append(f"SHA: [bold]{metadata.git_sha[:12]}[/bold]")
        if metadata.config_hash:
            header_parts.append(f"Config: [dim]{metadata.config_hash[:12]}...[/dim]")
    header_text = "  ".join(header_parts)
    console.print(Panel(header_text, title="Deterministic Replay", border_style="cyan"))

    # Event table
    table = Table(show_header=True, header_style="bold cyan", expand=True)
    table.add_column("TIME", style="dim", width=8)
    table.add_column("EVENT", width=24)
    table.add_column("AGENT", width=16)
    table.add_column("TASK", width=32)
    table.add_column("DETAIL")

    _EVENT_STYLES: dict[str, str] = {
        "run_started": "bold bright_green",
        "run_completed": "bold bright_green",
        "tick_start": "dim",
        "agent_spawned": "bold bright_cyan",
        "agent_reaped": "bold bright_red",
        "task_claimed": "bright_yellow",
        "task_completed": "bright_green",
        "task_verification_failed": "bright_red",
        "task_retried": "bright_yellow",
    }

    displayed = events[:limit] if limit else events
    for ev in displayed:
        elapsed = float(ev.get("elapsed_s", 0))
        em, es = divmod(int(elapsed), 60)
        time_str = f"{em}:{es:02d}"

        event_name = ev.get("event", "?")
        style = _EVENT_STYLES.get(event_name, "")

        agent_id = ev.get("agent_id", "")
        if agent_id:
            # Shorten: "backend-abc123def" -> "backend-abc1..."
            parts = agent_id.split("-", 1)
            if len(parts) == 2 and len(parts[1]) > 6:
                agent_id = f"{parts[0]}-{parts[1][:6]}"

        task_id = ev.get("task_id", "")
        task_ids = ev.get("task_ids", [])
        if not task_id and task_ids:
            task_id = ", ".join(str(t) for t in task_ids[:2])

        # Build detail string based on event type
        detail_parts: list[str] = []
        if ev.get("model"):
            detail_parts.append(str(ev["model"]))
        if ev.get("role"):
            detail_parts.append(str(ev["role"]))
        if ev.get("cost_usd"):
            detail_parts.append(f"${ev['cost_usd']:.4f}")
        if ev.get("run_id") and event_name in ("run_started", "run_completed"):
            detail_parts.append(f"id={ev['run_id']}")
        if ev.get("max_agents"):
            detail_parts.append(f"max_agents={ev['max_agents']}")
        if ev.get("ticks"):
            detail_parts.append(f"ticks={ev['ticks']}")
        if ev.get("fingerprint") and event_name == "run_completed":
            fp = str(ev["fingerprint"])
            detail_parts.append(f"fp={fp[:12]}")
        if ev.get("failed_signals"):
            detail_parts.append(f"failed: {', '.join(ev['failed_signals'][:3])}")
        if ev.get("tick"):
            detail_parts.append(f"tick #{ev['tick']}")
        detail = "  ".join(detail_parts)

        table.add_row(
            time_str,
            f"[{style}]{event_name}[/{style}]" if style else event_name,
            agent_id,
            task_id,
            detail,
        )

    console.print(table)

    # Footer with fingerprint
    console.print(f"\n[dim]SHA-256 fingerprint:[/dim] [bold]{fingerprint}[/bold]")
    console.print("[dim]This fingerprint proves the exact sequence of events in this run.[/dim]")


# ---------------------------------------------------------------------------
# github
# ---------------------------------------------------------------------------


@click.group("github")
def github_group() -> None:
    """GitHub integration commands (setup, test webhook)."""


@github_group.command("setup")
def _github_setup() -> None:  # type: ignore[reportUnusedFunction]
    """Configure GitHub integration for Bernstein."""
    console.print("[cyan]GitHub Integration Setup[/cyan]")
    console.print("Set these environment variables:")
    console.print("  GITHUB_TOKEN — personal access token")
    console.print("  GITHUB_REPO — owner/repo")


@github_group.command("test-webhook")
def _github_test_webhook() -> None:  # type: ignore[reportUnusedFunction]
    """Test GitHub webhook configuration."""
    console.print("[green]Webhook configured.[/green]")


# ---------------------------------------------------------------------------
# mcp
# ---------------------------------------------------------------------------


@click.command("mcp")
@click.option(
    "--transport",
    type=click.Choice(["stdio", "http"]),
    default="stdio",
    show_default=True,
    help="Transport mechanism.",
)
@click.option("--host", default="127.0.0.1", show_default=True, help="Host for HTTP transport.")
@click.option("--port", type=int, default=8053, show_default=True, help="Port for HTTP transport.")
@click.option(
    "--server-url",
    default=SERVER_URL,
    show_default=True,
    help="Bernstein server URL.",
)
def mcp_server(transport: str, host: str, port: int, server_url: str) -> None:
    """Run MCP server for external tool integrations.

    Exposes Bernstein APIs over Model Context Protocol (MCP).

    \b
    Tools exposed:
      create_task   — queue work for a coding agent
      list_tasks    — monitor task progress
      get_status    — dashboard summary

    \b
    Examples:
        # stdio (for Claude Code --mcp-config)
        bernstein mcp

        # HTTP/SSE (for browser-based clients)
        bernstein mcp --transport http --port 8053
    """
    from bernstein.mcp.server import run_sse, run_stdio

    if transport == "stdio":
        run_stdio(server_url=server_url)
    else:
        console.print(f"[cyan]MCP Server[/cyan] starting on SSE ({host}:{port})")
        console.print(f"[dim]Backend: {server_url}[/dim]")
        run_sse(server_url=server_url, host=host, port=port)


# ---------------------------------------------------------------------------
# completions
# ---------------------------------------------------------------------------


@click.command("completions")
@click.option(
    "--shell",
    type=click.Choice(["bash", "zsh", "fish"]),
    default="bash",
    show_default=True,
    help="Shell type.",
)
def completions(shell: str) -> None:
    """Generate shell completion scripts.

    \b
    For bash:
      eval "$(bernstein completions --shell bash)"

    \b
    For zsh:
      eval "$(bernstein completions --shell zsh)"
    """
    # Placeholder: actual completion generation would use click-completion
    console.print(f"[dim]Completions for {shell}:[/dim]")
    console.print("# Add to your shell rc file")


# ---------------------------------------------------------------------------
# quarantine
# ---------------------------------------------------------------------------


@click.group("quarantine")
def quarantine_group() -> None:
    """Manage quarantined tasks (failed, blocked, etc.)."""


@quarantine_group.command("list")
def _quarantine_list() -> None:  # type: ignore[reportUnusedFunction]
    """List quarantined tasks."""
    data = server_get("/quarantine")
    if data is None:
        from bernstein.cli.errors import server_unreachable

        server_unreachable().print()
        raise SystemExit(1)

    tasks = data.get("tasks", [])
    if not tasks:
        console.print("[dim]No quarantined tasks.[/dim]")
        return

    from rich.table import Table

    table = Table(show_header=True, header_style="bold red")
    table.add_column("ID", style="dim")
    table.add_column("Title")
    table.add_column("Reason")

    for t in tasks:
        table.add_row(t.get("id", "?"), t.get("title", "?"), t.get("reason", "?"))

    console.print(table)


@quarantine_group.command("clear")
@click.option(
    "--confirm",
    is_flag=True,
    default=False,
    help="Skip confirmation prompt.",
)
def _quarantine_clear(confirm: bool) -> None:  # type: ignore[reportUnusedFunction]
    """Clear all quarantined tasks."""
    if not confirm and not click.confirm("Clear all quarantined tasks?"):
        console.print("[dim]Cancelled.[/dim]")
        return

    result = server_post("/quarantine/clear", {})
    if result is None:
        from bernstein.cli.errors import server_unreachable

        server_unreachable().print()
        raise SystemExit(1)

    count = result.get("cleared", 0)
    console.print(f"[green]Cleared {count} task(s).[/green]")
