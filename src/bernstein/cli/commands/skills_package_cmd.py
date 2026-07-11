"""CLI surface for ``bernstein skills package`` (issue #2369).

Subcommands::

    bernstein skills package show
    bernstein skills package install [--host H --scope S | --dest DIR]
                                     [--record-only] [--force]
    bernstein skills package verify [--host H --scope S | --dest DIR]

``install`` copies the bundled cross-vendor ``bernstein-run`` skill into an
agent host's skill directory and anchors a content-addressed install
receipt in the ``skills`` lineage spine and the HMAC audit chain (the same
receipt machinery the signed skills catalog uses). ``--record-only``
anchors a tree the host already installed - e.g. a plugin checkout -
without writing to it. ``verify`` recomputes the installed tree's content
address and proves it against the anchored receipt.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import click

from bernstein.cli.helpers import console
from bernstein.core.skills.packaging import (
    PACKAGED_SKILL_NAME,
    PackagedInstallError,
    discover_installs,
    host_skill_parent,
    install_packaged_skill,
    manifest_hash_for,
    packaged_skill_dir,
    supported_hosts,
    tree_content_hash,
    update_packaged_install,
    verify_packaged_install,
)


def _load_hmac_key() -> bytes:
    from bernstein.core.security.audit import load_or_create_audit_key

    return load_or_create_audit_key()


def _resolve_dest(
    *,
    host: str | None,
    scope: str,
    dest: str | None,
    workdir: Path,
) -> tuple[Path, str, str]:
    """Return ``(dest, host_label, scope_label)`` from the CLI selection."""
    if dest is not None:
        return Path(dest), host or "dest", "dest" if host is None else scope
    if host is None:
        raise click.ClickException("Provide --host (with optional --scope) or an explicit --dest.")
    try:
        parent = host_skill_parent(host, scope, workdir=workdir)
    except PackagedInstallError as exc:
        raise click.ClickException(str(exc)) from exc
    return parent / PACKAGED_SKILL_NAME, host, scope


@click.group("package")
def package_group() -> None:
    """Install and verify the packaged bernstein agent skill.

    \b
      bernstein skills package show                       # bundled asset identity
      bernstein skills package install --host claude      # copy + anchor receipt
      bernstein skills package verify --host claude       # recompute + prove
    """


@package_group.command("show")
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    show_default=True,
    help="Project root containing .sdd/.",
)
def show_cmd(workdir: str) -> None:
    """Print the bundled skill's content address and manifest hash."""
    try:
        skill = packaged_skill_dir()
        skill_hash = tree_content_hash(skill)
        manifest_rel, manifest_hash = manifest_hash_for(skill)
    except PackagedInstallError as exc:
        raise click.ClickException(str(exc)) from exc

    console.print()
    console.print(f"[bold]Packaged skill[/bold] {PACKAGED_SKILL_NAME}")
    console.print(f"  source:        {skill}")
    console.print(f"  skill_hash:    {skill_hash}")
    console.print(f"  manifest:      {manifest_rel}")
    console.print(f"  manifest_sha:  {manifest_hash}")
    console.print(f"  hosts:         {', '.join(supported_hosts())}")


@package_group.command("install")
@click.option(
    "--host",
    type=click.Choice(sorted(supported_hosts())),
    default=None,
    help="Agent host whose default skills directory receives the install.",
)
@click.option(
    "--scope",
    type=click.Choice(["project", "user"]),
    default="project",
    show_default=True,
    help="Install into the project tree or the user home directory.",
)
@click.option(
    "--dest",
    type=click.Path(file_okay=False),
    default=None,
    help="Explicit destination directory (overrides --host/--scope).",
)
@click.option(
    "--record-only",
    is_flag=True,
    default=False,
    help="Anchor an already-installed tree at the destination without copying.",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Overwrite a destination whose content differs from the bundled skill.",
)
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    show_default=True,
    help="Project root containing .sdd/ (where the receipt is anchored).",
)
def install_cmd(
    host: str | None,
    scope: str,
    dest: str | None,
    record_only: bool,
    force: bool,
    workdir: str,
) -> None:
    """Install the packaged skill and anchor a chain-verifiable receipt.

    Exit codes: 0 = installed and anchored, 1 = error.
    """
    root = Path(workdir).resolve()
    target, host_label, scope_label = _resolve_dest(host=host, scope=scope, dest=dest, workdir=root)
    install_id = f"agent-plugin-{host_label}-{scope_label}"

    try:
        outcome = install_packaged_skill(
            workdir=root,
            dest=target,
            hmac_key=_load_hmac_key(),
            install_id=install_id,
            timestamp=int(datetime.now(tz=UTC).timestamp()),
            host=host_label,
            scope=scope_label,
            force=force,
            record_only=record_only,
        )
    except PackagedInstallError as exc:
        raise click.ClickException(str(exc)) from exc

    action = "anchored" if not outcome.copied else "installed"
    console.print(f"[green]{action}[/green] {PACKAGED_SKILL_NAME} -> {outcome.dest}")
    console.print(f"  skill_hash:    {outcome.skill_hash}")
    console.print(f"  manifest:      {outcome.manifest_path}")
    console.print(f"  manifest_sha:  {outcome.manifest_hash}")
    console.print(f"  install_id:    {outcome.install_id}")
    console.print(f"  spine_anchor:  {outcome.spine_anchor}")


@package_group.command("verify")
@click.option(
    "--host",
    type=click.Choice(sorted(supported_hosts())),
    default=None,
    help="Agent host whose default skills directory is verified.",
)
@click.option(
    "--scope",
    type=click.Choice(["project", "user"]),
    default="project",
    show_default=True,
)
@click.option(
    "--dest",
    type=click.Path(file_okay=False),
    default=None,
    help="Explicit installed directory (overrides --host/--scope).",
)
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    show_default=True,
    help="Project root containing .sdd/.",
)
def verify_cmd(host: str | None, scope: str, dest: str | None, workdir: str) -> None:
    """Recompute the installed tree's receipt and verify its anchor.

    Exit codes: 0 = verified, 1 = bad input (missing directory),
    2 = attestation failure (no receipt for the recomputed content
    address, install spine tamper, or manifest drift).
    """
    root = Path(workdir).resolve()
    target, _, _ = _resolve_dest(host=host, scope=scope, dest=dest, workdir=root)
    if not target.is_dir():
        console.print(f"[red]No installed tree at[/red] {target}")
        raise SystemExit(1)

    result = verify_packaged_install(workdir=root, dest=target, hmac_key=_load_hmac_key())
    console.print()
    console.print(f"[bold]Packaged install verify[/bold] dest={target}")
    if result.ok:
        console.print("[green]OK[/green] -- content address matches an anchored install receipt.")
        raise SystemExit(0)
    console.print(f"[red]FAILED[/red] -- {result.reason}")
    raise SystemExit(2)


@package_group.command("update")
@click.option(
    "--host",
    type=click.Choice(sorted(supported_hosts())),
    default=None,
    help="Agent host whose default skills directory is updated in place.",
)
@click.option(
    "--scope",
    type=click.Choice(["project", "user"]),
    default="project",
    show_default=True,
)
@click.option(
    "--dest",
    type=click.Path(file_okay=False),
    default=None,
    help="Explicit installed directory (overrides --host/--scope).",
)
@click.option(
    "--source",
    type=click.Path(file_okay=False, exists=True),
    default=None,
    help="Tree to update to (defaults to the bundled skill).",
)
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    show_default=True,
    help="Project root containing .sdd/ (where the receipt is anchored).",
)
def update_cmd(
    host: str | None,
    scope: str,
    dest: str | None,
    source: str | None,
    workdir: str,
) -> None:
    """Supersede a prior install and anchor a chain-verifiable update receipt.

    The update binds the prior content address to the new one, so the
    supersession chains back to the root install. Exit codes: 0 = updated or
    already current, 1 = error (missing or unattested install).
    """
    root = Path(workdir).resolve()
    target, host_label, scope_label = _resolve_dest(host=host, scope=scope, dest=dest, workdir=root)
    install_id = f"agent-plugin-{host_label}-{scope_label}"

    try:
        outcome = update_packaged_install(
            workdir=root,
            dest=target,
            source=Path(source) if source is not None else None,
            hmac_key=_load_hmac_key(),
            install_id=install_id,
            timestamp=int(datetime.now(tz=UTC).timestamp()),
            host=host_label,
            scope=scope_label,
        )
    except PackagedInstallError as exc:
        raise click.ClickException(str(exc)) from exc

    if not outcome.changed:
        console.print(f"[green]already current[/green] {PACKAGED_SKILL_NAME} -> {outcome.dest}")
        console.print(f"  skill_hash:    {outcome.skill_hash}")
        return

    console.print(f"[green]updated[/green] {PACKAGED_SKILL_NAME} -> {outcome.dest}")
    console.print(f"  prior_hash:    {outcome.prior_skill_hash}")
    console.print(f"  skill_hash:    {outcome.skill_hash}")
    console.print(f"  manifest:      {outcome.manifest_path}")
    console.print(f"  manifest_sha:  {outcome.manifest_hash}")
    console.print(f"  install_id:    {outcome.install_id}")
    console.print(f"  spine_anchor:  {outcome.spine_anchor}")


@package_group.command("status")
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit the install status as JSON.",
)
@click.option(
    "--home",
    type=click.Path(file_okay=False),
    default=None,
    help="Home directory for user-scoped destinations (defaults to the real home).",
)
@click.option(
    "--workdir",
    "-w",
    type=click.Path(file_okay=False, exists=True),
    default=".",
    show_default=True,
    help="Project root containing .sdd/.",
)
def status_cmd(as_json: bool, home: str | None, workdir: str) -> None:
    """Verify every default host/scope install and report each verdict.

    Scans the default skill directory for each supported host and scope,
    re-hashes any present tree, and proves it against its anchored receipt.
    Exit codes: 0 = every present install verifies (or none present),
    2 = at least one present install failed verification.
    """
    root = Path(workdir).resolve()
    home_dir = Path(home).resolve() if home is not None else None
    hmac_key = _load_hmac_key()

    rows: list[dict[str, object]] = []
    any_failed = False
    for candidate in discover_installs(workdir=root, home=home_dir):
        if not candidate.exists:
            continue
        result = verify_packaged_install(workdir=root, dest=candidate.dest, hmac_key=hmac_key)
        if not result.ok:
            any_failed = True
        rows.append(
            {
                "host": candidate.host,
                "scope": candidate.scope,
                "dest": str(candidate.dest),
                "verified": result.ok,
                "reason": result.reason,
            }
        )

    if as_json:
        console.print_json(data={"installs": rows})
        raise SystemExit(2 if any_failed else 0)

    console.print()
    console.print("[bold]Packaged install status[/bold]")
    if not rows:
        console.print("  (no installs found under the scanned host directories)")
        raise SystemExit(0)
    for row in rows:
        mark = "[green]OK[/green]" if row["verified"] else f"[red]FAILED[/red] ({row['reason']})"
        console.print(f"  {row['host']}/{row['scope']}: {mark} -> {row['dest']}")
    raise SystemExit(2 if any_failed else 0)


__all__ = ["package_group"]
