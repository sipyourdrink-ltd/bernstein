"""CLI commands: ``bernstein fingerprint`` — agent output fingerprinting.

Detects when agent-generated code closely matches known open-source code
in a corpus, flagging potential licence risk from verbatim training-data
copy-paste.

Sub-commands::

    bernstein fingerprint build --corpus-dir PATH   # index a corpus
    bernstein fingerprint check FILE [FILE...]       # check files

Examples::

    # Build an index from a local OSS corpus directory
    bernstein fingerprint build --corpus-dir ~/oss-corpus

    # Check recently changed files
    bernstein fingerprint check $(git diff --name-only HEAD~1 -- '*.py')

    # Use a pre-built index
    bernstein fingerprint check src/mymodule.py --index .sdd/fp.json
"""

from __future__ import annotations

from pathlib import Path

import click

from bernstein.cli.helpers import console

_DEFAULT_INDEX = Path(".sdd/fingerprint_index.json")
_DEFAULT_THRESHOLD = 0.8


@click.group("fingerprint")
def fingerprint_group() -> None:
    """Agent output fingerprinting for licence-risk detection."""


@fingerprint_group.command("build")
@click.option(
    "--corpus-dir",
    required=True,
    type=click.Path(exists=True, file_okay=False, resolve_path=True),
    help="Directory of known open-source code to index.",
)
@click.option(
    "--output",
    "-o",
    default=str(_DEFAULT_INDEX),
    show_default=True,
    type=click.Path(dir_okay=False, resolve_path=True),
    help="Destination path for the fingerprint index JSON.",
)
@click.option(
    "--glob",
    default="**/*.py",
    show_default=True,
    help="Glob pattern for files to index within the corpus directory.",
)
@click.option(
    "--max-files",
    default=5000,
    show_default=True,
    type=int,
    help="Maximum number of files to index.",
)
def fingerprint_build_cmd(
    corpus_dir: str,
    output: str,
    glob: str,
    max_files: int,
) -> None:
    """Build a fingerprint index from a corpus directory.

    \b
      bernstein fingerprint build --corpus-dir ~/oss-corpus
      bernstein fingerprint build --corpus-dir /code/stdlib --output idx.json
    """
    from bernstein.core.output_fingerprint import CorpusIndex, FingerprintConfig

    config = FingerprintConfig(enabled=True)
    index = CorpusIndex(config)

    corpus_path = Path(corpus_dir)
    output_path = Path(output)

    console.print(f"[dim]Indexing corpus: {corpus_path} (glob={glob})[/dim]")
    indexed = index.add_directory(corpus_path, glob=glob, max_files=max_files)

    if indexed == 0:
        console.print("[yellow]No files found in corpus directory.[/yellow]")
        return

    index.save(output_path)
    console.print(f"[green]Indexed {indexed} file(s) → {output_path}[/green]")


@fingerprint_group.command("check")
@click.argument("files", nargs=-1, required=True)
@click.option(
    "--index",
    "-i",
    default=None,
    type=click.Path(exists=True, dir_okay=False, resolve_path=True),
    help=f"Pre-built index file (default: {_DEFAULT_INDEX}).",
)
@click.option(
    "--corpus-dir",
    default=None,
    type=click.Path(exists=True, file_okay=False, resolve_path=True),
    help="Corpus directory to index on-the-fly (alternative to --index).",
)
@click.option(
    "--threshold",
    "-t",
    default=_DEFAULT_THRESHOLD,
    show_default=True,
    type=float,
    help="Minimum Jaccard similarity to flag (0.0-1.0).",
)
@click.option(
    "--block",
    is_flag=True,
    default=False,
    help="Exit 1 when any match above threshold is found.",
)
@click.option(
    "--json",
    "output_json",
    is_flag=True,
    default=False,
    help="Output results as JSON.",
)
def fingerprint_check_cmd(
    files: tuple[str, ...],
    index: str | None,
    corpus_dir: str | None,
    threshold: float,
    block: bool,
    output_json: bool,
) -> None:
    """Check files for similarity to known open-source code.

    \b
      bernstein fingerprint check src/foo.py --index .sdd/fp.json
      bernstein fingerprint check src/ --corpus-dir ~/oss --threshold 0.85
    """
    import json as _json

    from rich.table import Table

    from bernstein.core.output_fingerprint import (
        CorpusIndex,
        FingerprintConfig,
        check_fingerprint,
    )

    config = FingerprintConfig(
        enabled=True,
        threshold=threshold,
        block_on_match=block,
    )

    # Resolve corpus index
    corpus_index: CorpusIndex
    if index:
        try:
            corpus_index = CorpusIndex.load(Path(index), config)
            console.print(f"[dim]Loaded index: {index} ({corpus_index.size} entries)[/dim]")
        except Exception as exc:
            console.print(f"[red]Failed to load index {index}: {exc}[/red]")
            raise SystemExit(1) from exc
    elif corpus_dir:
        corpus_index = CorpusIndex(config)
        indexed = corpus_index.add_directory(Path(corpus_dir))
        console.print(f"[dim]Built index from {corpus_dir}: {indexed} files[/dim]")
    else:
        # Check for default index
        if _DEFAULT_INDEX.exists():
            try:
                corpus_index = CorpusIndex.load(_DEFAULT_INDEX, config)
                console.print(f"[dim]Using default index: {_DEFAULT_INDEX} ({corpus_index.size} entries)[/dim]")
            except Exception as exc:
                console.print(f"[yellow]Default index unreadable: {exc}. No corpus to compare against.[/yellow]")
                return
        else:
            console.print("[yellow]No corpus index found. Use --index or --corpus-dir to specify one.[/yellow]")
            return

    if corpus_index.size == 0:
        console.print("[yellow]Corpus index is empty — nothing to compare against.[/yellow]")
        return

    # Collect files to check
    paths_to_check: list[Path] = []
    for file_arg in files:
        p = Path(file_arg)
        if p.is_dir():
            paths_to_check.extend(sorted(p.rglob("*.py")))
        elif p.is_file():
            paths_to_check.append(p)
        else:
            console.print(f"[yellow]Skipping (not found): {file_arg}[/yellow]")

    if not paths_to_check:
        console.print("[yellow]No files to check.[/yellow]")
        return

    any_flagged = False
    all_results: list[dict[str, object]] = []

    for fp in paths_to_check:
        try:
            code = fp.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            console.print(f"[yellow]Cannot read {fp}: {exc}[/yellow]")
            continue

        result = check_fingerprint(code, corpus_index, config)

        if output_json:
            all_results.append(
                {
                    "file": str(fp),
                    "passed": result.passed,
                    "blocked": result.blocked,
                    "detail": result.detail,
                    "matches": [
                        {
                            "source_label": m.source_label,
                            "similarity": m.similarity,
                            "flagged": m.flagged,
                        }
                        for m in result.matches
                    ],
                }
            )
        else:
            flagged_matches = [m for m in result.matches if m.flagged]
            if flagged_matches:
                any_flagged = True
                console.print(
                    f"\n[bold yellow]{fp}[/bold yellow] — "
                    f"[red]{len(flagged_matches)} match(es) above "
                    f"{threshold:.0%} threshold[/red]"
                )
                table = Table(show_header=True, box=None, padding=(0, 2))
                table.add_column("Corpus file", style="dim", no_wrap=False)
                table.add_column("Similarity", style="red", no_wrap=True)
                for m in flagged_matches[:10]:
                    table.add_row(m.source_label, f"{m.similarity:.1%}")
                console.print(table)
            else:
                console.print(f"[green]✓[/green] {fp}  [dim](no matches)[/dim]")

        if result.blocked:
            any_flagged = True

    if output_json:
        console.print(_json.dumps(all_results, indent=2))

    if block and any_flagged:
        console.print("\n[bold red]Fingerprint check FAILED — matches above threshold detected[/bold red]")
        raise SystemExit(1)

    if not output_json and not any_flagged:
        console.print("\n[bold green]Fingerprint check passed.[/bold green]")
