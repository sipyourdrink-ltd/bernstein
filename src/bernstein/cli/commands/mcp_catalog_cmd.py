"""CLI surface for ``bernstein mcp catalog`` (release 1.9).

Subcommands::

    bernstein mcp catalog browse
    bernstein mcp catalog search <q>
    bernstein mcp catalog install <id> [--yes]
    bernstein mcp catalog list-installed
    bernstein mcp catalog upgrade <id>
    bernstein mcp catalog upgrade --all
    bernstein mcp catalog info <id>
    bernstein mcp catalog status

Every fetch / install / upgrade / uninstall emits a HMAC-chained audit
event under the existing ``.sdd/audit/`` log. Unverified manifests
(``verified_by_bernstein=false``) trigger a prominent warning before
the install preview runs.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import click

from bernstein.cli.helpers import console
from bernstein.core.protocols.mcp_catalog import (
    DEFAULT_CHECK_INTERVAL_SECONDS,
    DEFAULT_REVALIDATE_SECONDS,
    CatalogAuditor,
    CatalogFetcher,
    CatalogService,
    CatalogServiceConfig,
    CatalogValidationError,
    InstallPreview,
    default_cache_path,
    default_user_config_path,
)

logger = logging.getLogger(__name__)


def _audit_dir() -> Path:
    """Return the project-local audit directory."""
    override = os.environ.get("BERNSTEIN_MCP_CATALOG_AUDIT_DIR")
    if override:
        return Path(override)
    return Path.cwd() / ".sdd" / "audit"


def _check_interval_seconds() -> int:
    """Resolve ``mcp.catalog.check_interval`` from env or defaults."""
    raw = os.environ.get("BERNSTEIN_MCP_CATALOG_CHECK_INTERVAL")
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return DEFAULT_CHECK_INTERVAL_SECONDS


def _revalidate_seconds() -> int:
    """Resolve the cache revalidation window from env or defaults."""
    raw = os.environ.get("BERNSTEIN_MCP_CATALOG_REVALIDATE_INTERVAL")
    if raw:
        try:
            return max(1, int(raw))
        except ValueError:
            pass
    return DEFAULT_REVALIDATE_SECONDS


def _build_service() -> CatalogService:
    """Construct a :class:`CatalogService` wired to host paths."""
    cache_path = (
        Path(os.environ["BERNSTEIN_MCP_CATALOG_CACHE_PATH"])
        if os.environ.get("BERNSTEIN_MCP_CATALOG_CACHE_PATH")
        else default_cache_path()
    )
    user_config = (
        Path(os.environ["BERNSTEIN_MCP_USER_CONFIG_PATH"])
        if os.environ.get("BERNSTEIN_MCP_USER_CONFIG_PATH")
        else default_user_config_path()
    )
    fetcher = CatalogFetcher(
        cache_path=cache_path,
        revalidate_seconds=_revalidate_seconds(),
    )
    auditor = CatalogAuditor(audit_dir=_audit_dir())
    config = CatalogServiceConfig(
        check_interval_seconds=_check_interval_seconds(),
        user_config_path=user_config,
    )
    return CatalogService(
        fetcher=fetcher,
        user_config_path=user_config,
        auditor=auditor,
        config=config,
        confirm_callback=_prompt_confirm,
    )


def _prompt_confirm(preview: InstallPreview) -> bool:
    """Default confirmation gate using a Click prompt."""
    return click.confirm("Apply this install to the user MCP config?", default=False)


def _render_preview(preview: InstallPreview) -> None:
    """Render a sandboxed dry-run preview in the console."""
    status = "ok" if preview.succeeded else f"failed (exit={preview.exit_code})"
    console.print(
        f"[bold]Sandbox preview[/bold] {status} in {preview.duration_seconds:.2f}s; {len(preview.diff)} file change(s)"
    )
    if preview.timed_out:
        console.print("[red]Preview timed out before completing.[/red]")
    if preview.diff:
        for change in preview.diff:
            console.print(f"  [{change.change_type}] {change.path} ({change.size_bytes}B)")
    if preview.stdout:
        console.print("[dim]stdout:[/dim]")
        console.print(preview.stdout.decode("utf-8", errors="replace"))
    if preview.stderr:
        console.print("[dim]stderr:[/dim]")
        console.print(preview.stderr.decode("utf-8", errors="replace"))


@click.group("catalog", invoke_without_command=False)
def catalog_group() -> None:
    """Browse, install, and upgrade MCP servers from the community catalog."""


@catalog_group.command("browse")
@click.option("--refresh", is_flag=True, help="Skip the freshness window.")
def browse_cmd(refresh: bool) -> None:
    """List every entry in the catalog."""
    from rich.table import Table

    service = _build_service()
    try:
        catalog = service.browse(force_refresh=refresh)
    except CatalogValidationError as exc:
        raise click.ClickException(f"Catalog rejected: {exc}") from exc
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc

    table = Table(title="MCP Catalog", header_style="bold cyan")
    table.add_column("ID")
    table.add_column("Name")
    table.add_column("Version")
    table.add_column("Verified")
    table.add_column("Transports")
    for entry in catalog.entries:
        table.add_row(
            entry.id,
            entry.name,
            entry.version_pin,
            "yes" if entry.verified_by_bernstein else "no",
            ",".join(entry.transports),
        )
    console.print(table)


@catalog_group.command("search")
@click.argument("query")
@click.option("--refresh", is_flag=True, help="Skip the freshness window.")
def search_cmd(query: str, refresh: bool) -> None:
    """Search the catalog by id / name / description substring."""
    service = _build_service()
    try:
        results = service.search(query, force_refresh=refresh)
    except CatalogValidationError as exc:
        raise click.ClickException(f"Catalog rejected: {exc}") from exc
    if not results:
        console.print(f"[yellow]No catalog matches for {query!r}.[/yellow]")
        return
    for entry in results:
        verified = "[green]verified[/green]" if entry.verified_by_bernstein else "[yellow]unverified[/yellow]"
        console.print(f"[bold]{entry.id}[/bold] ({entry.version_pin}) {verified} - {entry.name}: {entry.description}")


@catalog_group.command("info")
@click.argument("entry_id")
@click.option("--refresh", is_flag=True, help="Skip the freshness window.")
def info_cmd(entry_id: str, refresh: bool) -> None:
    """Show full info for a single catalog entry."""
    service = _build_service()
    entry = service.info(entry_id, force_refresh=refresh)
    if entry is None:
        raise click.ClickException(f"Catalog entry {entry_id!r} not found")
    console.print(f"[bold]{entry.id}[/bold] - {entry.name} ({entry.version_pin})")
    console.print(f"Description: {entry.description}")
    console.print(f"Homepage:    {entry.homepage}")
    console.print(f"Repository:  {entry.repository}")
    console.print(f"Transports:  {', '.join(entry.transports)}")
    console.print(f"Verified by Bernstein: {'yes' if entry.verified_by_bernstein else 'no'}")
    console.print(f"Auto-upgrade: {'yes' if entry.auto_upgrade else 'no'}")
    console.print(f"Install command: {' '.join(entry.install_command)}")
    if entry.signature is not None:
        console.print(f"Signature: {entry.signature}")


@catalog_group.command("install")
@click.argument("entry_id")
@click.option("--yes", is_flag=True, help="Skip the confirmation prompt.")
@click.option("--refresh", is_flag=True, help="Skip the freshness window.")
def install_cmd(entry_id: str, yes: bool, refresh: bool) -> None:
    """Run the sandboxed dry-run preview and install on confirmation."""
    service = _build_service()
    try:
        catalog = service.browse(force_refresh=refresh)
    except CatalogValidationError as exc:
        raise click.ClickException(f"Catalog rejected: {exc}") from exc

    entry = catalog.find(entry_id)
    if entry is None:
        raise click.ClickException(f"Catalog entry {entry_id!r} not found")

    if not entry.verified_by_bernstein:
        console.print(
            f"[bold yellow]WARNING[/bold yellow] {entry.id} is not verified by "
            "Bernstein. Review the install command before continuing:"
        )
        console.print(f"  $ {' '.join(entry.install_command)}")

    outcome = service.install(entry_id, skip_confirmation=yes, force_refresh=False)
    _render_preview(outcome.preview)

    if outcome.installed is None:
        if not outcome.preview.succeeded:
            raise click.ClickException(
                f"Sandbox preview failed (exit {outcome.preview.exit_code}); host MCP config left untouched."
            )
        if not outcome.confirmed:
            console.print("[yellow]Install aborted by user.[/yellow]")
            return
    else:
        console.print(
            f"[green]Installed[/green] {outcome.installed.id} "
            f"({outcome.installed.version_pin}) into {service.user_config_path}"
        )


@catalog_group.command("list-installed")
def list_installed_cmd() -> None:
    """List entries Bernstein has installed into the user MCP config."""
    from rich.table import Table

    service = _build_service()
    try:
        rows = service.installed_with_catalog_state()
    except (CatalogValidationError, RuntimeError):
        rows = [(installed, None) for installed in service.list_installed()]

    if not rows:
        console.print("[dim]No MCP servers installed via catalog.[/dim]")
        return

    table = Table(title="Installed MCP servers", header_style="bold cyan")
    table.add_column("ID")
    table.add_column("Pinned")
    table.add_column("Installed at")
    table.add_column("Last upgrade check")
    table.add_column("In catalog")
    for installed, catalog_entry in rows:
        in_catalog = "yes" if catalog_entry is not None else "no"
        table.add_row(
            installed.id,
            installed.version_pin,
            installed.installed_at,
            installed.last_upgrade_check or "-",
            in_catalog,
        )
    console.print(table)


@catalog_group.command("upgrade")
@click.argument("entry_id", required=False)
@click.option("--all", "all_entries", is_flag=True, help="Upgrade all installed entries.")
@click.option("--yes", is_flag=True, help="Skip confirmation prompts.")
@click.option("--refresh", is_flag=True, help="Skip the freshness window.")
def upgrade_cmd(entry_id: str | None, all_entries: bool, yes: bool, refresh: bool) -> None:
    """Re-fetch the catalog and upgrade installed entries."""
    if not entry_id and not all_entries:
        raise click.ClickException("Provide an entry id or use --all")

    service = _build_service()
    try:
        if all_entries:
            outcomes = service.upgrade_all(skip_confirmation=yes, force_refresh=refresh)
        else:
            assert entry_id is not None
            outcomes = [service.upgrade(entry_id, skip_confirmation=yes, force_refresh=refresh)]
    except KeyError as exc:
        raise click.ClickException(str(exc)) from exc
    except CatalogValidationError as exc:
        raise click.ClickException(f"Catalog rejected: {exc}") from exc

    for outcome in outcomes:
        if outcome.applied:
            console.print(f"[green]Upgraded[/green] {outcome.entry_id}: {outcome.from_version} -> {outcome.to_version}")
        elif outcome.from_version == outcome.to_version:
            console.print(f"[dim]{outcome.entry_id} already on latest ({outcome.from_version}).[/dim]")
        else:
            console.print(f"[yellow]Skipped[/yellow] {outcome.entry_id} ({outcome.skipped_reason})")
        if outcome.preview is not None and not outcome.applied:
            _render_preview(outcome.preview)


@catalog_group.command("uninstall")
@click.argument("entry_id")
def uninstall_cmd(entry_id: str) -> None:
    """Remove an entry from the bernstein-managed block of the user config."""
    service = _build_service()
    if service.uninstall(entry_id):
        console.print(f"[green]Uninstalled[/green] {entry_id}")
    else:
        raise click.ClickException(f"{entry_id!r} is not installed")


@catalog_group.command("status")
def status_cmd() -> None:
    """Show cache + cadence + installed-count for ``mcp catalog``."""
    service = _build_service()
    status = service.status()
    console.print("[bold cyan]MCP Catalog Status[/bold cyan]")
    console.print(f"Cache:                {status.cache_path}")
    console.print(f"Last fetch:           {status.last_fetch_at or 'never'}")
    console.print(f"Next due:             {status.next_due_at}")
    console.print(f"Check interval (sec): {status.check_interval_seconds}  (BERNSTEIN_MCP_CATALOG_CHECK_INTERVAL)")
    console.print(f"Installed:            {status.installed_count}")
    console.print(f"Cache state:          {status.last_check_log}")


def maybe_run_background_check(*, on_serve_startup: bool = False) -> dict[str, Any]:
    """Trigger a non-blocking background catalog check.

    Called by ``bernstein mcp serve`` startup. Returns a small summary
    dict so the caller can log without re-reading the audit log.
    """
    service = _build_service()
    if on_serve_startup and not service.background_check_due():
        return {"checked": False, "reason": "cadence not elapsed"}
    try:
        service.browse(force_refresh=True)
    except (CatalogValidationError, RuntimeError) as exc:
        logger.warning("Background catalog check failed: %s", exc)
        return {"checked": False, "reason": str(exc)}
    return {"checked": True, "reason": "ok"}


__all__ = ["catalog_group", "maybe_run_background_check"]
