"""``bernstein readme-l10n`` - translated README drift gate (issue #3425).

Every translated ``README.<ietf-tag>.md`` mirrors the English
``README.md`` section for section. Each translated section carries an
HTML-comment binding to a content hash of the English section it
mirrors; ``verify`` recomputes those hashes and fails on drift, naming
the language and the exact stale section heading, so a PR that edits an
English section and leaves a translation behind turns red in CI.

Non-zero exit codes are operator contract:

- ``0``  - all configured languages are in sync
- ``1``  - drift detected (stale binding, translated code block, or
           modified verbatim block)
- ``2``  - configuration error (malformed ``languages`` entry)

Designed for CI gating::

    bernstein readme-l10n verify || (echo 'translated README drift; run sync' && exit 1)
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import TYPE_CHECKING

import click

from bernstein.core.knowledge.readme_l10n import (
    BINDING_RE,
    HEADER_SECTION,
    load_config,
    section_hash,
    split_sections,
    verify_language,
)

if TYPE_CHECKING:
    import re

_BINDING_TEMPLATE = '<!-- l10n: en="{en}" hash="{hash}" -->'


def _resolve_workdir(workdir: Path) -> Path:
    if not (workdir / "README.md").is_file():
        raise click.UsageError(f"{workdir} does not contain a README.md; pass --workdir REPO_ROOT")
    return workdir


def _load_languages(workdir: Path) -> list[str]:
    """Load the configured language set, mapping config errors to exit 2."""
    try:
        return load_config(workdir / "pyproject.toml")
    except ValueError as exc:
        click.echo(f"CONFIG   {exc}", err=True)
        sys.exit(2)
    return []


@click.group(name="readme-l10n")
def readme_l10n_cmd() -> None:
    """Verify and regenerate translated README bindings."""


@readme_l10n_cmd.command(name="verify")
@click.option(
    "--workdir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path.cwd,
    show_default="cwd",
    help="Repository root containing README.md and pyproject.toml.",
)
def readme_l10n_verify(workdir: Path) -> None:
    """Exit 1 if any translated README drifts from the English source.

    Checks, per configured language:

    - every prose section carries a binding to the current hash of its
      English source (a stale binding names the section),
    - fenced code blocks are verbatim against the English section they
      mirror (a translated command or flag is a failure, not a warning),
    - every binding sits directly under the translated heading it
      mirrors (duplicated or orphaned bindings are a failure),
    - every translated section carries the same number of paragraph
      blocks as the English section it mirrors, so a paragraph added to
      the English source cannot silently go missing from a translation.
      A translation must therefore preserve the blank-line block
      structure of its English section: merging two paragraphs into one
      is exit 1, even though no content is missing,
    - the header and footer blocks (logo, badges, language links,
      license line) are shared verbatim.

    Designed for CI gating::

        bernstein readme-l10n verify || (echo 'drift; run sync' && exit 1)
    """
    _resolve_workdir(workdir)
    languages = _load_languages(workdir)
    if not languages:
        click.echo("SKIP     no [tool.bernstein.readme-l10n] languages configured; nothing to verify")
        return

    source_text = (workdir / "README.md").read_text(encoding="utf-8")
    sections = split_sections(source_text)

    drift_total = 0
    for lang in languages:
        tpath = workdir / f"README.{lang}.md"
        if not tpath.is_file():
            click.echo(f"MISSING  README.{lang}.md (configured but not present)", err=True)
            drift_total += 1
            continue
        result = verify_language(sections, lang, tpath.read_text(encoding="utf-8"))
        if result.ok:
            click.echo(f"OK       README.{lang}.md")
            continue
        for err in result.errors:
            click.echo(f"DRIFT    README.{lang}.md: {err}", err=True)
        drift_total += len(result.errors)

    if drift_total:
        click.echo(
            f"\n{drift_total} drift(s) across {len(languages)} language file(s). "
            "Run `bernstein readme-l10n sync` to update bindings.",
            err=True,
        )
        sys.exit(1)
    click.echo(f"OK       all {len(languages)} translated README(s) in sync")


@readme_l10n_cmd.command(name="sync")
@click.option(
    "--workdir",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=Path.cwd,
    show_default="cwd",
    help="Repository root containing README.md and pyproject.toml.",
)
def readme_l10n_sync(workdir: Path) -> None:
    """Recompute binding hashes in every translated README.

    Rewrites the ``hash="..."`` value of every existing l10n binding
    line to the current hash of its English section. Bindings that are
    missing entirely are reported, not invented - a new translated
    section must declare which English section it mirrors before sync
    can pin it.
    """
    _resolve_workdir(workdir)
    languages = _load_languages(workdir)
    if not languages:
        click.echo("SKIP     no languages configured")
        return

    source_text = (workdir / "README.md").read_text(encoding="utf-8")
    sections = split_sections(source_text)
    by_heading = {s.heading: s for s in sections}
    prose_headings = [s.heading for s in sections if s.heading not in (HEADER_SECTION, "(footer)")]

    updated = 0
    for lang in languages:
        tpath = workdir / f"README.{lang}.md"
        if not tpath.is_file():
            click.echo(f"MISSING  README.{lang}.md (configured but not present)", err=True)
            continue
        text = tpath.read_text(encoding="utf-8")

        def _rehash(match: re.Match[str], lang: str = lang) -> str:
            nonlocal updated
            en, _old = match.group(1), match.group(2)
            section = by_heading.get(en)
            if section is None:
                click.echo(
                    f'WARN     README.{lang}.md binds unknown section "{en}" (no such heading in README.md)',
                    err=True,
                )
                return match.group(0)
            new_hash = section_hash(section)
            if new_hash != match.group(2):
                updated += 1
            return _BINDING_TEMPLATE.format(en=en, hash=new_hash)

        new_text = BINDING_RE.sub(_rehash, text)
        if new_text != text:
            tpath.write_text(new_text, encoding="utf-8")
            click.echo(f"SYNCED   README.{lang}.md")

        # Report bindings that are missing entirely (translated sections
        # that never declared their English source).
        bound = {en for en, _h in BINDING_RE.findall(new_text)}
        for heading in prose_headings:
            if heading not in bound:
                click.echo(
                    f"MISSING  README.{lang}.md has no binding for section "
                    f'"{heading}"; add a binding line under the translated '
                    "heading, then run sync",
                    err=True,
                )

    click.echo(f"done: {updated} binding(s) updated")
