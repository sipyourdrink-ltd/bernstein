"""Man page generator — creates troff-format man pages from Click CLI commands.

Walks the Click command tree and produces one man page per command/subcommand
in standard troff format suitable for ``man(1)``.

Usage::

    bernstein man-pages              # writes to docs/man/
    bernstein man-pages --output-dir /tmp/man
"""

from __future__ import annotations

import datetime
from pathlib import Path

import click


def _escape_troff(text: str) -> str:
    """Escape characters that are special in troff.

    Backslashes and hyphens need escaping to render correctly.
    """
    return text.replace("\\", "\\\\").replace("-", "\\-")


def _format_option_name(name: str) -> str:
    """Format an option name with troff bold + hyphen escaping."""
    return f"\\fB{_escape_troff(name)}\\fR"


def generate_man_page(
    cmd_name: str,
    cmd_help: str,
    options: list[tuple[str, str]],
    subcommands: list[tuple[str, str]] | None = None,
) -> str:
    """Generate a troff-formatted man page string.

    Args:
        cmd_name: Command name (e.g. "run", "agents list").
        cmd_help: Help text / description for the command.
        options: List of (option_decl, help_text) pairs.
        subcommands: Optional list of (subcommand_name, help_text) pairs.

    Returns:
        Complete troff man page as a string.
    """
    # Build the page title: "bernstein-run" or "bernstein-agents-list"
    page_title = f"bernstein-{cmd_name}".replace(" ", "-")
    upper_title = page_title.upper()
    date_str = datetime.date.today().strftime("%B %Y")

    lines: list[str] = []

    # Header
    lines.append(f'.TH {upper_title} 1 "{date_str}" "Bernstein" "Bernstein Manual"')

    # NAME section
    escaped_name = _escape_troff(page_title)
    first_line = cmd_help.split("\n")[0].strip() if cmd_help else "Bernstein CLI command"
    escaped_first = _escape_troff(first_line.rstrip("."))
    lines.append(".SH NAME")
    lines.append(f"{escaped_name} \\- {escaped_first}")

    # SYNOPSIS section
    synopsis_cmd = f"bernstein {cmd_name}" if cmd_name != "bernstein" else "bernstein"
    escaped_synopsis = _escape_troff(synopsis_cmd)
    lines.append(".SH SYNOPSIS")
    if subcommands:
        lines.append(f".B {escaped_synopsis}")
        lines.append("[\\fICOMMAND\\fR] [\\fIOPTIONS\\fR]")
    elif options:
        lines.append(f".B {escaped_synopsis}")
        lines.append("[\\fIOPTIONS\\fR]")
    else:
        lines.append(f".B {escaped_synopsis}")

    # DESCRIPTION section
    if cmd_help:
        lines.append(".SH DESCRIPTION")
        for paragraph in cmd_help.strip().split("\n\n"):
            cleaned = " ".join(paragraph.split())
            lines.append(_escape_troff(cleaned))
            lines.append(".PP")
        # Remove trailing .PP
        if lines[-1] == ".PP":
            lines.pop()

    # OPTIONS section
    if options:
        lines.append(".SH OPTIONS")
        for opt_decl, opt_help in options:
            lines.append(".TP")
            lines.append(_format_option_name(opt_decl))
            lines.append(_escape_troff(opt_help) if opt_help else "")

    # SUBCOMMANDS section
    if subcommands:
        lines.append(".SH SUBCOMMANDS")
        for sub_name, sub_help in subcommands:
            lines.append(".TP")
            lines.append(f"\\fB{_escape_troff(sub_name)}\\fR")
            lines.append(_escape_troff(sub_help) if sub_help else "")

    # SEE ALSO section
    lines.append(".SH SEE ALSO")
    lines.append(f"\\fBbernstein\\fR(1)")

    return "\n".join(lines) + "\n"


def _collect_options(cmd: click.Command) -> list[tuple[str, str]]:
    """Extract option declarations and help text from a Click command."""
    options: list[tuple[str, str]] = []
    for param in cmd.params:
        if isinstance(param, click.Option):
            decl = ", ".join(param.opts)
            if param.secondary_opts:
                decl += ", " + ", ".join(param.secondary_opts)
            help_text = param.help or ""
            options.append((decl, help_text))
    return options


def _walk_commands(
    group: click.Group,
    prefix: str = "",
) -> list[tuple[str, click.Command]]:
    """Recursively walk Click command tree, yielding (full_name, command)."""
    result: list[tuple[str, click.Command]] = []
    for name, cmd in sorted(group.commands.items()):
        full_name = f"{prefix} {name}".strip() if prefix else name
        result.append((full_name, cmd))
        if isinstance(cmd, click.Group):
            result.extend(_walk_commands(cmd, full_name))
    return result


def generate_all_man_pages(cli_group: click.Group) -> dict[str, str]:
    """Walk a Click command tree and generate man pages for every command.

    Args:
        cli_group: The top-level Click group.

    Returns:
        Dict mapping command name (e.g. "run") to troff content string.
    """
    pages: dict[str, str] = {}

    # Top-level page for the group itself
    top_options = _collect_options(cli_group)
    top_subcommands: list[tuple[str, str]] = [
        (name, (cmd.help or cmd.short_help or "").split("\n")[0])
        for name, cmd in sorted(cli_group.commands.items())
    ]
    pages["bernstein"] = generate_man_page(
        cmd_name="bernstein",
        cmd_help=cli_group.help or "Declarative agent orchestration for engineering teams.",
        options=top_options,
        subcommands=top_subcommands,
    )

    # Each subcommand (including nested)
    for full_name, cmd in _walk_commands(cli_group):
        options = _collect_options(cmd)
        subcommands: list[tuple[str, str]] | None = None
        if isinstance(cmd, click.Group):
            subcommands = [
                (sub_name, (sub_cmd.help or sub_cmd.short_help or "").split("\n")[0])
                for sub_name, sub_cmd in sorted(cmd.commands.items())
            ]
        pages[full_name] = generate_man_page(
            cmd_name=full_name,
            cmd_help=cmd.help or cmd.short_help or "",
            options=options,
            subcommands=subcommands,
        )

    return pages


def write_man_pages(output_dir: Path, pages: dict[str, str]) -> list[Path]:
    """Write man pages to disk as ``bernstein-{name}.1`` files.

    Args:
        output_dir: Directory to write man page files into.
        pages: Dict from :func:`generate_all_man_pages`.

    Returns:
        List of paths written.
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, content in sorted(pages.items()):
        filename = f"bernstein-{name}.1" if name != "bernstein" else "bernstein.1"
        # Normalize spaces in filename to hyphens
        filename = filename.replace(" ", "-")
        path = output_dir / filename
        path.write_text(content)
        written.append(path)
    return written


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------


@click.command("man-pages")
@click.option(
    "--output-dir",
    default="docs/man",
    show_default=True,
    type=click.Path(),
    help="Directory to write man page files into.",
)
def man_pages_cmd(output_dir: str) -> None:
    """Generate troff man pages for all Bernstein CLI commands."""
    from bernstein.cli.helpers import console
    from bernstein.cli.main import cli as cli_group

    pages = generate_all_man_pages(cli_group)
    written = write_man_pages(Path(output_dir), pages)
    console.print(f"[green]Wrote {len(written)} man page(s) to {output_dir}/[/green]")
    for p in written:
        console.print(f"  [dim]{p.name}[/dim]")
