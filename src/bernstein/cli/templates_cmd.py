"""Plan template library — list and scaffold reusable YAML plans."""

from __future__ import annotations

import shutil
from pathlib import Path

import click
from rich.table import Table

from bernstein.cli.helpers import console

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


@templates_group.command("list")
def templates_list() -> None:
    """List available plan templates."""
    tdir = _templates_dir()
    if tdir is None:
        console.print("[red]Templates directory not found.[/red]")
        raise SystemExit(1)

    yamls = sorted(tdir.glob("*.yaml"))
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
        console.print("[red]Templates directory not found.[/red]")
        raise SystemExit(1)

    src = tdir / f"{template_name}.yaml"
    if not src.exists():
        available = [p.stem for p in tdir.glob("*.yaml")]
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
        console.print("[red]Templates directory not found.[/red]")
        raise SystemExit(1)

    src = tdir / f"{template_name}.yaml"
    if not src.exists():
        available = [p.stem for p in tdir.glob("*.yaml")]
        console.print(f"[red]Unknown template: {template_name!r}[/red]")
        if available:
            console.print(f"Available: {', '.join(available)}")
        raise SystemExit(1)

    console.print(src.read_text())
