"""CLI command group: ``bernstein logs`` — tail and search agent logs.

Subcommands:
  tail    Tail a single agent log file in real time or print recent lines.
  search  Search across all agent/orchestrator/MCP logs with filtering.
"""

from __future__ import annotations

import time
from pathlib import Path

import click

from bernstein.cli.helpers import console

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_agent_logs(runtime_dir: Path, agent_id: str | None) -> list[Path]:
    """Return agent log files from runtime_dir sorted by mtime.

    Args:
        runtime_dir: Directory to scan for ``*.log`` files.
        agent_id: Optional partial session ID to filter by.

    Returns:
        Sorted list of matching log file paths.
    """
    if not runtime_dir.exists():
        return []
    log_list = [p for p in runtime_dir.glob("*.log") if p.name != "watchdog.log"]
    if agent_id:
        log_list = [p for p in log_list if agent_id in p.stem]
    return sorted(log_list, key=lambda p: p.stat().st_mtime)


# ---------------------------------------------------------------------------
# Group
# ---------------------------------------------------------------------------


@click.group("logs")
def logs_group() -> None:
    """Tail and search agent log output.

    \b
    Examples:
      bernstein logs tail                        # last 50 lines of most recent log
      bernstein logs tail -f                     # follow in real time
      bernstein logs search "TypeError"          # search all logs for TypeError
      bernstein logs search "rate limit" \\
          --time-range "last 1h" \\
          --agent-role backend                   # filtered search
    """


# ---------------------------------------------------------------------------
# Subcommand: tail
# ---------------------------------------------------------------------------


@logs_group.command("tail")
@click.option("--follow", "-f", is_flag=True, default=False, help="Stream log output in real-time.")
@click.option("--agent", "-a", default=None, help="Filter by agent session ID (partial match).")
@click.option("--lines", "-n", default=50, show_default=True, help="Lines to show without --follow.")
@click.option(
    "--runtime-dir",
    default=".sdd/runtime",
    show_default=True,
    hidden=True,
    help="Directory containing agent log files.",
)
def logs_tail(follow: bool, agent: str | None, lines: int, runtime_dir: str) -> None:
    """Tail agent log output.

    Without --follow, prints the last N lines of the most recent agent log.
    With --follow (-f), streams new output in real-time until Ctrl+C.
    """
    rdir = Path(runtime_dir)
    log_files = _find_agent_logs(rdir, agent)

    if not log_files:
        suffix = f" matching '{agent}'" if agent else ""
        console.print(f"[yellow]No agent logs found{suffix} in {rdir}[/yellow]")
        raise SystemExit(1)

    log_path = log_files[-1]
    console.print(f"[dim]Watching:[/dim] [bold]{log_path.name}[/bold]")

    if not follow:
        text = log_path.read_text(errors="replace")
        tail_lines = text.splitlines()[-lines:]
        console.print("\n".join(tail_lines) or "[dim](empty)[/dim]")
        return

    try:
        existing = log_path.read_text(errors="replace")
        context = existing.splitlines()[-lines:]
        if context:
            console.print("\n".join(context))
        offset = log_path.stat().st_size
    except FileNotFoundError:
        offset = 0

    console.print("[dim]--- following (Ctrl+C to stop) ---[/dim]")
    try:
        while True:
            try:
                size = log_path.stat().st_size
            except FileNotFoundError:
                time.sleep(0.2)
                continue

            if size > offset:
                with log_path.open("rb") as fh:
                    fh.seek(offset)
                    new_bytes = fh.read(size - offset)
                offset = size
                console.print(new_bytes.decode(errors="replace"), end="")

            time.sleep(0.2)
    except KeyboardInterrupt:
        console.print("\n[dim]stopped.[/dim]")


# ---------------------------------------------------------------------------
# Subcommand: search
# ---------------------------------------------------------------------------


@logs_group.command("search")
@click.argument("query")
@click.option(
    "--time-range",
    default="",
    help="Relative time window, e.g. 'last 1h', 'last 30m', 'last 2d'.",
)
@click.option(
    "--agent-role",
    default="",
    help="Filter by agent role label (partial, case-insensitive).",
)
@click.option(
    "--level",
    type=click.Choice(["error", "warning", "info", "debug"]),
    default=None,
    help="Filter by log level.",
)
@click.option(
    "--limit",
    default=50,
    show_default=True,
    help="Maximum number of results to display.",
)
@click.option(
    "--workdir",
    default=".",
    type=click.Path(exists=True),
    help="Project root directory.",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output results as JSON.")
def logs_search(
    query: str,
    time_range: str,
    agent_role: str,
    level: str | None,
    limit: int,
    workdir: str,
    as_json: bool,
) -> None:
    """Search logs across all agent sessions, the orchestrator, and MCP servers.

    \b
    QUERY is a case-insensitive substring to match against log lines.

    \b
    Examples:
      bernstein logs search "TypeError"
      bernstein logs search "rate limit" --time-range "last 1h"
      bernstein logs search "FAILED" --agent-role backend --level error
      bernstein logs search "import" --limit 20 --json
    """
    import json as _json

    from bernstein.core.log_search import LogSearchIndex

    workdir_path = Path(workdir).resolve()
    index = LogSearchIndex(workdir_path)

    result = index.search(
        query,
        time_range=time_range,
        agent_role=agent_role,
        level=level or "",
        limit=limit,
    )

    if as_json:
        import dataclasses

        console.print(
            _json.dumps(
                {
                    "query": result.query,
                    "total_scanned": result.total_scanned,
                    "matches": len(result.entries),
                    "entries": [dataclasses.asdict(e) for e in result.entries],
                },
                indent=2,
            )
        )
        return

    if not result.entries:
        console.print(
            f"[yellow]No results[/yellow] for [bold]{query!r}[/bold] (scanned {result.total_scanned:,} lines)."
        )
        return

    import datetime

    console.print(
        f"[bold]{len(result.entries)}[/bold] result(s) for [bold]{query!r}[/bold] "
        f"— {result.total_scanned:,} lines scanned\n"
    )

    _LEVEL_COLOR: dict[str, str] = {
        "error": "red",
        "warning": "yellow",
        "info": "dim",
        "debug": "dim",
    }

    for entry in result.entries:
        if entry.timestamp > 0:
            ts_str = datetime.datetime.fromtimestamp(entry.timestamp).strftime("%Y-%m-%d %H:%M:%S")
        else:
            ts_str = "               "

        color = _LEVEL_COLOR.get(entry.level, "")
        role_tag = f"[{entry.agent_role}] " if entry.agent_role else ""
        source_short = Path(entry.source).name

        # Highlight query in message
        msg = entry.message
        if query:
            msg = msg.replace(query, f"[bold]{query}[/bold]")

        level_badge = f"[{color}]{entry.level.upper():<7}[/{color}]" if color else entry.level.upper()
        console.print(f"[dim]{ts_str}[/dim] {level_badge} [cyan]{source_short}[/cyan] {role_tag}{msg}")
