"""``bernstein wiki`` - emit a deterministic ``WIKI.md`` for the repo.

Smallest-viable slice of repo-wiki + code-search. Builds the AST symbol
graph in-process and renders a Markdown summary to stdout, or writes it
to ``WIKI.md`` at the repo root with ``--write``. HTTP routes, MCP
exposure, and post-commit re-indexing remain follow-ups.

The command renders locally on every invocation against a private repo.
"""

from __future__ import annotations

from pathlib import Path

import click

from bernstein.cli.helpers import console
from bernstein.core.git_context import ls_files
from bernstein.core.knowledge.ast_symbol_graph import build_semantic_graph
from bernstein.core.knowledge.wiki_renderer import render_wiki

_DEFAULT_OUTPUT = Path("WIKI.md")


@click.group("wiki")
def wiki_group() -> None:
    """Render a Markdown wiki for the current repo.

    Runs locally, with no network round-trip and no external service, so
    it works offline and on private repositories.
    """


@wiki_group.command("build")
@click.option(
    "--repo",
    "repo_path",
    type=click.Path(exists=True, file_okay=False, dir_okay=True, path_type=Path),
    default=Path.cwd(),
    show_default=False,
    help="Repo root to scan (defaults to current working directory).",
)
@click.option(
    "--write",
    "write_output",
    is_flag=True,
    default=False,
    help="Write the rendered wiki to WIKI.md at the repo root.",
)
@click.option(
    "--output",
    "output_path",
    type=click.Path(file_okay=True, dir_okay=False, path_type=Path),
    default=None,
    help="Custom output path (implies --write). Useful for CI snapshots.",
)
def wiki_build(
    repo_path: Path,
    write_output: bool,
    output_path: Path | None,
) -> None:
    """Render WIKI.md from the AST symbol graph and stream it to stdout.

    With ``--write`` (or ``--output PATH``) the rendered Markdown is
    written to disk instead and the path is logged. The output is
    deterministic for a given repo state, which makes it safe to commit
    or diff in CI.
    """
    repo_root = repo_path.resolve()
    repo_name = repo_root.name
    files = ls_files(repo_root)
    graph = build_semantic_graph(repo_root)
    markdown = render_wiki(graph, files, repo_name=repo_name)

    if output_path is not None:
        target = output_path if output_path.is_absolute() else repo_root / output_path
    elif write_output:
        target = repo_root / _DEFAULT_OUTPUT
    else:
        click.echo(markdown, nl=False)
        return

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(markdown, encoding="utf-8")
    console.print(f"[green]Wrote wiki:[/green] {target}")
