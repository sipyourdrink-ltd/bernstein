"""Status and diagnostic commands: status, ps, doctor, commit-stats."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, cast

import click

from bernstein.cli.helpers import (
    console,
    is_json,
    is_process_alive,
    print_banner,
    print_json,
    server_get,
)
from bernstein.cli.status import render_status
from bernstein.cli.ui import make_console
from bernstein.core.agent_discovery import AgentCapabilities, DiscoveryResult, discover_agents_cached
from bernstein.worker_badges import format_worker_badge, get_badge_for_worker


def _load_remote_agents_from_snapshot(runtime_dir: Path) -> list[dict[str, Any]]:
    """Load remote bridge-backed sessions from ``agents.json``."""
    state_path = runtime_dir / "agents.json"
    if not state_path.exists():
        return []
    try:
        data_raw = json.loads(state_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data_raw, dict):
        return []
    agents_raw = data_raw.get("agents")
    if not isinstance(agents_raw, list):
        return []

    remote_agents: list[dict[str, Any]] = []
    for item in agents_raw:
        if not isinstance(item, dict):
            continue
        runtime_backend = item.get("runtime_backend", "local")
        if runtime_backend == "local":
            continue
        started_at = item.get("spawn_ts", 0)
        runtime_s = time.time() - started_at if isinstance(started_at, (int, float)) and started_at else 0
        minutes, secs = divmod(int(runtime_s), 60)
        hours, minutes = divmod(minutes, 60)
        runtime_str = f"{hours}h {minutes:02d}m" if hours else f"{minutes}m {secs:02d}s"
        remote_agents.append(
            {
                "session": item.get("id", "?"),
                "role": item.get("role", "?"),
                "command": f"[remote] {runtime_backend}",
                "model": item.get("model", "?"),
                "worker_pid": "remote",
                "child_pid": "—",
                "runtime": runtime_str,
                "started_at": started_at,
                "runtime_backend": runtime_backend,
                "bridge_session_key": item.get("bridge_session_key"),
                "bridge_run_id": item.get("bridge_run_id"),
            }
        )
    return remote_agents


def _match_discovered_agent(
    command: str,
    model: str,
    discovery: DiscoveryResult,
) -> AgentCapabilities | None:
    """Best-effort match of a running session to discovered agent metadata."""
    command_token = command.replace("[remote]", "").strip().split(" ", 1)[0].lower()
    command_token = Path(command_token).name
    for agent in discovery.agents:
        binary_name = Path(agent.binary).name.lower()
        if command_token and (agent.name.lower() == command_token or binary_name == command_token):
            return agent

    model_lower = model.lower()
    for agent in discovery.agents:
        if any(
            model_lower == available.lower() or model_lower in available.lower() for available in agent.available_models
        ):
            return agent
    return None


def _skill_badges(agent: AgentCapabilities | None) -> list[str]:
    """Render concise capability badges for ps/status output."""
    if agent is None:
        return []
    badges = [f"reasoning:{agent.reasoning_strength}"]
    if agent.supports_mcp:
        badges.append("mcp")
    badges.extend(agent.best_for[:2])
    return badges


def _worker_tier(agent: AgentCapabilities | None) -> str:
    """Map discovered cost tier to the worker badge tier palette."""
    if agent is None:
        return "paid"
    if agent.cost_tier == "free":
        return "free"
    if agent.cost_tier in {"cheap", "moderate", "expensive"}:
        return "paid"
    return "enterprise"


def _decorate_agent_rows(agents: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Attach worker badge and capability badges to ps output rows."""
    try:
        discovery = discover_agents_cached()
    except Exception:
        return agents

    for agent in agents:
        matched = _match_discovered_agent(str(agent["command"]), str(agent["model"]), discovery)
        badge = get_badge_for_worker(
            worker_id=str(agent["session"]),
            role=str(agent["role"]),
            model=str(agent["model"]),
            tier=_worker_tier(matched),
        )
        agent["worker_badge"] = format_worker_badge(badge)
        agent["skill_badges"] = _skill_badges(matched)
    return agents


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


@click.command("score", hidden=True)
@click.option("--json", "as_json", is_flag=True, default=False, help="Output raw JSON.")
@click.option("--no-color", "no_color", is_flag=True, default=False, help="Disable colour output.")
@click.option(
    "--mode",
    "view_mode",
    type=click.Choice(["novice", "standard", "expert"], case_sensitive=False),
    default=None,
    help="Dashboard detail level (novice, standard, expert). Default: persisted or standard.",
)
def status(as_json: bool, no_color: bool, view_mode: str | None) -> None:
    """Task summary, active agents, cost estimate.

    \b
      bernstein status                  # Rich table output
      bernstein status --json           # machine-readable JSON
      bernstein status --mode expert    # show all details
      bernstein status --mode novice    # minimal output
    """
    from bernstein.core.view_mode import ViewMode, get_view_config, load_view_mode

    data = server_get("/status")
    if data is None:
        if as_json or is_json():
            print_json({"error": "Cannot reach task server"})
        else:
            console.print(
                "[red]Cannot reach task server.[/red] Is Bernstein running? Run [bold]bernstein[/bold] to start."
            )
        raise SystemExit(1)

    if as_json or is_json():
        print_json(data)
        return

    print_banner()

    # Resolve view mode: CLI flag > persisted > standard
    mode = ViewMode(view_mode.lower()) if view_mode is not None else load_view_mode(Path.cwd())
    vc = get_view_config(mode)

    # Detect non-TTY (piped output) or explicit --no-color
    force_no_color = no_color or not sys.stdout.isatty()
    con = make_console(no_color=force_no_color)

    render_status(data, console=con, view_config=vc)


# ---------------------------------------------------------------------------
# ps — process visibility
# ---------------------------------------------------------------------------


@click.command("ps")
@click.option("--json-output", "as_json", is_flag=True, help="Output as JSON instead of table.")
@click.option("--pid-dir", default=".sdd/runtime/pids", help="PID metadata directory.")
def ps_cmd(as_json: bool, pid_dir: str) -> None:
    """Show running Bernstein agent processes."""
    from rich.table import Table

    pid_path = Path(pid_dir)
    agents: list[dict[str, Any]] = []
    stale_files: list[Path] = []

    if pid_path.exists():
        for pid_file in sorted(pid_path.glob("*.json")):
            try:
                info = json.loads(pid_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue

            worker_pid = info.get("worker_pid", 0)
            child_pid = info.get("child_pid")
            alive = is_process_alive(worker_pid) if worker_pid else False

            if not alive:
                stale_files.append(pid_file)
                continue

            started_at = info.get("started_at", 0)
            runtime_s = time.time() - started_at if started_at else 0
            minutes, secs = divmod(int(runtime_s), 60)
            hours, minutes = divmod(minutes, 60)
            runtime_str = f"{hours}h {minutes:02d}m" if hours else f"{minutes}m {secs:02d}s"

            agents.append(
                {
                    "session": info.get("session", "?"),
                    "role": info.get("role", "?"),
                    "command": info.get("command", "?"),
                    "model": info.get("model", "?"),
                    "worker_pid": worker_pid,
                    "child_pid": child_pid,
                    "runtime": runtime_str,
                    "started_at": started_at,
                }
            )

    # Clean up stale PID files
    for f in stale_files:
        f.unlink(missing_ok=True)

    runtime_dir = pid_path.parent
    remote_agents = _load_remote_agents_from_snapshot(runtime_dir)
    seen_sessions = {str(agent["session"]) for agent in agents}
    for remote in remote_agents:
        if str(remote["session"]) not in seen_sessions:
            agents.append(remote)

    agents = _decorate_agent_rows(agents)

    if as_json or is_json():
        print_json(agents)
        return

    if not agents:
        console.print("[dim]No running agents.[/dim]")
        return

    table = Table(title="Bernstein Agents", show_lines=False, header_style="bold cyan")
    table.add_column("Session", style="dim", min_width=18)
    table.add_column("Role", min_width=10)
    table.add_column("CLI", min_width=8)
    table.add_column("Model", min_width=16)
    table.add_column("Worker", min_width=22)
    table.add_column("Skills", min_width=18)
    table.add_column("Worker PID", justify="right")
    table.add_column("Agent PID", justify="right")
    table.add_column("Runtime", justify="right")

    for a in agents:
        table.add_row(
            a["session"],
            f"[bold]{a['role']}[/bold]",
            a["command"],
            a["model"],
            str(a.get("worker_badge", "")),
            ", ".join(cast("list[str]", a.get("skill_badges", []))),
            str(a["worker_pid"]),
            str(a["child_pid"] or "—"),
            a["runtime"],
        )

    console.print(table)
    console.print(f"\n[dim]{len(agents)} agent(s) running[/dim]")


# ---------------------------------------------------------------------------
# doctor — self-diagnostic helpers
# ---------------------------------------------------------------------------

_CheckFn = Any  # Callable[[str, bool, str, str, str], None]


def _doctor_check_storage(_check: _CheckFn) -> None:
    """Check storage backend connectivity (memory/postgres/redis)."""
    storage_backend = os.environ.get("BERNSTEIN_STORAGE_BACKEND", "memory")
    if storage_backend == "memory":
        _check("Storage backend", True, "memory (default, no external dependencies)", "")
        return
    if storage_backend == "postgres":
        _doctor_check_postgres(_check)
        return
    if storage_backend == "redis":
        _doctor_check_redis(_check)
        return
    _check(
        "Storage backend",
        False,
        f"unknown backend: {storage_backend}",
        "Set BERNSTEIN_STORAGE_BACKEND to memory, postgres, or redis",
    )


def _doctor_check_postgres(_check: _CheckFn) -> None:
    """Check postgres backend connectivity."""
    db_url = os.environ.get("BERNSTEIN_DATABASE_URL")
    if not db_url:
        _check(
            "Storage backend",
            False,
            "postgres — BERNSTEIN_DATABASE_URL not set",
            "export BERNSTEIN_DATABASE_URL=postgresql://user:pass@localhost/bernstein",
        )
        return
    try:
        import asyncpg  # type: ignore[import-untyped]

        async def _check_pg() -> bool:
            conn = await asyncpg.connect(db_url)  # type: ignore[reportUnknownVariableType,reportUnknownMemberType]
            await conn.close()  # type: ignore[reportUnknownMemberType]
            return True

        import asyncio

        asyncio.run(_check_pg())
        _check("Storage backend", True, f"postgres — connected ({db_url[:40]}...)", "")
    except ImportError:
        _check(
            "Storage backend",
            False,
            "postgres — asyncpg not installed",
            "pip install bernstein[postgres]",
        )
    except Exception as exc:
        _check(
            "Storage backend",
            False,
            f"postgres — connection failed: {exc}",
            "Check BERNSTEIN_DATABASE_URL and ensure PostgreSQL is running",
        )


def _doctor_check_redis(_check: _CheckFn) -> None:
    """Check redis backend connectivity."""
    db_url = os.environ.get("BERNSTEIN_DATABASE_URL")
    redis_url = os.environ.get("BERNSTEIN_REDIS_URL")
    storage_ok = True
    if not db_url:
        _check(
            "Storage backend (postgres)",
            False,
            "redis mode — BERNSTEIN_DATABASE_URL not set",
            "export BERNSTEIN_DATABASE_URL=postgresql://user:pass@localhost/bernstein",
        )
        storage_ok = False
    if not redis_url:
        _check(
            "Storage backend (redis)",
            False,
            "redis mode — BERNSTEIN_REDIS_URL not set",
            "export BERNSTEIN_REDIS_URL=redis://localhost:6379",
        )
        storage_ok = False
    if storage_ok:
        _check("Storage backend", True, "redis mode (pg + redis locking)", "")


def _doctor_check_secrets(workdir: Path, _check: _CheckFn) -> None:
    """Check secrets manager configuration and connectivity."""
    from bernstein.core.secrets import SecretsConfig, check_provider_connectivity

    secrets_cfg: SecretsConfig | None = None
    for config_path in (workdir / ".sdd" / "config.yaml", workdir / "bernstein.yaml"):
        if secrets_cfg is not None:
            break
        if not config_path.exists():
            continue
        try:
            import yaml as _yaml

            raw_data = _yaml.safe_load(config_path.read_text(encoding="utf-8"))
            if isinstance(raw_data, dict) and "secrets" in raw_data:
                s = raw_data["secrets"]
                if isinstance(s, dict) and "provider" in s and "path" in s:
                    secrets_cfg = SecretsConfig(
                        provider=s["provider"],
                        path=s["path"],
                        ttl=s.get("ttl", 300),
                        field_map=s.get("field_map", {}),
                    )
        except Exception:
            pass

    if secrets_cfg is not None:
        sm_ok, sm_detail = check_provider_connectivity(secrets_cfg)
        _check(
            f"Secrets: {secrets_cfg.provider}",
            sm_ok,
            sm_detail,
            f"Check {secrets_cfg.provider} connectivity and credentials" if not sm_ok else "",
        )
    else:
        _check("Secrets manager", True, "not configured (using env vars)", "")


def _doctor_auto_fix(
    checks: list[dict[str, Any]],
    stale_pid_paths: list[Path],
    workdir: Path,
    fixed: list[str],
    manual_needed: list[str],
) -> None:
    """Attempt to auto-fix issues detected by doctor checks."""
    failed = [c for c in checks if not c["ok"] and c.get("fix_id")]
    for c in failed:
        fix_id = c["fix_id"]
        if fix_id == "port_in_use":
            try:
                from bernstein.cli.stop_cmd import soft_stop

                soft_stop(timeout=10)
                fixed.append("Killed stale server on port 8052")
            except Exception:
                manual_needed.append("Run 'bernstein stop' to free port 8052")
        elif fix_id == "sdd_missing":
            try:
                from bernstein.core.server_launch import ensure_sdd

                ensure_sdd(workdir)
                fixed.append("Created .sdd workspace")
            except Exception:
                manual_needed.append("Run 'bernstein init' to create .sdd workspace")
        elif fix_id == "stale_pids":
            count = 0
            for pid_file in stale_pid_paths:
                try:
                    pid_file.unlink(missing_ok=True)
                    count += 1
                except OSError:
                    pass
            if count > 0:
                fixed.append(f"Cleaned {count} stale PID file(s)")
        elif fix_id == "codex_login":
            manual_needed.append("Run 'codex login' to authenticate Codex CLI")
        elif fix_id == "gemini_auth":
            manual_needed.append("Run 'gemini' to authenticate Gemini CLI (prompts on first run)")


# ---------------------------------------------------------------------------
# doctor — self-diagnostic
# ---------------------------------------------------------------------------


@click.command("doctor")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output raw JSON.")
@click.option("--fix", "auto_fix", is_flag=True, default=False, help="Attempt to auto-fix issues.")
def doctor(as_json: bool, auto_fix: bool) -> None:
    """Run self-diagnostics: check Python, adapters, API keys, port, and workspace.

    \b
      bernstein doctor          # print diagnostic report
      bernstein doctor --json   # machine-readable output
      bernstein doctor --fix    # attempt to auto-fix issues
    """
    import shutil
    import socket

    from bernstein.core.preflight import (
        _claude_has_oauth_session,  # type: ignore[reportPrivateUsage]
        _codex_has_auth,  # type: ignore[reportPrivateUsage]
        gemini_has_auth,  # type: ignore[reportPrivateUsage]
    )

    checks: list[dict[str, Any]] = []
    # Track auto-fix results: (description, succeeded)
    fixed: list[str] = []
    manual_needed: list[str] = []

    def _check(name: str, ok: bool, detail: str, fix: str = "", fix_id: str = "") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail, "fix": fix, "fix_id": fix_id})

    # 1. Python version
    major, minor = sys.version_info.major, sys.version_info.minor
    py_ok = (major, minor) >= (3, 12)
    _check(
        "Python version",
        py_ok,
        f"Python {major}.{minor} (need 3.12+)",
        "Install Python 3.12 or newer" if not py_ok else "",
    )

    # 2. CLI adapters
    adapter_names = ["claude", "codex", "gemini"]
    any_adapter = False
    for adapter_name in adapter_names:
        found = shutil.which(adapter_name) is not None
        if found:
            any_adapter = True
        _check(
            f"Adapter: {adapter_name}",
            found,
            "found in PATH" if found else "not in PATH",
            f"Install {adapter_name} CLI — see docs" if not found else "",
        )

    # 3. Auth checks — detect all auth methods per adapter
    any_key = False

    # Claude: API key or OAuth
    claude_has_key = bool(os.environ.get("ANTHROPIC_API_KEY"))
    claude_authed = claude_has_key
    claude_detail = "ANTHROPIC_API_KEY set" if claude_has_key else "ANTHROPIC_API_KEY not set"
    if not claude_has_key:
        if _claude_has_oauth_session():
            claude_detail = "OAuth active"
            claude_authed = True
        else:
            claude_detail = "not authenticated"
    if claude_authed:
        any_key = True
    _check(
        "Auth: claude",
        claude_authed,
        claude_detail,
        "export ANTHROPIC_API_KEY=key or: claude login" if not claude_authed else "",
    )

    # Codex: API key or ChatGPT login
    codex_authed, codex_method = _codex_has_auth()
    if codex_authed:
        any_key = True
        codex_detail = codex_method
    else:
        codex_detail = "not authenticated"
    _check(
        "Auth: codex",
        codex_authed,
        codex_detail,
        "export OPENAI_API_KEY=key or: codex login" if not codex_authed else "",
        fix_id="codex_login" if not codex_authed else "",
    )

    # Gemini: API key, gcloud auth, config dir, GOOGLE_APPLICATION_CREDENTIALS
    gemini_authed, gemini_method = gemini_has_auth()
    if gemini_authed:
        any_key = True
        gemini_detail = gemini_method
    else:
        gemini_detail = "not authenticated"
    _check(
        "Auth: gemini",
        gemini_authed,
        gemini_detail,
        "export GOOGLE_API_KEY=key, or: gcloud auth login" if not gemini_authed else "",
        fix_id="gemini_auth" if not gemini_authed else "",
    )

    # 4. Port 8052 availability
    port = 8052
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            result = s.connect_ex(("127.0.0.1", port))
            port_in_use = result == 0
    except Exception:
        port_in_use = False
    _check(
        f"Port {port}",
        not port_in_use,
        "in use — server may already be running" if port_in_use else "available",
        "Run 'bernstein stop' to free the port" if port_in_use else "",
        fix_id="port_in_use" if port_in_use else "",
    )

    # 5. .sdd/ structure
    workdir = Path.cwd()
    required_dirs = [".sdd", ".sdd/backlog", ".sdd/runtime"]
    sdd_ok = all((workdir / d).exists() for d in required_dirs)
    _check(
        ".sdd workspace",
        sdd_ok,
        "present" if sdd_ok else "missing or incomplete",
        "Run 'bernstein' or 'bernstein -g \"goal\"' to initialise" if not sdd_ok else "",
        fix_id="sdd_missing" if not sdd_ok else "",
    )

    # 6. Stale PID files
    stale_pids: list[str] = []
    stale_pid_paths: list[Path] = []
    for pid_name in ("server.pid", "spawner.pid", "watchdog.pid"):
        pid_path = workdir / ".sdd" / "runtime" / pid_name
        if pid_path.exists():
            try:
                pid_val = int(pid_path.read_text().strip())
                from bernstein.core.platform_compat import process_alive

                if not process_alive(pid_val):
                    stale_pids.append(pid_name)
                    stale_pid_paths.append(pid_path)
            except ValueError:
                stale_pids.append(pid_name)
                stale_pid_paths.append(pid_path)
    _check(
        "Stale PID files",
        len(stale_pids) == 0,
        f"found: {', '.join(stale_pids)}" if stale_pids else "none",
        "Run 'bernstein stop' to clean up" if stale_pids else "",
        fix_id="stale_pids" if stale_pids else "",
    )

    # 7. Guardrail stats
    from bernstein.core.guardrails import get_guardrail_stats

    guardrail_stats = get_guardrail_stats(workdir)
    g_total = guardrail_stats["total"]
    g_blocked = guardrail_stats["blocked"]
    g_flagged = guardrail_stats["flagged"]
    if g_total > 0:
        g_detail = f"{g_total} checked, {g_blocked} blocked, {g_flagged} flagged"
    else:
        g_detail = "no events recorded yet"
    _check("Guardrails", True, g_detail)

    # 8. CI tool dependencies (ruff, pytest, pyright)
    from bernstein.core.ci_fix import check_test_dependencies

    ci_dep_results = check_test_dependencies()
    for dep in ci_dep_results:
        _check(
            f"CI tool: {dep['name']}",
            dep["ok"] == "True",
            dep["detail"],
            dep["fix"],
        )

    # 9. Storage backend connectivity
    _doctor_check_storage(_check)

    # 10. Secrets manager connectivity
    _doctor_check_secrets(workdir, _check)

    # 11. Overall readiness
    any_adapter_key = any_adapter and any_key
    _check(
        "Ready to run",
        py_ok and any_adapter_key,
        "yes" if (py_ok and any_adapter_key) else "missing adapter or API key",
        "Install an adapter (claude/codex/gemini) and set its API key" if not any_adapter_key else "",
    )

    # 12. Context file warnings (CLAUDE.md, AGENTS.md, etc.)
    from bernstein.context_files_doctor import check_context_files

    context_warnings = check_context_files(workdir)
    for w in context_warnings:
        _check(
            w.name,
            w.ok,
            w.detail,
            w.fix,
        )

    # 13. MCP server reachability
    from bernstein.context_files_doctor import check_mcp_servers

    mcp_warnings = check_mcp_servers(workdir)
    for w in mcp_warnings:
        _check(
            w.name,
            w.ok,
            w.detail,
            w.fix,
        )

    # 14. Permission rule health
    from bernstein.context_files_doctor import check_permission_rules

    perm_warnings = check_permission_rules(workdir)
    for w in perm_warnings:
        _check(
            w.name,
            w.ok,
            w.detail,
            w.fix,
        )

    # 15. Installation mismatches
    from bernstein.install_check import check_installations

    for w in check_installations():
        _check(
            w.name,
            w.ok,
            w.detail,
            w.fix,
        )

    # 16. Plugin loading errors
    from bernstein.plugin_errors import get_plugin_errors

    plugin_errors = get_plugin_errors().get_errors()
    if plugin_errors:
        for pe in plugin_errors:
            detail = f"[{pe.phase}] {pe.message}"
            _check(
                f"Plugin: {pe.plugin_name}",
                ok=False,
                detail=detail,
                fix=f"Check plugin {pe.plugin_name} configuration",
            )
    else:
        _check(
            "Plugin loading",
            ok=True,
            detail="no errors",
        )

    # 17. Commit attribution (doctor check — shows agent contribution summary)
    from bernstein.commit_stats import collect_commit_stats

    commit_result = collect_commit_stats(repo_dir=str(workdir))
    if commit_result.error:
        _check(
            "Commit attribution",
            ok=False,
            detail=f"git log error: {commit_result.error}",
            fix="Ensure this is a git repository with git installed",
        )
    elif not commit_result.roles:
        _check(
            "Commit attribution",
            ok=True,
            detail="no commits found in this repository",
        )
    else:
        role_parts = ", ".join(
            f"{role}: {rs.commits} commits, +{rs.lines_added}/-{rs.lines_deleted}"
            for role, rs in commit_result.roles.items()
        )
        _check(
            "Commit attribution",
            ok=True,
            detail=f"{commit_result.total_commits} commits: {role_parts}",
        )

    # 18. Compliance mode prerequisites
    from bernstein.core.compliance import load_compliance_config

    compliance_cfg = load_compliance_config(workdir / ".sdd")
    compliance_env = os.environ.get("BERNSTEIN_COMPLIANCE")
    if compliance_env:
        from bernstein.core.compliance import ComplianceConfig, CompliancePreset

        compliance_cfg = ComplianceConfig.from_preset(CompliancePreset(compliance_env.lower()))

    if compliance_cfg is not None:
        preset_label = compliance_cfg.preset.value if compliance_cfg.preset else "custom"
        prereq_warnings = compliance_cfg.check_prerequisites()
        if prereq_warnings:
            _check(
                f"Compliance ({preset_label})",
                False,
                f"{len(prereq_warnings)} issue(s): {prereq_warnings[0]}",
                "; ".join(prereq_warnings),
            )
        else:
            _check(f"Compliance ({preset_label})", True, "all prerequisites met", "")

    # --fix: attempt to auto-fix issues
    if auto_fix:
        _doctor_auto_fix(checks, stale_pid_paths, workdir, fixed, manual_needed)

    # 10. Secrets provider connectivity
    try:
        # Load config from bernstein.yaml if it exists
        yaml_path = workdir / "bernstein.yaml"
        if yaml_path.exists():
            from bernstein.core.seed import parse_seed

            seed = parse_seed(yaml_path)
            if seed.secrets:
                from bernstein.core.secrets import check_secrets_connectivity

                ok, detail = check_secrets_connectivity(seed.secrets)
                _check(
                    f"Secrets: {seed.secrets.provider}",
                    ok,
                    detail,
                    f"Check {seed.secrets.provider} credentials and path {seed.secrets.path}" if not ok else "",
                )
            else:
                _check("Secrets", True, "none (using environment variables)", "")
        else:
            _check("Secrets", True, "no bernstein.yaml (using environment variables)", "")
    except Exception as exc:
        _check("Secrets", False, f"configuration error: {exc}", "Check bernstein.yaml syntax")

    if as_json or is_json():
        result_dict: dict[str, Any] = {"checks": checks}
        if auto_fix:
            result_dict["fixed"] = fixed
            result_dict["manual_needed"] = manual_needed
        print_json(result_dict)
        failed_checks = [c for c in checks if not c["ok"]]
        if failed_checks:
            raise SystemExit(1)
        return

    from rich.table import Table

    table = Table(title="Bernstein Doctor", header_style="bold cyan", show_lines=False)
    table.add_column("Check", min_width=22)
    table.add_column("Status", min_width=8)
    table.add_column("Detail", min_width=35)
    table.add_column("Fix")

    for c in checks:
        icon = "[green]✓[/green]" if c["ok"] else "[red]✗[/red]"
        table.add_row(
            c["name"],
            icon,
            c["detail"],
            f"[dim]{c['fix']}[/dim]" if c["fix"] else "",
        )

    console.print(table)

    # Show --fix results
    if auto_fix and (fixed or manual_needed):
        console.print()
        if fixed:
            console.print("[bold green]Fixed:[/bold green]")
            for msg in fixed:
                console.print(f"  [green]✓[/green] {msg}")
        if manual_needed:
            console.print("[bold yellow]Manual action needed:[/bold yellow]")
            for msg in manual_needed:
                console.print(f"  [yellow]→[/yellow] {msg}")

    failed_checks = [c for c in checks if not c["ok"]]
    if failed_checks:
        console.print(f"\n[red]{len(failed_checks)} issue(s) found.[/red]")
        if not auto_fix:
            console.print("[dim]Run 'bernstein doctor --fix' to attempt auto-repair.[/dim]")
        raise SystemExit(1)
    else:
        console.print("\n[green]All checks passed.[/green]")


# ---------------------------------------------------------------------------
# commit-stats — agent attribution report
# ---------------------------------------------------------------------------


@click.command("commit-stats")
@click.option("--since", default=None, help="Date range start (e.g. 2025-01-01).")
@click.option("--until", default=None, help="Date range end (e.g. 2025-12-31).")
@click.option("--repo-dir", default=".", help="Path to git repository.")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON.")
def commit_stats_cmd(since: str | None, until: str | None, repo_dir: str, as_json: bool) -> None:
    """Show commit attribution by agent role.

    \b
      bernstein commit-stats                    # all-time stats
      bernstein commit-stats --since 2025-01-01 # since a date
      bernstein commit-stats --json             # machine-readable output
    """
    from bernstein.commit_stats import collect_commit_stats, render_commit_stats

    result = collect_commit_stats(repo_dir=repo_dir, since=since, until=until)
    if result.error:
        if as_json or is_json():
            print_json(result.to_dict())
        else:
            console.print(f"[red]Error: {result.error}[/red]")
        raise SystemExit(1)

    if as_json or is_json():
        print_json(result.to_dict())
    else:
        render_commit_stats(result)
