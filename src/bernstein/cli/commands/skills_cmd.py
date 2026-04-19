"""``bernstein skills`` — list / show / verify skill packs (oai-004)."""

from __future__ import annotations

from pathlib import Path

import click

from bernstein import get_templates_dir
from bernstein.cli.helpers import console


@click.group("skills")
def skills_group() -> None:
    """List and inspect progressive-disclosure skill packs.

    \b
      bernstein skills list           # compact overview
      bernstein skills show backend   # print SKILL.md body
      bernstein skills show backend --reference python-conventions.md
    """


@skills_group.command("list")
@click.option(
    "--no-plugins",
    "no_plugins",
    is_flag=True,
    default=False,
    help="Skip third-party ``bernstein.skill_sources`` plugins.",
)
def skills_list(no_plugins: bool) -> None:
    """List every discoverable skill with a one-line description."""
    from rich.table import Table

    from bernstein.core.planning.role_resolver import get_loader

    templates_root = get_templates_dir(Path.cwd())
    templates_roles_dir = templates_root / "roles"
    try:
        loader = get_loader(templates_roles_dir, include_plugins=not no_plugins)
    except Exception as exc:
        console.print(f"[red]Failed to load skill index:[/red] {exc}")
        raise SystemExit(1) from exc

    skills = loader.list_all()
    if not skills:
        console.print(f"[dim]No skill packs found. Expected at {templates_root / 'skills'}[/dim]")
        return

    table = Table(
        title="Skill packs",
        show_lines=False,
        header_style="bold cyan",
    )
    table.add_column("NAME", style="dim", min_width=14)
    table.add_column("DESCRIPTION", min_width=50)
    table.add_column("REFS", justify="right", min_width=4)
    table.add_column("SCRIPTS", justify="right", min_width=6)
    table.add_column("SOURCE", min_width=8)

    for skill in skills:
        description = skill.description.strip().replace("\n", " ")
        if len(description) > 100:
            description = description[:97] + "..."
        table.add_row(
            skill.name,
            description,
            str(len(skill.references)),
            str(len(skill.scripts)),
            skill.source_name,
        )

    console.print(table)
    console.print(f"\n[dim]{len(skills)} skill(s) total[/dim]")


@skills_group.command("show")
@click.argument("name")
@click.option("--reference", "reference", help="Reference filename to load.")
@click.option("--script", "script", help="Script filename to load.")
def skills_show(name: str, reference: str | None, script: str | None) -> None:
    """Print the SKILL.md body for a skill (optionally a reference/script)."""
    from bernstein.core.skills.load_skill_tool import load_skill

    templates_root = get_templates_dir(Path.cwd())
    templates_roles_dir = templates_root / "roles"
    result = load_skill(
        name=name,
        reference=reference,
        script=script,
        templates_roles_dir=templates_roles_dir,
    )
    if result.error:
        console.print(f"[red]{result.error}[/red]")
        raise SystemExit(1)

    if reference is not None and result.reference_content is not None:
        console.print(result.reference_content)
        return
    if script is not None and result.script_content is not None:
        console.print(result.script_content)
        return

    console.print(result.body)
    if result.available_references:
        console.print("\n[dim]references: " + ", ".join(result.available_references) + "[/dim]")
    if result.available_scripts:
        console.print("[dim]scripts: " + ", ".join(result.available_scripts) + "[/dim]")
