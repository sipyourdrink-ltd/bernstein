"""Plan template library — list and scaffold reusable YAML plans."""

from __future__ import annotations

import shutil
from pathlib import Path

import click
from rich.table import Table

from bernstein.cli.helpers import console
from bernstein.core.hook_templates import list_hook_templates, scaffold_hook_template

_YAML_GLOB = "*.yaml"

_TEMPLATES_NOT_FOUND_MSG = "[red]Templates directory not found.[/red]"

# Templates ship alongside the bernstein package.
_TEMPLATES_DIR = Path(__file__).parent.parent.parent.parent / "plans" / "templates"

# Fallback: look relative to the installed package (editable installs).
_ALT_TEMPLATES_DIR = Path(__file__).parent.parent.parent / "plans" / "templates"

_DESCRIPTIONS: dict[str, str] = {
    "rest-api": "Models → Routes → Auth → Tests (4 stages)",
    "cli-tool": "Parser → Commands → Packaging (3 stages)",
    "library": "Core → Tests → Docs + PyPI (3 stages)",
    "fullstack": "DB → API → Frontend → Auth → Deploy (5 stages)",
    "refactor": "Tests → Refactor → Validate (3 stages)",
}


def _templates_dir() -> Path | None:
    """Return the templates directory, trying multiple candidate paths."""
    for candidate in (_TEMPLATES_DIR, _ALT_TEMPLATES_DIR):
        if candidate.is_dir():
            return candidate
    return None


@click.group("templates")
def templates_group() -> None:
    """Browse and scaffold reusable plan templates."""


@templates_group.group("hooks")
def templates_hooks_group() -> None:
    """Browse and scaffold bundled command-hook templates."""


@templates_group.command("list")
def templates_list() -> None:
    """List available plan templates."""
    tdir = _templates_dir()
    if tdir is None:
        console.print(_TEMPLATES_NOT_FOUND_MSG)
        raise SystemExit(1)

    yamls = sorted(tdir.glob(_YAML_GLOB))
    if not yamls:
        console.print("[yellow]No templates found.[/yellow]")
        return

    table = Table(
        "Template",
        "Description",
        "Usage",
        show_header=True,
        header_style="bold cyan",
        show_lines=False,
        box=None,
        pad_edge=False,
        padding=(0, 2),
    )
    for path in yamls:
        name = path.stem
        desc = _DESCRIPTIONS.get(name, "")
        table.add_row(
            f"[bold green]{name}[/bold green]",
            desc,
            f"[dim]bernstein templates use {name}[/dim]",
        )

    console.print()
    console.print(table)
    console.print()
    console.print("[dim]Templates are in[/dim] [bold]plans/templates/[/bold] — edit the copy after scaffolding.")
    console.print()


@templates_group.command("use")
@click.argument("template_name")
@click.argument("output", required=False, default=None)
def templates_use(template_name: str, output: str | None) -> None:
    """Copy TEMPLATE_NAME to OUTPUT (default: plans/<name>.yaml).

    Example:

        bernstein templates use rest-api plans/my-api.yaml
    """
    tdir = _templates_dir()
    if tdir is None:
        console.print(_TEMPLATES_NOT_FOUND_MSG)
        raise SystemExit(1)

    src = tdir / f"{template_name}.yaml"
    if not src.exists():
        available = [p.stem for p in tdir.glob(_YAML_GLOB)]
        console.print(f"[red]Unknown template: {template_name!r}[/red]")
        if available:
            console.print(f"Available: {', '.join(available)}")
        raise SystemExit(1)

    dest = Path(output) if output else Path("plans") / f"{template_name}.yaml"
    dest.parent.mkdir(parents=True, exist_ok=True)

    if dest.exists():
        console.print(f"[yellow]File already exists:[/yellow] {dest}")
        if not click.confirm("Overwrite?", default=False):
            console.print("[dim]Cancelled.[/dim]")
            return

    shutil.copy2(src, dest)
    console.print(f"[green]Created[/green] [bold]{dest}[/bold]")
    console.print(f"[dim]Edit the plan, then run:[/dim] [bold]bernstein run {dest}[/bold]")


@templates_group.command("show")
@click.argument("template_name")
def templates_show(template_name: str) -> None:
    """Print the contents of a template to stdout."""
    tdir = _templates_dir()
    if tdir is None:
        console.print(_TEMPLATES_NOT_FOUND_MSG)
        raise SystemExit(1)

    src = tdir / f"{template_name}.yaml"
    if not src.exists():
        available = [p.stem for p in tdir.glob(_YAML_GLOB)]
        console.print(f"[red]Unknown template: {template_name!r}[/red]")
        if available:
            console.print(f"Available: {', '.join(available)}")
        raise SystemExit(1)

    console.print(src.read_text())


@templates_hooks_group.command("list")
def templates_hooks_list() -> None:
    """List bundled command-hook templates."""
    table = Table(
        "Template",
        "Description",
        "Usage",
        show_header=True,
        header_style="bold cyan",
        show_lines=False,
        box=None,
        pad_edge=False,
        padding=(0, 2),
    )
    for template in list_hook_templates():
        table.add_row(
            f"[bold green]{template.name}[/bold green]",
            template.description,
            f"[dim]bernstein templates hooks use {template.name}[/dim]",
        )
    console.print()
    console.print(table)
    console.print()


@templates_hooks_group.command("use")
@click.argument("template_name")
@click.option(
    "--workdir", default=".", show_default=True, help="Workspace root where .bernstein/hooks will be created."
)
@click.option("--force", is_flag=True, default=False, help="Overwrite existing template files.")
def templates_hooks_use(template_name: str, workdir: str, force: bool) -> None:
    """Install a bundled command-hook template into WORKDIR/.bernstein/hooks."""
    try:
        created = scaffold_hook_template(template_name, Path(workdir), force=force)
    except ValueError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from exc
    except FileExistsError as exc:
        console.print(f"[yellow]{exc}[/yellow]")
        console.print("[dim]Use --force to overwrite the existing template files.[/dim]")
        raise SystemExit(1) from exc

    console.print(f"[green]Installed hook template:[/green] [bold]{template_name}[/bold]")
    for path in created:
        console.print(f"  [dim]-[/dim] {path}")
