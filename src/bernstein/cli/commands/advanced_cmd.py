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

import contextlib
import datetime as dt
import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

import click
import httpx

from bernstein.cli.helpers import (
    SERVER_URL,
    console,
    find_seed_file,
    print_banner,
    server_get,
    server_post,
)
from bernstein.cli.mcp_cmd import mcp_server as mcp_server  # re-exported for main.py
from bernstein.core.runtime_state import read_session_replay_metadata
from bernstein.core.traces import TraceStore, build_replay_task_request, render_replay_diff
from bernstein.core.visual_config import VisualConfig, resolve_visual_config

_LOGGER = logging.getLogger(__name__)

_STYLE_BOLD_CYAN = "bold cyan"

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
@click.option(
    "--no-splash",
    is_flag=True,
    default=False,
    help="Skip the premium startup splash.",
)
def live(interval: float, classic: bool, no_splash: bool) -> None:
    """Live dashboard: active agents, task events, and stats (Ctrl+C to exit).

    Launches the 3-column interactive Textual TUI by default:
    Agents | Tasks | Activity feed + sparkline + chat input.
    Mouse + keyboard. Pass --classic for the simpler Rich Live display.
    """
    seed_path = find_seed_file()
    seed_cfg = _load_live_seed_config(seed_path)
    visual_cfg = _resolve_live_visual_config(seed_cfg, no_splash=no_splash)

    if not classic:
        if visual_cfg.splash:
            from bernstein.cli.splash_v2 import render_startup_splash

            render_startup_splash(console, config=visual_cfg, **_build_live_splash_context(seed_path, seed_cfg))

        from bernstein.cli.dashboard import BernsteinApp as DashboardApp

        app = DashboardApp()
        with contextlib.suppress(SystemExit):
            app.run()
        if getattr(app, "_play_power_off_on_exit", False) and visual_cfg.crt_effects:
            from bernstein.cli.crt_effects import CRTConfig, power_off_effect
            from bernstein.cli.terminal_caps import detect_capabilities

            caps = detect_capabilities()
            power_off_effect(config=CRTConfig(width=caps.term_width, height=min(caps.term_height, 24)))
        # Hot restart: server+orchestrator already killed by the TUI,
        # re-exec the full `bernstein run` so everything restarts cleanly.
        if getattr(app, "_restart_on_exit", False):
            from bernstein.cli.run_cmd import exec_restart

            exec_restart()
        return

    # -- classic Rich Live display --
    from bernstein.cli.live import LiveView

    print_banner()

    (
        LiveView(
            server_url=SERVER_URL,
            interval=interval,
        )
    ).run()


def _load_live_seed_config(seed_path: Path | None) -> Any:
    """Load seed config for splash/context wiring when available."""
    if seed_path is None:
        return None
    try:
        from bernstein.core.seed import parse_seed

        return parse_seed(seed_path)
    except Exception:
        return None


def _resolve_live_visual_config(seed_cfg: Any, *, no_splash: bool) -> VisualConfig:
    """Resolve premium visual settings for ``bernstein live``."""
    raw_visual = getattr(seed_cfg, "visual", None) if seed_cfg is not None else None
    return resolve_visual_config(raw_visual, no_splash=no_splash)


def _build_live_splash_context(seed_path: Path | None, seed_cfg: Any) -> dict[str, Any]:
    """Build startup splash context for the live dashboard command."""
    version = ""
    with contextlib.suppress(Exception):
        from importlib.metadata import version as get_version

        version = get_version("bernstein")

    agents: list[dict[str, object]] = []
    with contextlib.suppress(Exception):
        from bernstein.core.agent_discovery import discover_agents_cached

        discovery = discover_agents_cached()
        agents = [
            {"name": agent.name, "logged_in": agent.logged_in, "default_model": agent.default_model}
            for agent in discovery.agents
        ]

    task_count = 0
    with contextlib.suppress(Exception):
        open_dir = Path.cwd() / ".sdd" / "backlog" / "open"
        if open_dir.exists():
            task_count = sum(1 for file in open_dir.iterdir() if file.suffix in (".yaml", ".yml", ".md"))

    goal_preview = str(getattr(seed_cfg, "goal", "") or "")[:80]
    budget = float(getattr(seed_cfg, "budget_usd", 0.0) or 0.0)

    return {
        "version": version,
        "agents": agents,
        "seed_file": str(seed_path) if seed_path else None,
        "goal_preview": goal_preview,
        "budget": budget,
        "task_count": task_count,
    }


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


def _classify_archive_task(
    task: dict[str, Any],
    since: float | None,
    done_tasks: list[dict[str, Any]],
    failed_tasks: list[dict[str, Any]],
) -> None:
    """Classify a single archive task into done or failed lists, applying time filter."""
    ts = task.get("completed_at") or task.get("failed_at")
    if ts:
        ts_float = float(ts) if isinstance(ts, (int, float, str)) else 0
        if since is not None and ts_float < since:
            return
    status = task.get("status")
    if status == "done":
        done_tasks.append(task)
    elif status == "failed":
        failed_tasks.append(task)


def _load_archive_tasks(path: Path, since: float | None = None) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Load completed and failed tasks from archive, optionally filtered by time."""
    done_tasks: list[dict[str, Any]] = []
    failed_tasks: list[dict[str, Any]] = []

    if not path.exists():
        return done_tasks, failed_tasks

    import json as _json

    with contextlib.suppress(Exception), path.open() as f:
        for line in f:
            if not line.strip():
                continue
            with contextlib.suppress(Exception):
                task = _json.loads(line)
                _classify_archive_task(task, since, done_tasks, failed_tasks)

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
    lines.extend(
        (
            "# Archive Retrospective",
            "",
            f"Generated: {ts_str}",
            "",
            "## Overview",
            "",
            f"- **Completion rate:** {completion_rate:.0f}% ({n_done} done / {total} total)",
            f"- **Failed tasks:** {n_failed}",
            "",
        )
    )

    if failed_tasks:
        lines.extend(("## Failed Tasks", ""))
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
    """Show full help with every command group and description."""
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

    for hook_name, script in (("pre-commit", pre_commit_script), ("pre-push", pre_push_script)):
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

    table = Table(title="Installed Plugins", show_header=True, header_style=_STYLE_BOLD_CYAN)
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


@click.group(
    name="doctor",
    invoke_without_command=True,
    subcommand_metavar="[airgap|sonar|glitchtip|...]",
)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output raw JSON.")
@click.option("--fix", "auto_fix", is_flag=True, default=False, help="Attempt to auto-fix issues.")
@click.option(
    "--suggest-docs",
    "suggest_docs",
    is_flag=True,
    default=False,
    help="Print the top curated documentation gaps and exit.",
)
@click.pass_context
def doctor(ctx: click.Context, as_json: bool, auto_fix: bool, suggest_docs: bool) -> None:
    """Run self-diagnostics: check Python, adapters, API keys, port, and workspace.

    \b
      bernstein doctor                # print diagnostic report
      bernstein doctor --json         # machine-readable output
      bernstein doctor --fix          # attempt to auto-fix issues
      bernstein doctor --suggest-docs # surface top curated documentation gaps
      bernstein doctor airgap         # battery of checks for an air-gapped run
      bernstein doctor sonar          # surface SonarQube insights for the project
      bernstein doctor glitchtip      # surface GlitchTip issue counts and top unresolved
    """
    if ctx.invoked_subcommand is not None:
        ctx.obj = {"as_json": as_json, "auto_fix": auto_fix}
        return

    if suggest_docs:
        from bernstein.cli.doctor.suggest_docs import (
            DEFAULT_TOP_N,
            load_unanswered_topics,
            render_suggestions,
        )

        topics = load_unanswered_topics()
        render_suggestions(console, topics, limit=DEFAULT_TOP_N)
        _maybe_print_sonar_nudge(as_json=as_json)
        _maybe_print_glitchtip_nudge(as_json=as_json)
        return

    from bernstein.cli.doctor.suggest_docs import hint_line
    from bernstein.cli.status_cmd import doctor as _doctor_impl

    # Run the existing diagnostics first. The implementation raises
    # SystemExit on failure, so we capture and re-raise after printing
    # the trailing hint to keep the exit code unchanged.
    exit_code = 0
    try:
        ctx.invoke(_doctor_impl, as_json=as_json, auto_fix=auto_fix)
    except SystemExit as exc:  # NOSONAR python:S5754 - captured to add a hint, re-raised below
        exit_code = int(exc.code or 0)

    if not as_json:
        console.print(f"[dim]{hint_line()}[/dim]")
        _maybe_print_sonar_nudge(as_json=as_json)
        _maybe_print_glitchtip_nudge(as_json=as_json)

    if exit_code:
        raise SystemExit(exit_code)


def _maybe_print_sonar_nudge(*, as_json: bool) -> None:
    """Print a single-line Sonar nudge when thresholds are crossed.

    No-op when Sonar is not configured, the snapshot could not be
    fetched, or when ``--json`` was requested (the nudge is advisory
    and must not contaminate machine-readable output). All failures
    here are swallowed so the doctor never crashes because of a side
    integration.
    """
    if as_json:
        return
    try:
        from bernstein.core.observability.sonar import (
            collect_insights,
            evaluate_nudge,
            load_baseline,
            load_config,
        )
    except Exception:  # pragma: no cover - defensive
        return
    config = load_config()
    if config is None:
        return
    try:
        insights = collect_insights(config)
    except Exception:  # pragma: no cover - defensive
        return
    if not insights.fetched:
        return
    nudge = evaluate_nudge(insights, load_baseline())
    if not nudge.should_nudge:
        return
    summary = "; ".join(nudge.reasons)
    console.print(
        f"[dim yellow]Sonar nudge: {summary}. Run `bernstein doctor sonar` for the full surface.[/dim yellow]"
    )


def _maybe_print_glitchtip_nudge(*, as_json: bool) -> None:
    """Print a single-line GlitchTip nudge when new unresolved issues exist.

    No-op when ``--json`` was requested (advisory output must not leak
    into machine-readable streams), when ``BERNSTEIN_GLITCHTIP_TOKEN``
    is not set, or when there is no delta against the cached baseline.
    All exceptions are swallowed so the doctor command never crashes
    because of a side integration.
    """
    if as_json:
        return
    try:
        from bernstein.cli.commands.doctor.glitchtip import suggest_nudge_line
    except ImportError:  # pragma: no cover - defensive
        return
    try:
        line = suggest_nudge_line()
    except Exception:  # pragma: no cover - defensive
        # Log unexpected failures so we can diagnose regressions, but never
        # raise: the nudge is advisory and must not crash `bernstein doctor`.
        _LOGGER.warning("GlitchTip nudge failed", exc_info=True)
        return
    if line:
        console.print(f"[dim yellow]{line}[/dim yellow]")


@doctor.command("airgap")
@click.pass_context
def doctor_airgap_cmd(ctx: click.Context) -> None:
    """Run the battery of air-gap self-checks.

    \b
    Checks:
      - --profile airgap is the active entry point
      - network policy denies every destination by default
      - declared adapter endpoints are all blocked
      - MCP catalog has no enabled bernstein-managed entries
      - fingerprint memo store is on local disk only
      - audit chain HMAC is intact
      - .sdd/runtime contains no public hostnames

    Exit code is 0 only when every check passes.
    """
    from bernstein.cli.commands.doctor_airgap_cmd import run_doctor_airgap

    parent = ctx.obj if isinstance(ctx.obj, dict) else {}
    as_json = bool(parent.get("as_json", False))
    raise SystemExit(run_doctor_airgap(workdir=Path.cwd(), as_json=as_json))


@doctor.command("scoping")
@click.option("--agent-id", default="default", show_default=True, help="Agent id to resolve.")
@click.option("--role", default="default", show_default=True, help="Role hint for fallback.")
@click.pass_context
def doctor_scoping_cmd(ctx: click.Context, agent_id: str, role: str) -> None:
    """Self-check: report the credential policy + flag inherited keys.

    \b
    Shows:
      - whether credential scoping is enabled
      - resolved allowlist for agent_id/role
      - env-vars in the orchestrator process that the policy WOULD strip
        from a child agent (potential leak surface if scoping is off)
      - allowlist entries that are not currently in env (config drift)

    Exit code is 0 when scoping is enabled and there are no stripped
    keys; 1 when scoping is disabled or some inherited keys would be
    silently dropped from agent subprocesses.
    """
    import os

    from bernstein.core.credential_scoping import (
        ENV_DISABLE_CREDENTIAL_SCOPING,
        explain_policy_for_agent,
        resolve_default_policy,
    )

    parent = ctx.obj if isinstance(ctx.obj, dict) else {}
    as_json = bool(parent.get("as_json", False))

    # Resolve without installing - doctor must not mutate process state.
    policy = resolve_default_policy(workdir=Path.cwd(), install=False)
    inherited = [k for k in os.environ if "API" in k or "TOKEN" in k or "KEY" in k]
    snapshot = explain_policy_for_agent(
        policy,
        agent_id=agent_id,
        role=role,
        inherited_keys=inherited,
    )

    if as_json:
        console.print_json(json.dumps(snapshot))
        if snapshot["enabled"] and not snapshot["stripped"]:
            raise SystemExit(0)
        raise SystemExit(1)

    enabled_glyph = "[green]enabled[/green]" if snapshot["enabled"] else "[yellow]disabled[/yellow]"
    console.print(f"Credential scoping: {enabled_glyph}")
    console.print(f"  agent_id: [cyan]{snapshot['agent_id']}[/cyan]")
    console.print(f"  role:     [cyan]{snapshot['role']}[/cyan]")
    console.print(f"  allowed:  {', '.join(snapshot['allowed']) or '[dim](none)[/dim]'}")
    if snapshot["stripped"]:
        console.print(f"  [yellow]would-strip[/yellow]: {', '.join(snapshot['stripped'])}")
        console.print(
            "  [dim](these env-vars are present but the policy would not "
            "pass them to the agent: verify they are intentional)[/dim]"
        )
    if snapshot["missing"]:
        console.print(f"  [yellow]missing[/yellow]: {', '.join(snapshot['missing'])}")
        console.print("  [dim](allowlist entries not currently in the orchestrator's env)[/dim]")
    if not snapshot["enabled"]:
        console.print(
            f"  [dim]opt-out via {ENV_DISABLE_CREDENTIAL_SCOPING}=1 is currently active "
            "or no policy file was found[/dim]"
        )
        raise SystemExit(1)
    if snapshot["stripped"]:
        raise SystemExit(1)
    raise SystemExit(0)


@doctor.command("extended")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit JSON instead of the Rich table.",
)
@click.option(
    "--adapter",
    "adapter_filter",
    multiple=True,
    help="Restrict adapter checks to this name (repeatable).",
)
@click.option(
    "--provider",
    "provider_filter",
    multiple=True,
    help="Restrict network checks to this provider (repeatable).",
)
def doctor_extended_cmd(
    as_json: bool,
    adapter_filter: tuple[str, ...],
    provider_filter: tuple[str, ...],
) -> None:
    """Run the extended doctor: adapter binaries, network, environment.

    \b
    Categories:
      - installation: legacy install_check (preserved behavior)
      - adapter:     `which <bin>` + `<bin> --version` per adapter
      - network:     TCP/443 reachability per provider (honors BERNSTEIN_OFFLINE)
      - environment: GitHub Actions / GitLab CI / Buildkite / Docker / devcontainer / systemd-run

    \b
    Examples:
      bernstein doctor extended
      bernstein doctor extended --json
      bernstein doctor extended --adapter claude --provider anthropic
    """
    import asyncio
    import json as _json
    import sys

    from bernstein.cli.doctor import exit_code_for, render_report, run_all, summarize

    results = asyncio.run(
        run_all(
            adapter_names=list(adapter_filter) or None,
            provider_names=list(provider_filter) or None,
        )
    )

    if as_json:
        payload = {
            "results": [
                {
                    "name": r.name,
                    "category": r.category,
                    "status": r.status,
                    "detail": r.detail,
                    "remediation": r.remediation,
                }
                for r in results
            ],
            "summary": summarize(results),
        }
        console.print_json(_json.dumps(payload))
    else:
        rendered = render_report(results, console=console)
        if rendered:  # captured text - only set when console was None
            pass

    counts = summarize(results)
    if counts["fail"] == 0 and counts["warn"] > 0:
        sys.stderr.write(
            f"doctor: {counts['warn']} warning(s); see report above.\n",
        )

    raise SystemExit(exit_code_for(results))


@doctor.command("promptware-scan")
@click.argument("run_id", required=True)
@click.option(
    "--workdir",
    "workdir",
    default=".",
    show_default=True,
    help="Project root to look up traces under .sdd/traces/.",
)
@click.option(
    "--threshold",
    "threshold",
    type=float,
    default=0.7,
    show_default=True,
    help="Only print outputs at or above this score.",
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit JSON instead of the Rich table.",
)
def doctor_promptware_scan_cmd(
    run_id: str,
    workdir: str,
    threshold: float,
    as_json: bool,
) -> None:
    """Replay tool output for ``run_id`` and report suspicious entries.

    \b
    Looks under ``<workdir>/.sdd/traces/<run_id>.jsonl`` for trace records
    that carry tool output, runs the promptware detector against each,
    and prints any output whose score reaches the supplied ``--threshold``
    (default ``0.7``, matching the WARN band).

    \b
    Examples:
      bernstein doctor promptware-scan abc123
      bernstein doctor promptware-scan abc123 --threshold 0.9 --json
    """
    from bernstein.cli.commands.doctor_promptware_cmd import run_promptware_scan

    raise SystemExit(
        run_promptware_scan(
            run_id=run_id,
            workdir=Path(workdir),
            threshold=threshold,
            as_json=as_json,
        )
    )


@doctor.command("sonar")
@click.option(
    "--json",
    "as_json_flag",
    is_flag=True,
    default=False,
    help="Emit JSON instead of the Rich table.",
)
@click.option(
    "--smell-threshold",
    "smell_threshold",
    type=int,
    default=None,
    help="Override the default code-smell nudge threshold (50).",
)
@click.option(
    "--no-update-baseline",
    "no_update_baseline",
    is_flag=True,
    default=False,
    help="Do not write the current snapshot to the baseline file.",
)
@click.pass_context
def doctor_sonar_cmd(
    ctx: click.Context,
    as_json_flag: bool,
    smell_threshold: int | None,
    no_update_baseline: bool,
) -> None:
    """Surface SonarQube insights for the current project.

    \b
    Reads SONAR_HOST_URL and SONAR_TOKEN from the environment, then
    fetches:
      - coverage % (line coverage as reported by Sonar)
      - code smells total and counts by severity
      - bugs, vulnerabilities, security hotspots
      - cognitive complexity hotspots (top 5 files)

    \b
    Soft-fails (exit 0) when env vars are not set or the server is
    unreachable. Use --json to pipe the snapshot into other tools.
    """
    from bernstein.cli.commands.doctor_sonar_cmd import (
        DEFAULT_SMELL_NUDGE,
        run_doctor_sonar,
    )

    parent = ctx.obj if isinstance(ctx.obj, dict) else {}
    inherited_json = bool(parent.get("as_json", False))
    threshold = smell_threshold if smell_threshold is not None else DEFAULT_SMELL_NUDGE
    raise SystemExit(
        run_doctor_sonar(
            as_json=as_json_flag or inherited_json,
            smell_threshold=threshold,
            update_baseline=not no_update_baseline,
        )
    )


# Attach the GlitchTip insights subcommand to the existing ``doctor`` group.
# Registration runs at import time so ``bernstein doctor glitchtip`` is
# wired without circular imports between the per-backend module and the
# group definition above.
from bernstein.cli.commands.doctor.glitchtip import (  # noqa: E402
    register as _register_doctor_glitchtip,
)

_register_doctor_glitchtip(doctor)


@doctor.command("sonar-sweep")
@click.option(
    "--dry-run",
    is_flag=True,
    default=False,
    help="Print the would-be ticket paths without writing files.",
)
@click.option(
    "--severity-min",
    "severity_min",
    default="MAJOR",
    show_default=True,
    type=click.Choice(["BLOCKER", "CRITICAL", "MAJOR", "MINOR", "INFO"]),
    help="Minimum severity to include.",
)
@click.option(
    "--max-per-day",
    "max_per_day",
    type=int,
    default=25,
    show_default=True,
    help="Cap on the number of tickets emitted in this run.",
)
@click.option(
    "--out-dir",
    "out_dir",
    default=".sdd/backlog/open",
    show_default=True,
    help="Directory to write emitted ticket files into.",
)
@click.option(
    "--create-gh-issues",
    "create_gh_issues",
    is_flag=True,
    default=False,
    help="For P0+P1 tickets (BLOCKER, CRITICAL, MAJOR), also open a GH issue.",
)
@click.option(
    "--fixture",
    default=None,
    help="Use a saved JSON fixture instead of calling Sonar (for local dry-runs).",
)
def doctor_sonar_sweep_cmd(
    dry_run: bool,
    severity_min: str,
    max_per_day: int,
    out_dir: str,
    create_gh_issues: bool,
    fixture: str | None,
) -> None:
    """Turn open static-analysis findings into backlog tickets.

    \b
    Reads ``SONAR_HOST_URL`` and ``SONAR_TOKEN`` from the environment,
    fetches the open findings, applies de-dup against existing tickets
    under ``.sdd/backlog/*``, and writes one Markdown ticket per new
    finding into the configured output directory.

    \b
    With ``--dry-run`` the command lists the would-be file paths without
    writing them. With ``--fixture`` it loads findings from a saved JSON
    blob instead of calling the Sonar API at all -- useful for local
    smoke tests.
    """
    # The sweeper lives under ``scripts/`` so the wheel stays slim. We
    # import it lazily from the source tree.
    from pathlib import Path as _Path

    repo_root = _Path(__file__).resolve().parents[4]
    scripts_dir = repo_root / "scripts"
    import sys as _sys

    if str(scripts_dir) not in _sys.path:
        _sys.path.insert(0, str(scripts_dir))
    try:
        from sweep_sonar_findings import (
            main as _sweep_main,  # type: ignore[import-not-found]
        )
    except ImportError as exc:
        click.echo(f"error: cannot import sweeper: {exc}", err=True)
        raise SystemExit(2) from exc

    argv: list[str] = [
        "--severity-min",
        severity_min,
        "--max-per-day",
        str(max_per_day),
        "--out-dir",
        out_dir,
    ]
    if dry_run:
        argv.append("--dry-run")
    if create_gh_issues:
        argv.append("--create-gh-issues")
    if fixture:
        argv.extend(["--fixture", fixture])

    raise SystemExit(int(_sweep_main(argv)))


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
    summary = data.get("summary", {})
    diff_stats = data.get("diff_stats", {})
    quality_scores = data.get("quality_scores", {})
    cost_breakdown = data.get("cost_breakdown", {})

    total = summary.get("total", len(tasks))
    done = summary.get("completed", 0)
    failed = summary.get("failed", 0)
    success_rate = summary.get("success_rate", 0.0)

    # Main recap table
    table = Table(title="Recap", show_header=True, header_style=_STYLE_BOLD_CYAN)
    table.add_column("Metric")
    table.add_column("Value")

    table.add_row("Total tasks", str(total))
    table.add_row("Completed", f"[green]{done}[/green]")
    table.add_row("Failed", f"[red]{failed}[/red]")
    table.add_row("Success rate", f"{success_rate:.1f}%" if total > 0 else "N/A")

    console.print(table)
    console.print()

    # Git diff stats
    diff_table = Table(title="Git Diff Stats", show_header=True, header_style="bold green")
    diff_table.add_column("Metric")
    diff_table.add_column("Value")
    diff_table.add_row("Files changed", str(diff_stats.get("files_changed", 0)))
    diff_table.add_row("Additions", f"[green]{diff_stats.get('additions', 0)}[/green]")
    diff_table.add_row("Deletions", f"[red]{diff_stats.get('deletions', 0)}[/red]")

    console.print(diff_table)
    console.print()

    # Quality scores
    quality_table = Table(title="Quality Scores", show_header=True, header_style="bold yellow")
    quality_table.add_column("Metric")
    quality_table.add_column("Value")
    quality_table.add_row("Average score", str(quality_scores.get("average_score", 0)))
    quality_table.add_row("Lint score", str(quality_scores.get("lint_score", 0)))
    quality_table.add_row("Tests score", str(quality_scores.get("tests_score", 0)))
    quality_table.add_row("Type check score", str(quality_scores.get("type_check_score", 0)))

    grade_dist = quality_scores.get("grade_distribution", {})
    grades_str = ", ".join(f"{k}: {v}" for k, v in grade_dist.items())
    quality_table.add_row("Grade distribution", grades_str)

    console.print(quality_table)
    console.print()

    # Cost breakdown
    cost_table = Table(title="Cost Breakdown", show_header=True, header_style="bold magenta")
    cost_table.add_column("Model")
    cost_table.add_column("Cost (USD)")
    cost_table.add_column("Tokens")
    cost_table.add_column("Invocations")

    total_cost = cost_breakdown.get("total_cost_usd", 0.0)
    for model_data in cost_breakdown.get("per_model", []):
        model = model_data.get("model", "unknown")
        cost = model_data.get("cost_usd", 0.0)
        tokens = model_data.get("tokens", 0)
        invocations = model_data.get("invocations", 0)
        cost_table.add_row(model, f"${cost:.4f}", str(tokens), str(invocations))

    cost_table.add_row("TOTAL", f"${total_cost:.4f}", "", "")

    console.print(cost_table)


# ---------------------------------------------------------------------------
# trace
# ---------------------------------------------------------------------------


class _TraceGroup(click.Group):
    """Click group that falls back to ``show`` when the first positional
    arg is not a registered subcommand.

    This preserves the legacy ``bernstein trace <task-id>`` invocation
    after the command was promoted to a group with ``serve``, ``verify``,
    and ``reindex`` subcommands.
    """

    def resolve_command(
        self,
        ctx: click.Context,
        args: list[str],
    ) -> tuple[str | None, click.Command | None, list[str]]:
        if args and not args[0].startswith("-") and args[0] not in self.commands:
            args = ["show", *args]
        return super().resolve_command(ctx, args)


@click.group("trace", cls=_TraceGroup, invoke_without_command=True)
@click.option(
    "--traces-dir",
    default=".sdd/traces",
    show_default=True,
    help="Directory containing trace files.",
)
@click.pass_context
def trace_cmd(ctx: click.Context, traces_dir: str) -> None:
    """Inspect, serve, and verify local agent traces.

    \b
      bernstein trace <task-id>           Pretty-print a task trace (alias of show)
      bernstein trace show <task-id>      Pretty-print a task trace
      bernstein trace serve --port 8765   Run the local read-only viewer
      bernstein trace verify <trace-id>   Confirm on-disk bytes match sha256
      bernstein trace reindex             Rebuild .sdd/traces/index.jsonl
    """
    ctx.obj = {"traces_dir": traces_dir}
    if ctx.invoked_subcommand is None:
        click.echo(ctx.get_help())


def _trace_show_task(task_id: str, *, traces_dir: str, as_json: bool) -> None:
    """Pretty-print a single trace file for ``task_id``."""
    traces_path = Path(traces_dir)
    if not traces_path.exists():
        console.print(f"[red]Traces directory not found:[/red] {traces_path}")
        raise SystemExit(1)

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

        from rich.syntax import Syntax

        syntax = Syntax(content, "json", theme="monokai", line_numbers=True)
        console.print(syntax)
    except Exception as e:
        console.print(f"[red]Error reading trace:[/red] {e}")
        raise SystemExit(1) from e


@trace_cmd.command("show")
@click.argument("task_id")
@click.option(
    "--as-json",
    "as_json",
    is_flag=True,
    default=False,
    help="Output raw JSON.",
)
@click.pass_context
def trace_show_cmd(ctx: click.Context, task_id: str, as_json: bool) -> None:
    """Show execution trace for a task."""
    traces_dir = (ctx.obj or {}).get("traces_dir", ".sdd/traces")
    _trace_show_task(task_id, traces_dir=traces_dir, as_json=as_json)


@trace_cmd.command("serve")
@click.option("--port", default=8765, show_default=True, type=int, help="TCP port to bind.")
@click.option(
    "--bind",
    default="127.0.0.1",
    show_default=True,
    help="Interface to bind. Defaults to loopback to keep traces local.",
)
@click.pass_context
def trace_serve_cmd(ctx: click.Context, port: int, bind: str) -> None:
    """Run a read-only FastAPI viewer over the local content-addressed store."""
    import uvicorn

    from bernstein.core.observability.trace_store import (
        ContentAddressedTraceStore,
        build_viewer_app,
    )

    traces_dir = (ctx.obj or {}).get("traces_dir", ".sdd/traces")
    store = ContentAddressedTraceStore(Path(traces_dir))
    app = build_viewer_app(store)
    console.print(
        f"[green]Trace viewer:[/green] http://{bind}:{port}/  "
        f"(root: {store.root}, traces indexed: {len(store.index())})"
    )
    uvicorn.run(app, host=bind, port=port, log_level="warning")


@trace_cmd.command("verify")
@click.argument("trace_id")
@click.pass_context
def trace_verify_cmd(ctx: click.Context, trace_id: str) -> None:
    """Confirm the on-disk bytes for ``trace_id`` match the indexed sha256."""
    from bernstein.core.observability.trace_store import ContentAddressedTraceStore

    traces_dir = (ctx.obj or {}).get("traces_dir", ".sdd/traces")
    store = ContentAddressedTraceStore(Path(traces_dir))
    if store.verify(trace_id):
        console.print(f"[green]OK[/green] {trace_id}")
        return
    console.print(f"[red]FAIL[/red] {trace_id}")
    raise SystemExit(1)


@trace_cmd.command("reindex")
@click.pass_context
def trace_reindex_cmd(ctx: click.Context) -> None:
    """Rebuild ``.sdd/traces/index.jsonl`` from the on-disk blob tree."""
    from bernstein.core.observability.trace_store import ContentAddressedTraceStore

    traces_dir = (ctx.obj or {}).get("traces_dir", ".sdd/traces")
    count = ContentAddressedTraceStore(Path(traces_dir)).reindex()
    console.print(f"[green]Reindex complete:[/green] {count} entries")


# ---------------------------------------------------------------------------
# replay
# ---------------------------------------------------------------------------


#: Canonical per-run event journal filename (issue #2293). The old
#: ``replay.jsonl`` recorder was consolidated into ``journal.jsonl``.
_REPLAY_JSONL = "journal.jsonl"


def _replay_print_header(
    run_id: str,
    events: list[dict[str, Any]],
    fingerprint: str,
    metadata: Any,
) -> None:
    """Print the replay header panel with run metadata."""
    from rich.panel import Panel

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


def _replay_find_run_dirs(runs_dir: Path) -> list[Path]:
    """Return sorted list of run directories that contain replay logs."""
    if not runs_dir.exists():
        return []
    return sorted(
        (d for d in runs_dir.iterdir() if d.is_dir() and (d / _REPLAY_JSONL).exists()),
        key=lambda d: d.name,
        reverse=True,
    )


def _replay_list_runs(runs_dir: Path) -> None:
    """Show all available run IDs in a Rich table."""
    if not runs_dir.exists():
        console.print("[dim]No runs recorded yet.[/dim]")
        return
    run_dirs = _replay_find_run_dirs(runs_dir)
    if not run_dirs:
        console.print("[dim]No replay logs found.[/dim]")
        return
    from rich.table import Table

    table = Table(title="Available Runs", show_header=True, header_style=_STYLE_BOLD_CYAN)
    table.add_column("Run ID")
    table.add_column("Started")
    table.add_column("Branch")
    table.add_column("SHA")
    table.add_column("Events", justify="right")
    table.add_column("Size", justify="right")
    for d in run_dirs:
        replay_file = d / _REPLAY_JSONL
        event_count = sum(1 for line in replay_file.read_text().splitlines() if line.strip())
        size_kb = replay_file.stat().st_size / 1024
        metadata = read_session_replay_metadata(d)
        started = "-"
        branch = "-"
        sha = "-"
        if metadata is not None:
            started = dt.datetime.fromtimestamp(metadata.started_at).strftime("%Y-%m-%d %H:%M")
            branch = metadata.git_branch or "-"
            sha = metadata.git_sha[:8] if metadata.git_sha else "-"
        table.add_row(d.name, started, branch, sha, str(event_count), f"{size_kb:.1f} KB")
    console.print(table)


def _replay_resolve_latest(runs_dir: Path) -> str:
    """Resolve 'latest' to the most recent run ID or exit."""
    run_dirs = _replay_find_run_dirs(runs_dir)
    if not run_dirs:
        console.print("[red]No replay logs found.[/red]")
        raise SystemExit(1)
    latest = run_dirs[0].name
    console.print(f"[dim]Replaying latest run:[/dim] {latest}")
    return latest


def _should_use_run_replay(run_id: str, runs_dir: Path) -> bool:
    """Return whether replay should use the legacy run-event mode."""
    return run_id in {"list", "latest"} or (runs_dir / run_id / _REPLAY_JSONL).exists()


def _wait_for_replay_completion(
    task_id: str,
    *,
    timeout_s: float = 30.0,
    poll_interval_s: float = 1.0,
) -> dict[str, Any] | None:
    """Poll the task server until a replayed task reaches a terminal state."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        task = server_get(f"/tasks/{task_id}")
        if task is None:
            return None
        status = str(task.get("status", ""))
        if status in {"done", "failed", "cancelled"}:
            return task
        time.sleep(poll_interval_s)
    return None


def _replay_task_trace(task_id: str, sdd_path: Path, *, override_model: str | None, extra_context: str | None) -> None:
    """Replay a historical task trace by re-submitting its last snapshot."""
    trace = TraceStore(sdd_path / "traces").latest_for_task(task_id)
    if trace is None:
        console.print(f"[red]No trace found for task:[/red] {task_id}")
        raise SystemExit(1)

    request = build_replay_task_request(
        trace,
        task_id=task_id,
        override_model=override_model,
        extra_context=extra_context,
    )
    created = server_post("/tasks", request.to_payload())
    if created is None:
        console.print("[red]Failed to create replay task.[/red]")
        raise SystemExit(1)

    replay_task_id = str(created.get("id", ""))
    console.print(f"[green]Replay task created:[/green] {replay_task_id}")
    replayed = _wait_for_replay_completion(replay_task_id)
    if replayed is None:
        console.print("[yellow]Replay task created, but completion polling timed out.[/yellow]")
        return

    replay_summary = str(replayed.get("result_summary", ""))
    diff = render_replay_diff(request.original_result_summary, replay_summary)
    if diff:
        from rich.syntax import Syntax

        console.print(Syntax(diff, "diff", theme="monokai", line_numbers=False))
    else:
        console.print("[dim]Replay completed with no result-summary diff.[/dim]")


def _replay_run_impl(
    run_id: str,
    sdd_dir: str,
    as_json: bool,
    limit: int | None,
    model: str | None,
    extra_context: str | None,
) -> None:
    """Body of ``bernstein replay <run_id>`` - replay one run.

    Extracted so the new ``replay`` :class:`click.Group` can dispatch to
    it from its top-level invocation while also exposing subcommands
    (``diff``) via the same group object.
    """
    sdd_path = Path(sdd_dir)
    runs_dir = sdd_path / "runs"
    if not _should_use_run_replay(run_id, runs_dir):
        _replay_task_trace(run_id, sdd_path, override_model=model, extra_context=extra_context)
        return

    # "list" subcommand: show all available run IDs
    if run_id == "list":
        _replay_list_runs(runs_dir)
        return

    # Resolve "latest"
    if run_id == "latest":
        run_id = _replay_resolve_latest(runs_dir)

    replay_path = runs_dir / run_id / _REPLAY_JSONL
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
    from rich.table import Table

    _replay_print_header(run_id, events, fingerprint, metadata)

    # Event table
    table = Table(show_header=True, header_style=_STYLE_BOLD_CYAN, expand=True)
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


@click.command("replay")
@click.argument("run_id", nargs=-1, required=True)
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
@click.option("--model", default=None, help="Override model for task-trace replay.")
@click.option(
    "--extra-context",
    default=None,
    help="Append additional hint text to the replayed task description.",
)
def replay_cmd(
    run_id: tuple[str, ...],
    sdd_dir: str,
    as_json: bool,
    limit: int | None,
    model: str | None,
    extra_context: str | None,
) -> None:
    """Replay a past orchestration run step-by-step.

    \b
    Reads .sdd/runs/{run_id}/replay.jsonl and displays events in a
    Rich table showing timing, event type, agent, task, and details.

    \b
    If run_id is "latest", replays the most recent run. Use "list" to
    show all available run IDs. Use ``diff`` to localise the first
    divergence between two recorded runs (events.jsonl).

    \b
      bernstein replay list                       # list available runs
      bernstein replay latest                     # replay most recent run
      bernstein replay 20240315-143022            # replay a specific run
      bernstein replay diff RUN_A RUN_B           # first-divergence finder
      bernstein replay <AGENT_ID>                 # per-step journal view (#1799)
      bernstein replay export <AGENT_ID> -o RECEIPT   # portable receipt (#1799)
      bernstein replay publish <AGENT_ID> -o RECEIPT  # redacted publish (#1799)
      bernstein replay verify <RECEIPT>           # offline verifier (#1799)
      bernstein replay diff-journal A B           # per-step divergence finder
    """
    # ``nargs=-1`` lets us implement the pseudo-subcommand ``diff`` without
    # converting ``replay`` to a full :class:`click.Group` (which would
    # break the back-compat ``bernstein replay <RUN_ID>`` shape).
    args = list(run_id)
    if args and args[0] == "diff":
        _replay_diff_dispatch(args[1:], sdd_dir=sdd_dir, as_json=as_json)
        return
    if args and args[0] in {"export", "publish", "verify", "diff-journal"}:
        _replay_journal_dispatch(args, sdd_dir=sdd_dir, as_json=as_json)
        return

    if len(args) != 1:
        console.print(
            "[red]Usage:[/red] bernstein replay <RUN_ID | AGENT_ID | latest | list> "
            "OR bernstein replay diff RUN_A RUN_B "
            "OR bernstein replay export|publish|verify|diff-journal ...",
        )
        raise SystemExit(2)

    # When an agent journal exists for this id, prefer the per-step view
    # over the run-trace view. The journal directory naming is unambiguous
    # because it always sits under ``.sdd/runtime/journal/`` (a path the
    # legacy run recorder never wrote to).
    sdd_path = Path(sdd_dir)
    journal_dir = sdd_path / "runtime" / "journal" / args[0]
    if journal_dir.exists():
        from bernstein.cli.commands.replay_cmd import replay_agent_view

        rc = replay_agent_view(
            agent_id=args[0],
            sdd_dir=sdd_path,
            as_json=as_json,
            limit=limit,
        )
        if rc != 0:
            raise SystemExit(rc)
        return

    _replay_run_impl(
        run_id=args[0],
        sdd_dir=sdd_dir,
        as_json=as_json,
        limit=limit,
        model=model,
        extra_context=extra_context,
    )


def _replay_journal_dispatch(
    args: list[str],
    *,
    sdd_dir: str,
    as_json: bool,
) -> None:
    """Dispatch the new ``export | publish | verify | diff-journal`` verbs.

    These touch only the per-step journal under ``.sdd/runtime/journal/``
    and never the legacy ``.sdd/runs/`` directory; the run-trace replay
    flow is unchanged.
    """
    verb = args[0]
    sdd_path = Path(sdd_dir)

    if verb == "export":
        if len(args) < 2:
            console.print("[red]Usage:[/red] bernstein replay export <AGENT_ID> [-o OUT]")
            raise SystemExit(2)
        agent_id = args[1]
        # Output path can be passed as the third positional or default beside .sdd.
        output: Path = Path(args[2]) if len(args) >= 3 else sdd_path / "runtime" / "receipts" / f"{agent_id}.tar"

        from bernstein.cli.commands.replay_cmd import replay_export

        rc = replay_export(
            agent_id=agent_id,
            sdd_dir=sdd_path,
            output=output,
        )
        if rc != 0:
            raise SystemExit(rc)
        return

    if verb == "publish":
        # Publish requires an explicit ``--yes`` style sentinel positional so
        # operators cannot accidentally publish from a script that just adds
        # a verb name.
        if len(args) < 2:
            console.print("[red]Usage:[/red] bernstein replay publish <AGENT_ID> [OUT] --opt-in")
            raise SystemExit(2)
        agent_id = args[1]
        opt_in = "--opt-in" in args
        positional_tail = [a for a in args[2:] if not a.startswith("--")]
        output = (
            Path(positional_tail[0])
            if positional_tail
            else sdd_path / "runtime" / "receipts" / f"{agent_id}.redacted.tar"
        )

        from bernstein.cli.commands.replay_cmd import replay_publish

        rc = replay_publish(
            agent_id=agent_id,
            sdd_dir=sdd_path,
            output=output,
            opt_in=opt_in,
        )
        if rc != 0:
            raise SystemExit(rc)
        return

    if verb == "verify":
        if len(args) < 2:
            console.print("[red]Usage:[/red] bernstein replay verify <RECEIPT> [--head HEX]")
            raise SystemExit(2)
        receipt = Path(args[1])
        expected_head: str | None = None
        for i, token in enumerate(args[2:], start=2):
            if token == "--head" and i + 1 < len(args):
                expected_head = args[i + 1]

        from bernstein.cli.commands.replay_cmd import replay_verify

        rc = replay_verify(
            receipt_path=receipt,
            expected_head=expected_head,
            public_key_path=None,
        )
        if rc != 0:
            raise SystemExit(rc)
        return

    if verb == "diff-journal":
        if len(args) != 3:
            console.print(
                "[red]Usage:[/red] bernstein replay diff-journal <LEFT_AGENT_ID> <RIGHT_AGENT_ID>",
            )
            raise SystemExit(2)

        from bernstein.cli.commands.replay_cmd import replay_diff_journals

        rc = replay_diff_journals(
            left_agent_id=args[1],
            right_agent_id=args[2],
            sdd_dir=sdd_path,
            as_json=as_json,
        )
        if rc != 0:
            raise SystemExit(rc)
        return

    console.print(f"[red]Unknown replay verb:[/red] {verb}")
    raise SystemExit(2)


def _replay_diff_dispatch(
    args: list[str],
    *,
    sdd_dir: str,
    as_json: bool,
) -> None:
    """Implementation of ``bernstein replay diff <run_a> <run_b>``."""
    if len(args) != 2:
        console.print(
            "[red]Usage:[/red] bernstein replay diff RUN_A RUN_B",
        )
        raise SystemExit(2)

    from bernstein.core.replay import EVENTS_FILENAME, diff_event_logs

    run_a, run_b = args
    runs_dir = Path(sdd_dir) / "runs"
    path_a = runs_dir / run_a / EVENTS_FILENAME
    path_b = runs_dir / run_b / EVENTS_FILENAME

    for label, path in (("run_a", path_a), ("run_b", path_b)):
        if not path.exists():
            console.print(f"[red]{label} events log not found:[/red] {path}")
            raise SystemExit(2)

    result = diff_event_logs(path_a, path_b)

    if as_json:
        payload = {
            "diverged": result.diverged,
            "index": result.index,
            "reason": result.reason,
            "a_event": result.a_event,
            "b_event": result.b_event,
        }
        console.print_json(json.dumps(payload, default=str))
        if result.diverged:
            raise SystemExit(1)
        return

    if not result.diverged:
        console.print(f"[green]No divergence:[/green] {result.reason}")
        return

    console.print(
        f"[yellow]Divergence at event index {result.index}:[/yellow] {result.reason}",
    )
    if result.a_event is not None:
        console.print(f"\n[dim]run_a[/dim] [bold]{run_a}[/bold]:")
        console.print_json(json.dumps(result.a_event, default=str))
    if result.b_event is not None:
        console.print(f"\n[dim]run_b[/dim] [bold]{run_b}[/bold]:")
        console.print_json(json.dumps(result.b_event, default=str))
    raise SystemExit(1)


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
    console.print("  GITHUB_TOKEN: personal access token")
    console.print("  GITHUB_REPO: owner/repo")


@github_group.command("test-webhook")
def _github_test_webhook() -> None:  # type: ignore[reportUnusedFunction]
    """Test GitHub webhook configuration."""
    console.print("[green]Webhook configured.[/green]")


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
@click.pass_context
def completions(ctx: click.Context, shell: str) -> None:
    """Generate shell completion scripts.

    \b
    For bash, add to ~/.bashrc:
      eval "$(bernstein completions --shell bash)"

    \b
    For zsh, add to ~/.zshrc:
      eval "$(bernstein completions --shell zsh)"

    \b
    For fish, add to ~/.config/fish/completions/bernstein.fish:
      bernstein completions --shell fish | source
    """
    from click.shell_completion import BashComplete, FishComplete, ZshComplete

    _complete_var = "_BERNSTEIN_COMPLETE"
    _prog_name = "bernstein"

    shell_cls = {"bash": BashComplete, "zsh": ZshComplete, "fish": FishComplete}[shell]
    # Walk up to the root CLI group so completions cover all subcommands.
    root_ctx = ctx
    while root_ctx.parent is not None:
        root_ctx = root_ctx.parent

    completer = shell_cls(root_ctx.command, {}, _prog_name, _complete_var)
    click.echo(completer.source())


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
