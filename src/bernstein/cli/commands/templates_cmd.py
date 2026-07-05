"""Plan template library: list, scaffold, and compress role templates.

Besides the plan/hook template browsing commands, this module carries
the operator-gated role-template compression surface (issue #2249):

  bernstein templates compress <role>|--all    LLM rewrite, validated,
                                               backed up, receipted.
  bernstein templates restore <role>           Byte-identical reversal.

Compression is never automatic - it runs only through this explicit
command. Savings figures come from the spend ledger on subsequent
spawns (``bernstein cost --by role``); compression itself reports only
the template token delta.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import TYPE_CHECKING

import click
from rich.table import Table

from bernstein.cli.helpers import console
from bernstein.core.hook_templates import list_hook_templates, scaffold_hook_template

if TYPE_CHECKING:
    from collections.abc import Callable

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
    console.print("[dim]Templates are in[/dim] [bold]plans/templates/[/bold]: edit the copy after scaffolding.")
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


# ---------------------------------------------------------------------------
# Role template compression (issue #2249)
# ---------------------------------------------------------------------------

_DEFAULT_COMPRESS_MODEL = "anthropic/claude-haiku-4-5"
_DEFAULT_COMPRESS_PROVIDER = "openrouter"

_ROLE_WORKDIR_OPTION = click.option(
    "--workdir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=".",
    help="Project root the role templates are resolved from.",
)


def _compress_llm_call(model: str, provider: str) -> Callable[[str], str]:
    """Return a sync ``prompt -> response`` callable over the configured adapter.

    Module-level indirection so tests can substitute a deterministic
    rewrite function without a live provider.
    """
    import asyncio

    from bernstein.core.llm import call_llm

    def _call(prompt: str) -> str:
        return asyncio.run(call_llm(prompt, model=model, provider=provider, max_tokens=8_000, temperature=0.0))

    return _call


def _resolve_compress_roles(workdir: Path, role: str | None, compress_all: bool) -> list[str]:
    """Return the role names to compress, validating the role/--all choice."""
    from bernstein.core.teams.drift import resolve_roles_dir

    if compress_all == (role is not None):
        raise click.UsageError("Provide exactly one of ROLE or --all.")
    if role is not None:
        return [role]
    roles_dir = resolve_roles_dir(workdir)
    if not roles_dir.is_dir():
        raise click.ClickException(f"role templates directory not found: {roles_dir}")
    return sorted(p.name for p in roles_dir.iterdir() if p.is_dir() and not p.name.startswith("_"))


@templates_group.command("compress")
@click.argument("role", required=False, default=None)
@click.option("--all", "compress_all", is_flag=True, default=False, help="Compress every role template.")
@_ROLE_WORKDIR_OPTION
@click.option("--model", default=_DEFAULT_COMPRESS_MODEL, show_default=True, help="Model for the rewrite.")
@click.option(
    "--provider",
    default=_DEFAULT_COMPRESS_PROVIDER,
    show_default=True,
    help="Adapter/provider for the rewrite (openrouter, openai, ...).",
)
@click.option("--yes", is_flag=True, default=False, help="Skip the confirmation prompt.")
def templates_compress(
    role: str | None,
    compress_all: bool,
    workdir: Path,
    model: str,
    provider: str,
    yes: bool,
) -> None:
    """Compress role prompt templates in place (operator-gated).

    The rewrite goes through the configured adapter, must pass every
    mechanical validator (fenced blocks, headings, URLs, inline code,
    placeholders, completion-contract block; at most two targeted fix
    passes), and is receipted on the audit chain. Originals are backed
    up out of tree, keyed by content hash; ``bernstein templates
    restore ROLE`` reverses the compression byte-identically.
    """
    from bernstein.core.tokens.sensitive_gate import resolve_default_chain
    from bernstein.core.tokens.template_compression import (
        TemplateCompressionError,
        compress_role_templates,
        default_backup_root,
    )

    roles = _resolve_compress_roles(workdir, role, compress_all)
    if not roles:
        console.print("[yellow]No role templates found to compress.[/yellow]")
        return

    if not yes:
        console.print(
            f"About to compress {len(roles)} role template(s) in place: {', '.join(roles)}.\n"
            f"Originals are backed up under [bold]{default_backup_root()}[/bold] "
            "(content-hash keyed, readback-verified) and every rewrite is receipted "
            "on the audit chain."
        )
        if not click.confirm("Proceed?", default=False):
            console.print("[dim]Cancelled.[/dim]")
            return

    chain = resolve_default_chain(workdir)
    llm_call = _compress_llm_call(model, provider)
    failures = 0
    for role_name in roles:
        try:
            outcome = compress_role_templates(
                role_name,
                workdir=workdir,
                llm_call=llm_call,
                adapter=provider,
                model=model,
                chain=chain,
            )
        except TemplateCompressionError as exc:
            console.print(f"[red]{role_name}: {exc}[/red]")
            failures += 1
            continue
        if outcome.applied:
            console.print(
                f"{role_name}: template reduced {outcome.pre_tokens} -> {outcome.post_tokens} tokens; "
                "per-spawn savings will appear in the ledger",
                soft_wrap=True,
            )
        else:
            console.print(f"[yellow]{role_name}: {outcome.reason}[/yellow]")
            failures += 1
    if failures:
        raise SystemExit(1)


@templates_group.command("restore")
@click.argument("role")
@_ROLE_WORKDIR_OPTION
def templates_restore(role: str, workdir: Path) -> None:
    """Restore ROLE's templates to the byte-identical pre-compression originals.

    Reads the out-of-tree backups keyed by content hash, verifies every
    hash on the way in and the role directory digest on the way out, and
    records the reversal on the audit chain.
    """
    from bernstein.core.tokens.sensitive_gate import resolve_default_chain
    from bernstein.core.tokens.template_compression import (
        TemplateCompressionError,
        restore_role_templates,
    )

    try:
        outcome = restore_role_templates(role, workdir=workdir, chain=resolve_default_chain(workdir))
    except TemplateCompressionError as exc:
        raise click.ClickException(str(exc)) from exc

    console.print(
        f"[green]{role}: restored {len(outcome.restored_files)} file(s) byte-identically[/green] "
        f"(directory digest verified: {outcome.pre_digest[:12]}...)"
    )
    for path in outcome.restored_files:
        console.print(f"  [dim]-[/dim] {path}")
