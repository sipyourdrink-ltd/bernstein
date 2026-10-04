"""``bernstein trust``: grant, show or revoke workspace trust.

Repository-authored commands run only in a trusted workspace: plugin and
lifecycle script hooks, workflow command nodes and predicates. Trust is a
record under ``.sdd/runtime/workspace_trust.json`` that nothing grants on
its own; being inside a directory is not consent to run code committed
into it. This command is the one sanctioned way to write that record.
"""

from __future__ import annotations

from pathlib import Path

import click
from rich.console import Console

from bernstein.core.persistence.workspace import (
    grant_workspace_trust,
    is_workspace_trusted,
    revoke_workspace_trust,
)

console = Console()


@click.command("trust")
@click.option(
    "--workdir",
    type=click.Path(file_okay=False, path_type=Path),
    default=Path.cwd,
    show_default="current directory",
    help="Project root whose trust record is written.",
)
@click.option("--revoke", is_flag=True, help="Revoke trust instead of granting it.")
@click.option("--status", is_flag=True, help="Print whether the workspace is trusted and exit.")
@click.option("--granted-by", default="operator", show_default=True, help="Principal recorded in the trust record.")
def trust_cmd(workdir: Path, revoke: bool, status: bool, granted_by: str) -> None:
    """Grant, show or revoke trust for the workspace at WORKDIR.

    \b
      bernstein trust              # allow script hooks and workflow commands here
      bernstein trust --status     # exit 0 when trusted, 1 when not
      bernstein trust --revoke     # remove the record; hooks and workflow commands are gated again
    """
    workdir = workdir.resolve()
    if status:
        trusted = is_workspace_trusted(workdir)
        console.print(f"{workdir}: {'trusted' if trusted else 'not trusted'}")
        raise SystemExit(0 if trusted else 1)
    if revoke:
        revoke_workspace_trust(workdir)
        console.print(f"[yellow]Workspace trust revoked:[/yellow] {workdir}")
        return
    if not (workdir / ".sdd").is_dir():
        raise click.ClickException(f"{workdir} has no .sdd directory; run `bernstein init` there first")
    grant_workspace_trust(workdir, granted_by=granted_by)
    console.print(f"[green]Workspace trusted:[/green] {workdir} (granted by {granted_by})")
