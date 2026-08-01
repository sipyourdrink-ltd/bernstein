"""Prompted-app scaffold generator.

Materialises a small project skeleton from a single goal prompt by picking
ONE template family via a deterministic keyword heuristic. The CLI verb is

    bernstein scaffold "<prompt>" [--template auto|...] [--output DIR]

A richer end-to-end flow (architect → backend → frontend → reviewer, preview
tunnel, deploy adapters) lives in follow-ups and composes on top of this
scaffold. This module ships the smallest viable slice: pick a template,
render its files, write to disk.
"""

from __future__ import annotations

from pathlib import Path

import click

from bernstein.cli.helpers import console
from bernstein.cli.scaffold.templates import (
    SCAFFOLD_TEMPLATES,
    ScaffoldError,
    ScaffoldTemplate,
    list_template_names,
    materialize_template,
    pick_template,
)


@click.command("scaffold")
@click.argument("prompt", required=True)
@click.option(
    "--template",
    "template_name",
    default="auto",
    show_default=True,
    metavar="NAME",
    help="Template to use; 'auto' picks via keyword heuristic on PROMPT.",
)
@click.option(
    "--output",
    "output_dir",
    default=None,
    metavar="DIR",
    help="Destination directory (default: ./<slug-of-prompt>).",
)
@click.option(
    "--force",
    is_flag=True,
    default=False,
    help="Allow writing into a non-empty directory.",
)
def scaffold_cmd(
    prompt: str,
    template_name: str,
    output_dir: str | None,
    force: bool,
) -> None:
    """Bootstrap a project skeleton from a single goal PROMPT.

    \b
    Examples:
      bernstein scaffold "Build me a habit tracker"
      bernstein scaffold "CLI to convert markdown to PDF" --template python-cli
      bernstein scaffold "static landing page" --output ./my-site
    """
    chosen: ScaffoldTemplate | None
    if template_name == "auto":
        chosen = pick_template(prompt)
    else:
        chosen = SCAFFOLD_TEMPLATES.get(template_name)
        if chosen is None:
            console.print(f"[red]Unknown template: {template_name!r}[/red]")
            console.print(f"Available: {', '.join(list_template_names())}")
            raise SystemExit(1)

    dest = (Path(output_dir) if output_dir else Path(_slugify(prompt))).resolve()
    try:
        created = materialize_template(chosen, dest, prompt=prompt, force=force)
    except ScaffoldError as exc:
        console.print(f"[red]{exc}[/red]")
        raise SystemExit(1) from exc

    console.print(
        f"[green]Scaffolded[/green] [bold]{chosen.name}[/bold] into [bold]{dest}[/bold]",
    )
    for path in created:
        console.print(f"  [dim]-[/dim] {path.relative_to(dest)}")
    console.print()
    console.print(
        f"[dim]Next:[/dim] cd {dest} && cat README.md",
    )


def _slugify(prompt: str) -> str:
    """Produce a filesystem-safe slug from a free-form prompt.

    Lowercases, keeps alphanumerics and dashes, collapses runs, trims to a
    sensible length. Empty input falls back to ``scaffold-app``.
    """
    cleaned: list[str] = []
    prev_dash = False
    for ch in prompt.lower():
        if ch.isalnum():
            cleaned.append(ch)
            prev_dash = False
        elif not prev_dash:
            cleaned.append("-")
            prev_dash = True
    slug = "".join(cleaned).strip("-")[:48]
    return slug or "scaffold-app"
