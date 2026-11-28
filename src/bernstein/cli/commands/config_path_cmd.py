"""Config path display in help output.

Show where bernstein.yaml is loaded from.

Adds a ``bernstein config path`` subcommand and a helper that resolves
the active configuration file path.
"""

from __future__ import annotations

import os
from pathlib import Path

import click

from bernstein.cli.helpers import console

# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------

_SEED_FILENAMES = ("bernstein.yaml", "bernstein.yml")
_SEED_PATH_ENV = "BERNSTEIN_SEED_PATH"


def resolve_config_path() -> Path | None:
    """Find the seed config file that a run in the current directory would load.

    ``BERNSTEIN_SEED_PATH`` comes first when it names an existing file, through the seed resolver
    itself, so this reports the file the resolver selects (#7958). It reported ``bernstein.yaml``
    while the resolver selected the override.

    Returns:
        Path to the config file, or None if not found.
    """
    if os.environ.get(_SEED_PATH_ENV, "").strip():
        # Imported here: this module loads with the CLI, and the seed module is not needed otherwise.
        from bernstein.core.config.seed import resolve_seed_path

        selected = resolve_seed_path(Path.cwd())
        if selected.is_file():
            return selected
    for name in _SEED_FILENAMES:
        p = Path.cwd() / name
        if p.is_file():
            return p.resolve()
    return None


def resolve_sdd_config_path() -> Path | None:
    """Find the .sdd/config.yaml if it exists.

    Returns:
        Path to .sdd/config.yaml, or None if not found.
    """
    p = Path.cwd() / ".sdd" / "config.yaml"
    if p.is_file():
        return p.resolve()
    return None


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------


@click.command("config-path")
@click.option("--json", "as_json", is_flag=True, default=False, help="Output as JSON.")
def config_path_cmd(as_json: bool) -> None:
    """Show where bernstein.yaml and .sdd/config.yaml are loaded from.

    \b
    Displays the resolved paths for the project configuration file
    and the workspace configuration file.

    \b
    Examples:
      bernstein config-path
      bernstein config-path --json
    """
    import json

    seed_path = resolve_config_path()
    sdd_path = resolve_sdd_config_path()

    if as_json:
        click.echo(
            json.dumps(
                {
                    "config_file": str(seed_path) if seed_path else None,
                    "sdd_config": str(sdd_path) if sdd_path else None,
                    "cwd": str(Path.cwd()),
                }
            )
        )
        return

    console.print("[bold]Bernstein configuration paths:[/bold]\n")

    if seed_path:
        console.print(f"  [green]Config file:[/green]  {seed_path}")
    else:
        console.print("  [yellow]Config file:[/yellow]  not found (bernstein.yaml / bernstein.yml)")
        console.print("  [dim]Run 'bernstein init' to create one.[/dim]")

    if sdd_path:
        console.print(f"  [green]SDD config:[/green]   {sdd_path}")
    else:
        console.print("  [dim]SDD config:[/dim]   not found (.sdd/config.yaml)")

    console.print(f"  [dim]Working dir:[/dim]  {Path.cwd()}")
