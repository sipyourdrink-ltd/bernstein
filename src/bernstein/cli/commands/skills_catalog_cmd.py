"""CLI surface for ``bernstein skills catalog`` (issue #1796).

Subcommands::

    bernstein skills catalog browse
    bernstein skills catalog list
    bernstein skills catalog search <q>
    bernstein skills catalog install <id> [--allow-unverified]
    bernstein skills catalog list-installed
    bernstein skills catalog upgrade <id>
    bernstein skills catalog upgrade --all
    bernstein skills catalog info <id>
    bernstein skills catalog uninstall <id>
    bernstein skills catalog sync
    bernstein skills catalog status

Modelled on the existing ``bernstein mcp catalog`` surface
(:mod:`bernstein.cli.commands.mcp_catalog_cmd`). Every install / upgrade
emits a signed HMAC-chained audit event under the existing
``.sdd/audit/`` log.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import click

from bernstein.cli.helpers import console
from bernstein.core.skills.catalog import (
    DEFAULT_REVALIDATE_SECONDS,
    ManifestSignatureError,
    SkillCatalogAuditor,
    SkillCatalogError,
    SkillCatalogFetcher,
    SkillCatalogService,
    SkillCatalogServiceConfig,
    SkillCatalogValidationError,
    default_cache_path,
    env_ttl_seconds,
)
from bernstein.core.skills.lifecycle import InstallScope

logger = logging.getLogger(__name__)


def _audit_dir() -> Path:
    """Return the project-local audit directory."""
    override = os.environ.get("BERNSTEIN_SKILLS_CATALOG_AUDIT_DIR")
    if override:
        return Path(override)
    return Path.cwd() / ".sdd" / "audit"


def _parse_scope(scope_str: str) -> InstallScope:
    """Coerce the ``--scope`` CLI flag into an :class:`InstallScope`."""
    try:
        return InstallScope(scope_str)
    except ValueError as exc:
        raise click.BadParameter(f"unknown scope {scope_str!r}; expected project or user") from exc


def _build_service(scope_str: str) -> SkillCatalogService:
    """Construct a :class:`SkillCatalogService` wired to host paths."""
    scope = _parse_scope(scope_str)
    fetcher = SkillCatalogFetcher(
        cache_path=default_cache_path(),
        revalidate_seconds=env_ttl_seconds(DEFAULT_REVALIDATE_SECONDS),
    )
    auditor = SkillCatalogAuditor(audit_dir=_audit_dir())
    config = SkillCatalogServiceConfig(
        workdir=Path.cwd(),
        scope=scope,
    )
    return SkillCatalogService(
        fetcher=fetcher,
        auditor=auditor,
        config=config,
    )


@click.group("catalog", invoke_without_command=False)
def catalog_group() -> None:
    """Browse, install, and upgrade skill packs from the community catalog."""


@catalog_group.command("browse")
@click.option("--refresh", is_flag=True, help="Skip the freshness window.")
@click.option(
    "--scope",
    type=click.Choice(["project", "user"]),
    default="project",
    help="Install scope used for status checks.",
)
def browse_cmd(refresh: bool, scope: str) -> None:
    """List every entry in the skill catalog."""
    from rich.table import Table

    service = _build_service(scope)
    try:
        catalog = service.browse(force_refresh=refresh)
    except SkillCatalogValidationError as exc:
        raise click.ClickException(f"Catalog rejected: {exc}") from exc
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc

    if not catalog.entries:
        console.print("[dim]Skill catalog is empty.[/dim]")
        return

    table = Table(title="Skill catalog", header_style="bold cyan")
    table.add_column("ID")
    table.add_column("Name")
    table.add_column("Version")
    table.add_column("Verified")
    table.add_column("Source")
    table.add_column("Tags")
    for entry in catalog.entries:
        table.add_row(
            entry.id,
            entry.name,
            entry.version,
            "yes" if entry.verified else "no",
            entry.source.kind,
            ",".join(entry.tags) or "-",
        )
    console.print(table)


@catalog_group.command("list")
@click.option(
    "--scope",
    type=click.Choice(["project", "user"]),
    default="project",
)
def list_cmd(scope: str) -> None:
    """List catalog entries currently installed via the lockfile."""
    from rich.table import Table

    rows = _build_service(scope).list_installed()
    if not rows:
        console.print("[dim]No skills installed via catalog.[/dim]")
        return

    table = Table(title="Installed catalog skills", header_style="bold cyan")
    table.add_column("ID")
    table.add_column("Version")
    table.add_column("Manifest SHA")
    table.add_column("Content digest")
    table.add_column("Installed at")
    for row in rows:
        table.add_row(
            row.id,
            row.version,
            row.manifest_sha256[:12] + "...",
            row.content_digest[:12] + "...",
            row.installed_at,
        )
    console.print(table)


@catalog_group.command("search")
@click.argument("query")
@click.option("--refresh", is_flag=True, help="Skip the freshness window.")
@click.option(
    "--scope",
    type=click.Choice(["project", "user"]),
    default="project",
)
def search_cmd(query: str, refresh: bool, scope: str) -> None:
    """Search the catalog by id / name / description / tags substring."""
    service = _build_service(scope)
    try:
        results = service.search(query, force_refresh=refresh)
    except SkillCatalogValidationError as exc:
        raise click.ClickException(f"Catalog rejected: {exc}") from exc

    if not results:
        console.print(f"[yellow]No catalog matches for {query!r}.[/yellow]")
        return
    for entry in results:
        verified = "[green]verified[/green]" if entry.verified else "[yellow]unverified[/yellow]"
        console.print(
            f"[bold]{entry.id}[/bold] ({entry.version}) {verified} - {entry.name}: {entry.description}",
        )


@catalog_group.command("info")
@click.argument("entry_id")
@click.option("--refresh", is_flag=True, help="Skip the freshness window.")
@click.option(
    "--scope",
    type=click.Choice(["project", "user"]),
    default="project",
)
def info_cmd(entry_id: str, refresh: bool, scope: str) -> None:
    """Show full info for a single catalog entry."""
    entry = _build_service(scope).info(entry_id, force_refresh=refresh)
    if entry is None:
        raise click.ClickException(f"Catalog entry {entry_id!r} not found")

    console.print(f"[bold]{entry.id}[/bold] - {entry.name} ({entry.version})")
    console.print(f"Description:     {entry.description}")
    if entry.homepage:
        console.print(f"Homepage:        {entry.homepage}")
    console.print(f"Source:          {entry.source.kind}")
    console.print(f"Manifest URL:    {entry.source.url_for_audit()}")
    console.print(f"Content digest:  {entry.content_digest}")
    console.print(f"Verified:        {'yes' if entry.verified else 'no'}")
    if entry.tags:
        console.print(f"Tags:            {', '.join(entry.tags)}")
    if entry.signature is not None:
        console.print(f"Signature:       {entry.signature[:32]}...")
    else:
        console.print("Signature:       (none)")


@catalog_group.command("install")
@click.argument("entry_id")
@click.option(
    "--allow-unverified",
    is_flag=True,
    default=False,
    help="Install even if the manifest signature does not verify.",
)
@click.option("--refresh", is_flag=True, help="Skip the freshness window.")
@click.option(
    "--scope",
    type=click.Choice(["project", "user"]),
    default="project",
)
def install_cmd(entry_id: str, allow_unverified: bool, refresh: bool, scope: str) -> None:
    """Install a catalog entry into the active scope."""
    service = _build_service(scope)
    try:
        outcome = service.install(
            entry_id,
            allow_unverified=allow_unverified,
            force_refresh=refresh,
        )
    except ManifestSignatureError as exc:
        raise click.ClickException(
            f"Refusing to install {entry_id!r}: {exc}. Re-run with --allow-unverified to override.",
        ) from exc
    except SkillCatalogError as exc:
        raise click.ClickException(str(exc)) from exc

    verified_marker = "[green]verified[/green]" if outcome.verified else "[yellow]unverified[/yellow]"
    console.print(
        f"[green]installed[/green] {outcome.entry_id} ({outcome.version}) {verified_marker}",
    )
    console.print(f"  install_dir:   {outcome.install_dir}")
    console.print(f"  manifest_url:  {outcome.manifest_url}")
    console.print(f"  manifest_sha:  {outcome.manifest_sha256}")
    console.print(f"  content_digest:{outcome.content_digest}")
    console.print(f"  chain_head:    {outcome.chain_head}")
    if outcome.verification_reason:
        console.print(f"  warning:       {outcome.verification_reason}")


@catalog_group.command("list-installed")
@click.option(
    "--scope",
    type=click.Choice(["project", "user"]),
    default="project",
)
def list_installed_cmd(scope: str) -> None:
    """Alias of ``bernstein skills catalog list``."""
    list_cmd.callback(scope)  # type: ignore[misc]


@catalog_group.command("upgrade")
@click.argument("entry_id", required=False)
@click.option("--all", "all_entries", is_flag=True, help="Upgrade all installed entries.")
@click.option(
    "--allow-unverified",
    is_flag=True,
    default=False,
    help="Upgrade even when the manifest signature does not verify.",
)
@click.option("--refresh", is_flag=True, help="Skip the freshness window.")
@click.option(
    "--scope",
    type=click.Choice(["project", "user"]),
    default="project",
)
def upgrade_cmd(
    entry_id: str | None,
    all_entries: bool,
    allow_unverified: bool,
    refresh: bool,
    scope: str,
) -> None:
    """Upgrade installed catalog entries."""
    if not entry_id and not all_entries:
        raise click.ClickException("Provide an entry id or use --all")

    service = _build_service(scope)
    try:
        if all_entries:
            outcomes = service.upgrade_all(
                allow_unverified=allow_unverified,
                force_refresh=refresh,
            )
        else:
            assert entry_id is not None
            outcomes = [
                service.upgrade(
                    entry_id,
                    allow_unverified=allow_unverified,
                    force_refresh=refresh,
                )
            ]
    except ManifestSignatureError as exc:
        raise click.ClickException(str(exc)) from exc
    except SkillCatalogError as exc:
        raise click.ClickException(str(exc)) from exc

    for outcome in outcomes:
        if outcome.applied:
            console.print(
                f"[green]upgraded[/green] {outcome.entry_id}: {outcome.from_version} -> {outcome.to_version}",
            )
        elif outcome.from_version == outcome.to_version and not outcome.skipped_reason:
            console.print(f"[dim]{outcome.entry_id} already on latest ({outcome.from_version}).[/dim]")
        else:
            console.print(f"[yellow]skipped[/yellow] {outcome.entry_id} ({outcome.skipped_reason})")


@catalog_group.command("uninstall")
@click.argument("entry_id")
@click.option(
    "--scope",
    type=click.Choice(["project", "user"]),
    default="project",
)
def uninstall_cmd(entry_id: str, scope: str) -> None:
    """Remove a catalog-installed skill and its lockfile row."""
    service = _build_service(scope)
    if service.uninstall(entry_id):
        console.print(f"[green]uninstalled[/green] {entry_id}")
    else:
        raise click.ClickException(f"{entry_id!r} is not installed")


@catalog_group.command("sync")
@click.option(
    "--scope",
    type=click.Choice(["project", "user"]),
    default="project",
)
def sync_cmd(scope: str) -> None:
    """Detect lockfile vs on-disk drift."""
    drift = _build_service(scope).sync()
    if not drift:
        console.print("[green]no drift detected[/green]")
        return
    console.print(f"[yellow]drift detected[/yellow] in {len(drift)} skill(s):")
    for entry_id, (locked, actual) in drift.items():
        console.print(f"  - {entry_id}: lockfile {locked[:12]}... vs installed {actual[:12]}...")


@catalog_group.command("verify")
@click.option("--refresh", is_flag=True, help="Skip the freshness window.")
@click.option(
    "--scope",
    type=click.Choice(["project", "user"]),
    default="project",
)
def verify_cmd(refresh: bool, scope: str) -> None:
    """Verify the catalog offline: transitive pins, inclusion proofs, revocations.

    Fails (non-zero exit) when any entry references an unpinned transitive
    source, an inclusion or consistency proof does not verify against the
    signed log head, or an installed version is under a signed revocation.
    """
    service = _build_service(scope)
    try:
        report = service.verify(force_refresh=refresh)
    except SkillCatalogValidationError as exc:
        raise click.ClickException(f"Catalog rejected: {exc}") from exc
    except RuntimeError as exc:
        raise click.ClickException(str(exc)) from exc

    for result in report.results:
        marker = "[green]ok[/green]" if result.ok else "[red]FAIL[/red]"
        console.print(f"{marker} {result.entry_id} ({result.version}): {result.detail}")

    if report.revoked:
        console.print(f"[red]revoked installs:[/red] {', '.join(report.revoked)}")

    if report.ok:
        console.print(f"[green]verified[/green] {report.entries_checked} entr(y/ies) offline")
    else:
        raise click.ClickException(
            f"catalog verification failed: {len(report.failures())} entr(y/ies) did not verify",
        )


@catalog_group.command("status")
@click.option(
    "--scope",
    type=click.Choice(["project", "user"]),
    default="project",
)
def status_cmd(scope: str) -> None:
    """Show cache + lockfile state for ``skills catalog``."""
    status = _build_service(scope).status()
    console.print("[bold cyan]Skill catalog status[/bold cyan]")
    console.print(f"Cache:                {status.cache_path}")
    console.print(f"Last fetch:           {status.last_fetch_at or 'never'}")
    console.print(f"Revalidate seconds:   {status.revalidate_seconds}  ({'BERNSTEIN_SKILLS_CATALOG_TTL'})")
    console.print(f"Installed (catalog):  {status.installed_count}")
    console.print(f"Drift count:          {status.drift_count}")
    console.print(f"Lockfile:             {status.lockfile_path}")
    console.print(f"Lockfile digest:      {status.lockfile_digest}")


__all__ = ["catalog_group"]
