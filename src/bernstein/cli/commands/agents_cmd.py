"""Agent catalog management commands: sync, list, validate, showcase, match, discover."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, cast

import click

from bernstein.cli.helpers import console

# ---------------------------------------------------------------------------
# agents group
# ---------------------------------------------------------------------------


@click.group("agents")
def agents_group() -> None:
    """Manage agent catalogs: sync, list, and validate.

    \b
      bernstein agents sync               # refresh all catalogs
      bernstein agents list               # show all available agents
      bernstein agents list --source local  # filter by source
      bernstein agents validate           # check catalog health
    """


@agents_group.command("sync")
@click.option(
    "--dir",
    "definitions_dir",
    default=".sdd/agents/definitions",
    show_default=True,
    help="Agent definitions directory.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Force re-sync even if within the 24-hour TTL.",
)
def agents_sync(definitions_dir: str, force: bool) -> None:
    """Force-refresh all agent catalogs and update cache."""
    from bernstein.agents.agency_provider import AgencyProvider
    from bernstein.agents.registry import AgentRegistry

    definitions_path = Path(definitions_dir)

    # Provider: local YAML definitions
    console.print("[bold]Syncing agent catalogs…[/bold]\n")
    console.print(f"[cyan]→ local[/cyan]  {definitions_path}")

    if not definitions_path.exists():
        console.print(f"  [yellow]Directory does not exist:[/yellow] {definitions_path}")
        console.print(f"  [dim]Create it with: mkdir -p {definitions_path}[/dim]")
    else:
        registry = AgentRegistry(definitions_dir=definitions_path)
        loaded = registry.load_definitions()
        console.print(f"  [green]✓[/green] Loaded {len(loaded)} agent definition(s)")
        for defn in loaded:
            console.print(f"    [dim]{defn.name}[/dim] v{defn.version} ({defn.role})")

    # Provider: agency catalog (legacy YAML format — .sdd/agents/agency/)
    agency_dir = Path(".sdd/agents/agency")
    console.print(f"\n[cyan]→ agency (local YAML)[/cyan] {agency_dir}")
    if not agency_dir.exists():
        console.print(f"  [dim]Directory not found — skipping (place Agency YAML files in {agency_dir})[/dim]")
    else:
        from bernstein.core.agency_loader import load_agency_catalog

        catalog = load_agency_catalog(agency_dir)
        console.print(f"  [green]✓[/green] Loaded {len(catalog)} agency agent(s)")
        for name in list(catalog)[:5]:
            agent = catalog[name]
            console.print(f"    [dim]{name}[/dim] ({agent.role})")
        if len(catalog) > 5:
            console.print(f"    [dim]… and {len(catalog) - 5} more[/dim]")

    # Provider: Agency GitHub repo (msitarzewski/agency-agents markdown format)
    default_agency_path = AgencyProvider.default_cache_path()
    console.print(f"\n[cyan]→ agency (GitHub)[/cyan] {default_agency_path}")
    ok, msg = AgencyProvider.sync_catalog(force=force)
    if ok:
        console.print(f"  [green]✓[/green] {msg}")
        provider = AgencyProvider(local_path=default_agency_path)
        agency_agents = asyncio.run(provider.fetch_agents())
        console.print(f"  [green]✓[/green] {len(agency_agents)} specialist agent(s) available")
        for a in agency_agents[:5]:
            caps = ", ".join(a.capabilities[:3]) if a.capabilities else "—"
            console.print(f"    [dim]{a.name}[/dim] ({a.role})  {caps}")
        if len(agency_agents) > 5:
            console.print(f"    [dim]… and {len(agency_agents) - 5} more[/dim]")
    else:
        console.print(f"  [yellow]![/yellow] {msg}")
        console.print(
            f"  [dim]Manual clone: git clone https://github.com/msitarzewski/agency-agents {default_agency_path}[/dim]"
        )

    console.print("\n[green]Sync complete.[/green]")


def _list_identities(status_filter: str) -> None:
    """Display agent identities from the identity store."""
    from rich.table import Table

    from bernstein.core.agent_identity import AgentIdentityStatus, AgentIdentityStore

    store = AgentIdentityStore(Path(".sdd/auth"))
    filter_status = AgentIdentityStatus(status_filter) if status_filter != "all" else None
    identities = store.list_identities(status=filter_status)

    if not identities:
        console.print("[dim]No agent identities found.[/dim]")
        return

    table = Table(
        title="Agent Identities",
        show_lines=False,
        header_style="bold cyan",
    )
    table.add_column("ID", style="dim", min_width=20)
    table.add_column("ROLE", min_width=10)
    table.add_column("STATUS", min_width=10)
    table.add_column("PERMISSIONS", min_width=30)
    table.add_column("CREATED", min_width=20)
    table.add_column("PARENT", min_width=16)

    from datetime import UTC, datetime

    for ident in identities:
        status_color = {
            AgentIdentityStatus.ACTIVE: "green",
            AgentIdentityStatus.SUSPENDED: "yellow",
            AgentIdentityStatus.REVOKED: "red",
        }.get(ident.status, "dim")
        created = datetime.fromtimestamp(ident.created_at, tz=UTC).strftime("%Y-%m-%d %H:%M")
        perms = ", ".join(sorted(ident.permissions)[:4])
        if len(ident.permissions) > 4:
            perms += f" (+{len(ident.permissions) - 4})"
        table.add_row(
            ident.id,
            ident.role,
            f"[{status_color}]{ident.status.value}[/{status_color}]",
            perms or "[dim]---[/dim]",
            created,
            ident.parent_identity_id or "[dim]---[/dim]",
        )

    console.print(table)
    console.print(f"\n[dim]{len(identities)} identity(ies) total[/dim]")


@agents_group.command("list")
@click.option(
    "--source",
    type=click.Choice(["local", "agency", "all"]),
    default="all",
    show_default=True,
    help="Filter agents by catalog source.",
)
@click.option(
    "--dir",
    "definitions_dir",
    default=".sdd/agents/definitions",
    show_default=True,
    help="Local agent definitions directory.",
)
@click.option(
    "--identities",
    is_flag=True,
    default=False,
    help="Show agent identities instead of catalog agents.",
)
@click.option(
    "--identity-status",
    type=click.Choice(["active", "suspended", "revoked", "all"]),
    default="all",
    show_default=True,
    help="Filter identities by status (only with --identities).",
)
def agents_list(source: str, definitions_dir: str, identities: bool, identity_status: str) -> None:
    """List all available agents from loaded catalogs."""
    if identities:
        _list_identities(identity_status)
        return
    from bernstein.agents.agency_provider import AgencyProvider
    from bernstein.agents.registry import AgentRegistry

    # rows: (id, name, role, capabilities, source)
    rows: list[tuple[str, str, str, str, str]] = []

    # Local definitions
    if source in ("local", "all"):
        definitions_path = Path(definitions_dir)
        if definitions_path.exists():
            registry = AgentRegistry(definitions_dir=definitions_path)
            registry.load_definitions()
            for defn in registry.definitions.values():
                rows.append((defn.name, defn.name, defn.role, "", "local"))

    # Agency catalog — legacy YAML format (.sdd/agents/agency/)
    if source in ("agency", "all"):
        agency_dir = Path(".sdd/agents/agency")
        if agency_dir.exists():
            from bernstein.core.agency_loader import load_agency_catalog

            catalog = load_agency_catalog(agency_dir)
            for name, agent in catalog.items():
                rows.append((name, agent.name, agent.role, "", "agency"))

    # Agency catalog — GitHub markdown format (~/.bernstein/catalogs/agency/)
    if source in ("agency", "all"):
        default_agency_path = AgencyProvider.default_cache_path()
        if default_agency_path.exists():
            provider = AgencyProvider(local_path=default_agency_path)
            agency_agents = asyncio.run(provider.fetch_agents())
            for a in agency_agents:
                caps = ", ".join(a.capabilities[:4]) if a.capabilities else ""
                rows.append((a.id or a.name, a.name, a.role, caps, "agency"))

    if not rows:
        console.print("[dim]No agents found. Run [bold]bernstein agents sync[/bold] first.[/dim]")
        return

    from rich.table import Table

    table = Table(
        title="Available Agents",
        show_lines=False,
        header_style="bold cyan",
    )
    table.add_column("NAME", style="dim", min_width=22)
    table.add_column("ROLE", min_width=12)
    table.add_column("CAPABILITIES", min_width=32)
    table.add_column("SOURCE", min_width=8)

    source_order = {"agency": 0, "local": 1}
    for _agent_id, name, role, caps, src in sorted(rows, key=lambda r: (source_order.get(r[4], 9), r[1])):
        src_color = "cyan" if src == "local" else "magenta"
        table.add_row(
            name,
            role,
            caps or "[dim]—[/dim]",
            f"[{src_color}]{src}[/{src_color}]",
        )

    console.print(table)
    console.print(f"\n[dim]{len(rows)} agent(s) total[/dim]")


@agents_group.command("validate")
@click.option(
    "--dir",
    "definitions_dir",
    default=".sdd/agents/definitions",
    show_default=True,
    help="Local agent definitions directory.",
)
def agents_validate(definitions_dir: str) -> None:
    """Validate all agent catalogs and report issues.

    Exits with code 1 if any provider is unreachable or has invalid agents.
    """
    import yaml

    from bernstein.agents.registry import SchemaValidationError

    definitions_path = Path(definitions_dir)
    issues: list[str] = []

    console.print("[bold]Validating agent catalogs…[/bold]\n")

    # --- Local definitions ---
    console.print(f"[cyan]→ local[/cyan]  {definitions_path}")
    if not definitions_path.exists():
        issues.append(f"local: definitions directory not found: {definitions_path}")
        console.print(f"  [red]✗[/red] Directory not found: {definitions_path}")
    else:
        yaml_files = list(definitions_path.glob("*.yaml")) + list(definitions_path.glob("*.yml"))
        if not yaml_files:
            console.print("  [dim]No YAML files found — catalog is empty[/dim]")
        for yaml_file in sorted(yaml_files):
            try:
                content = yaml_file.read_text(encoding="utf-8")
                data = yaml.safe_load(content)
                if not isinstance(data, dict):
                    raise ValueError("YAML must be a mapping")
                from bernstein.agents.registry import AgentRegistry

                registry = AgentRegistry(definitions_dir=definitions_path)
                registry._validate_schema(cast("dict[str, Any]", data), yaml_file)  # type: ignore[reportPrivateUsage]
                console.print(f"  [green]✓[/green] {yaml_file.name}")
            except SchemaValidationError as exc:
                issues.append(f"local/{yaml_file.name}: {exc}")
                console.print(f"  [red]✗[/red] {yaml_file.name}: {exc}")
            except Exception as exc:
                issues.append(f"local/{yaml_file.name}: {exc}")
                console.print(f"  [red]✗[/red] {yaml_file.name}: {exc}")

    # --- Agency catalog ---
    agency_dir = Path(".sdd/agents/agency")
    console.print(f"\n[cyan]→ agency[/cyan] {agency_dir}")
    if not agency_dir.exists():
        console.print("  [dim]Not configured — skipping[/dim]")
    else:
        from bernstein.core.agency_loader import parse_agency_agent

        agency_files = [p for p in sorted(agency_dir.iterdir()) if p.suffix in (".yaml", ".yml")]
        if not agency_files:
            console.print("  [dim]No YAML files found — catalog is empty[/dim]")
        for p in agency_files:
            try:
                parse_agency_agent(p)
                console.print(f"  [green]✓[/green] {p.name}")
            except ValueError as exc:
                issues.append(f"agency/{p.name}: {exc}")
                console.print(f"  [red]✗[/red] {p.name}: {exc}")

    # --- Summary ---
    console.print()
    if issues:
        console.print(f"[red]Validation failed: {len(issues)} issue(s)[/red]")
        for issue in issues:
            console.print(f"  [red]•[/red] {issue}")
        raise SystemExit(1)
    else:
        console.print("[green]All catalogs valid.[/green]")


@agents_group.command("showcase")
@click.option(
    "--dir",
    "definitions_dir",
    default=".sdd/agents/definitions",
    show_default=True,
    help="Local agent definitions directory.",
)
def agents_showcase(definitions_dir: str) -> None:
    """Rich display of available agents grouped by role, with success rates.

    \b
    Shows:
      - All agents from loaded catalogs, grouped by role / division
      - Per-agent match count and success rate (from .sdd/agents/registry.json)
      - Featured agents with the highest success rates
    """
    from rich.table import Table

    from bernstein.agents.discovery import AgentDiscovery

    # Load success metrics from registry
    discovery = AgentDiscovery.load()
    metrics = discovery.metrics

    rows: list[tuple[str, str, str, str, str, str]] = []

    # Local definitions
    definitions_path = Path(definitions_dir)
    if definitions_path.exists():
        from bernstein.agents.registry import AgentRegistry

        registry = AgentRegistry(definitions_dir=definitions_path)
        registry.load_definitions()
        for defn in registry.definitions.values():
            m = metrics.get("local")
            rate = f"{m.success_rate * 100:.0f}%" if m and m.tasks_assigned else "—"
            assigned = str(m.tasks_assigned) if m else "0"
            rows.append((defn.name, defn.role, defn.description[:60], "local", assigned, rate))

    # Agency catalog
    agency_dir = Path(".sdd/agents/agency")
    if agency_dir.exists():
        from bernstein.core.agency_loader import load_agency_catalog

        catalog = load_agency_catalog(agency_dir)
        for name, agent in catalog.items():
            m = metrics.get("agency")
            rate = f"{m.success_rate * 100:.0f}%" if m and m.tasks_assigned else "—"
            assigned = str(m.tasks_assigned) if m else "0"
            rows.append((name, agent.role, agent.description[:60], "agency", assigned, rate))

    # Built-in roles (fallback)
    from bernstein.agents.catalog import _BUILTIN_AGENT_ENTRIES  # type: ignore[reportPrivateUsage]

    builtin_names = {r[0] for r in rows}
    for entry in _BUILTIN_AGENT_ENTRIES:
        if entry["role"] not in builtin_names:
            m = metrics.get("builtin")
            rate = f"{m.success_rate * 100:.0f}%" if m and m.tasks_assigned else "—"
            assigned = str(m.tasks_assigned) if m else "0"
            rows.append(
                (
                    entry["role"],
                    entry["role"],
                    entry.get("description", ""),
                    "builtin",
                    assigned,
                    rate,
                )
            )

    if not rows:
        console.print("[dim]No agents found. Run [bold]bernstein agents sync[/bold] first.[/dim]")
        return

    # Sort by source priority then role
    source_order = {"agency": 0, "local": 1, "builtin": 2}
    rows.sort(key=lambda r: (source_order.get(r[3], 9), r[1], r[0]))

    # Identify "featured" agents — top success rates with >= 3 tasks
    top_sources = {m.source for m in discovery.top_sources(min_tasks=3)}

    table = Table(
        title="Agent Showcase",
        show_lines=False,
        header_style="bold cyan",
        expand=False,
    )
    table.add_column("Name", min_width=22)
    table.add_column("Role", min_width=14)
    table.add_column("Description", min_width=40)
    table.add_column("Source", min_width=8)
    table.add_column("Tasks", min_width=6, justify="right")
    table.add_column("Success", min_width=8, justify="right")

    for name, role, desc, src, assigned, rate in rows:
        src_color = {"agency": "magenta", "local": "cyan", "builtin": "dim"}.get(src, "white")
        star = " ★" if src in top_sources else ""
        name_text = f"[bold]{name}[/bold]{star}" if star else name
        table.add_row(
            name_text,
            role,
            desc or "[dim]—[/dim]",
            f"[{src_color}]{src}[/{src_color}]",
            assigned,
            rate,
        )

    console.print(table)

    # Summary line
    total = discovery.total_agents or len(rows)
    console.print(f"\n[dim]{len(rows)} agent(s) shown · {total} total across all directories[/dim]")
    if top_sources:
        console.print(f"[dim]★ Featured sources (≥3 tasks, highest success): {', '.join(sorted(top_sources))}[/dim]")

    # Discovery hints
    console.print()
    console.print("[dim]Discover more agents:[/dim]")
    console.print("[dim]  bernstein agents discover         # scan local + project dirs[/dim]")
    console.print("[dim]  bernstein agents discover --net   # also search GitHub & npm[/dim]")


@agents_group.command("match")
@click.option("--role", required=True, help="Agent role to match (e.g. security, backend, qa).")
@click.option("--task", "task_description", default="", help="Task description for fuzzy matching.")
def agents_match(role: str, task_description: str) -> None:
    """Show which agent would be selected for a given role.

    \b
    Example:
      bernstein agents match --role security
      bernstein agents match --role backend --task "add rate limiting middleware"
    """
    from bernstein.agents.catalog import CatalogRegistry

    # Load from agency catalog if available
    registry = CatalogRegistry.default()
    agency_dir = Path(".sdd/agents/agency")
    if agency_dir.exists():
        from bernstein.core.agency_loader import load_agency_catalog

        catalog = load_agency_catalog(agency_dir)
        registry.load_from_agency(catalog)

    match = registry.match(role, task_description)
    if match is None:
        console.print(f"[yellow]No catalog agent found for role '[bold]{role}[/bold]'.[/yellow]")
        console.print("[dim]Built-in role template will be used.[/dim]")
        return

    from rich.panel import Panel
    from rich.text import Text as RichText

    t = RichText()
    t.append("  Role      ", style="dim")
    t.append(f"{match.role}\n", style="bold")
    t.append("  Name      ", style="dim")
    t.append(f"{match.name}\n", style="bold cyan")
    t.append("  ID        ", style="dim")
    t.append(f"{match.id or '—'}\n")
    t.append("  Source    ", style="dim")
    t.append(f"{match.source}\n")
    t.append("  Priority  ", style="dim")
    t.append(f"{match.priority}\n")
    t.append("  Tools     ", style="dim")
    t.append(", ".join(match.tools) if match.tools else "—")
    t.append("\n\n")
    t.append("  Description\n", style="dim")
    t.append(f"    {match.description[:120]}\n")

    console.print(Panel(t, title=f"[bold]Agent match: {role}[/bold]", border_style="cyan"))


@agents_group.command("discover")
@click.option("--net", "include_network", is_flag=True, default=False, help="Also search GitHub and npm.")
def agents_discover(include_network: bool) -> None:
    """Scan known sources for agent directories and update the registry.

    \b
    Scans:
      ~/.bernstein/agents/     user-level definitions
      .sdd/agents/local/       project-level definitions
      GitHub (--net)           repos tagged bernstein-agents
      npm (--net)              packages with bernstein-agent keyword
    """
    from bernstein.agents.discovery import AgentDiscovery

    discovery = AgentDiscovery.load()

    console.print("[bold]Discovering agent directories…[/bold]\n")
    results = discovery.full_sync(include_network=include_network)

    for source, count in results.items():
        icon = "[green]✓[/green]" if count >= 0 else "[yellow]![/yellow]"
        console.print(f"  {icon} [cyan]{source}[/cyan]  {count} agent(s)")

    if include_network:
        gh_entries = [d for d in discovery.directories if d.source_type == "github"]
        npm_entries = [d for d in discovery.directories if d.source_type == "npm"]
        if gh_entries:
            console.print(f"\n  [magenta]GitHub[/magenta] ({len(gh_entries)} repos)")
            for e in gh_entries[:5]:
                console.print(f"    [dim]{e.name}[/dim]  {e.url}")
        if npm_entries:
            console.print(f"\n  [magenta]npm[/magenta] ({len(npm_entries)} packages)")
            for e in npm_entries[:5]:
                console.print(f"    [dim]{e.name}[/dim]  {e.url}")

    console.print(f"\n[green]Done.[/green] Registry: [dim]{discovery.registry_path}[/dim]")
    console.print(f"[dim]Total agents tracked: {discovery.total_agents}[/dim]")
