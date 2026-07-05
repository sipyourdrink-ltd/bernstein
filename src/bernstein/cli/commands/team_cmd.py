"""CLI surface for ``bernstein team`` (issue #2248).

Subcommands::

    bernstein team list
    bernstein team show <name>
    bernstein team drift [<name>]

``list`` and ``show`` inspect the named team manifests visible from the
workdir (project-local ``templates/teams/`` first, then the bundled
defaults). ``drift`` recomputes role template digests on disk and
compares them to the manifest's pinned ``role_template_digests`` - the
same lockfile-vs-disk UX as ``bernstein skills catalog sync``, plus a
non-zero exit code so CI can gate on it. A drift finding is also
recorded on the HMAC-chained audit log when one exists.
"""

from __future__ import annotations

import logging
from pathlib import Path

import click

from bernstein.cli.helpers import console
from bernstein.core.teams.audit import TeamManifestAuditor
from bernstein.core.teams.drift import detect_role_template_drift
from bernstein.core.teams.manifest import (
    TeamManifest,
    TeamManifestError,
    discover_team_manifest_paths,
    load_team_manifest,
    resolve_team_manifest,
)

logger = logging.getLogger(__name__)

_WORKDIR_OPTION = click.option(
    "--workdir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=".",
    help="Project root to resolve manifests and role templates from.",
)


@click.group("team", invoke_without_command=False)
def team_group() -> None:
    """Inspect named team manifests and detect role template drift."""


@team_group.command("list")
@_WORKDIR_OPTION
def list_cmd(workdir: Path) -> None:
    """List every team manifest visible from the workdir."""
    from rich.table import Table

    paths = discover_team_manifest_paths(workdir)
    if not paths:
        console.print("[dim]No team manifests found.[/dim]")
        return

    table = Table(title="Team manifests", header_style="bold cyan")
    table.add_column("Name")
    table.add_column("Version")
    table.add_column("Digest")
    table.add_column("Roles")
    table.add_column("Source")
    for name, path in paths.items():
        try:
            manifest = load_team_manifest(path)
        except TeamManifestError as exc:
            table.add_row(name, "-", "-", f"[red]invalid: {exc}[/red]", str(path))
            continue
        table.add_row(
            manifest.name,
            manifest.version,
            manifest.digest()[:12] + "...",
            ", ".join(role.role for role in manifest.roles),
            str(path),
        )
    console.print(table)


@team_group.command("show")
@click.argument("name")
@_WORKDIR_OPTION
def show_cmd(name: str, workdir: Path) -> None:
    """Show one manifest: digest, roles, policies, and pinned templates."""
    manifest = _resolve_or_fail(name, workdir)

    console.print(f"[bold]{manifest.name}[/bold] ({manifest.version})")
    console.print(f"Digest:       {manifest.digest()}")
    console.print(f"Source:       {manifest.source_path}")
    console.print(
        f"Coordination: parallelism={manifest.coordination.parallelism} "
        f"review_chain={'yes' if manifest.coordination.review_chain else 'no'}"
    )
    console.print("Roles:")
    for role in manifest.roles:
        parts = [f"{key}={value}" for key, value in sorted(role.model_policy.items())]
        if role.response_profile is not None:
            parts.append(f"response_profile={role.response_profile}")
        detail = ", ".join(parts) if parts else "(role defaults)"
        console.print(f"  - {role.role}: {detail}")
    if manifest.role_template_digests:
        console.print("Pinned role templates:")
        for role_name in sorted(manifest.role_template_digests):
            console.print(f"  - {role_name}: {manifest.role_template_digests[role_name]}")
    else:
        console.print("Pinned role templates: (none)")
    console.print(f"\nReference:    team_manifest: {manifest.name}@{manifest.digest()}")


@team_group.command("drift")
@click.argument("name", required=False)
@_WORKDIR_OPTION
def drift_cmd(name: str | None, workdir: Path) -> None:
    """Compare pinned role template digests to the on-disk templates.

    With NAME, checks one manifest; without, checks every visible
    manifest. Exits 1 when any pinned role template diverged.
    """
    if name is not None:
        manifests = [_resolve_or_fail(name, workdir)]
    else:
        manifests = []
        for manifest_path in discover_team_manifest_paths(workdir).values():
            try:
                manifests.append(load_team_manifest(manifest_path))
            except TeamManifestError as exc:
                console.print(f"[yellow]skipping invalid manifest {manifest_path}: {exc}[/yellow]")

    any_drift = False
    for manifest in manifests:
        drift = detect_role_template_drift(manifest, workdir=workdir)
        if not drift:
            console.print(f"[green]{manifest.name}: no drift detected[/green]")
            continue
        any_drift = True
        console.print(f"[yellow]{manifest.name}: drift detected[/yellow] in {len(drift)} role template(s):")
        for role, (pinned, actual) in sorted(drift.items()):
            actual_label = actual if actual == "<missing>" else actual[:12] + "..."
            console.print(f"  - {role}: pinned {pinned[:12]}... vs on-disk {actual_label}")
        _record_drift(workdir, manifest, sorted(drift))

    if any_drift:
        raise SystemExit(1)


def _resolve_or_fail(name: str, workdir: Path) -> TeamManifest:
    """Resolve *name* or raise a clean click error."""
    try:
        return resolve_team_manifest(name, workdir=workdir)
    except TeamManifestError as exc:
        raise click.ClickException(str(exc)) from exc


def _record_drift(workdir: Path, manifest: TeamManifest, drifted_roles: list[str]) -> None:
    """Best-effort drift event on the audit chain, if one exists."""
    audit_dir = workdir / ".sdd" / "audit"
    if not audit_dir.is_dir():
        return
    TeamManifestAuditor(audit_dir=audit_dir).drift(
        name=manifest.name,
        digest=manifest.digest(),
        drifted_roles=drifted_roles,
    )


__all__ = ["team_group"]
