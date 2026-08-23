"""``bernstein volunteer``: the donor-and-project surface for volunteer work (#3919).

A project opts in by committing ``.bernstein/volunteer.json``. A donor's worker
reads that file and refuses anything it does not permit. Until this group
existed the file had no reader on the CLI at all: the first feedback a
maintainer got about a bad manifest was a stranger's worker declining their
repository, which is the worst place to learn that ``allowed_paths`` has a typo.

``bernstein volunteer verify`` closes that loop locally::

    bernstein volunteer verify                 # this checkout
    bernstein volunteer verify /path/to/repo   # somewhere else
    bernstein volunteer verify --json          # for a CI step

The digest it prints is the point of the command, not decoration. It is the
same content address a result receipt binds itself to, so a maintainer learns
here what their submissions will be checked against, and anyone can re-derive
it from the committed file.

Only verbs whose backing code exists live here. An absent subcommand is honest;
one that prints "not implemented yet" is a promise the code has not made.
"""

from __future__ import annotations

import json
import warnings
from pathlib import Path
from typing import TYPE_CHECKING, Any

import click

if TYPE_CHECKING:
    from bernstein.core.volunteer import VolunteerManifest


@click.group("volunteer")
def volunteer_group() -> None:
    """Volunteer-worker surfaces: donate agent capacity to opt-in projects.

    A project declares its policy in `.bernstein/volunteer.json`; see
    `docs/reference/volunteer-manifest.md` for the schema.
    """


@volunteer_group.command("verify")
@click.argument(
    "repo_root",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
    default=".",
    required=False,
)
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def verify_cmd(repo_root: Path, as_json: bool) -> None:
    """Validate a project's volunteer manifest and print its digest.

    Exits non-zero on any rejection, naming the field at fault rather than
    raising a traceback at a maintainer who is editing a config file.
    """
    from bernstein.core.volunteer import (
        VOLUNTEER_MANIFEST_PATH,
        UnenforcedManifestFieldWarning,
        VolunteerManifestError,
        effective_egress,
        load_manifest_from_repo,
    )

    manifest_path = repo_root / VOLUNTEER_MANIFEST_PATH

    # Unenforced fields are a warning from the loader, not a return value.
    # Catching them here is what turns "your build is older than this policy"
    # from a stderr line into part of the report.
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always", UnenforcedManifestFieldWarning)
        try:
            manifest = load_manifest_from_repo(repo_root)
        except FileNotFoundError:
            _fail(
                "the project has not opted in to volunteer work",
                field="<file>",
                as_json=as_json,
                path=manifest_path,
            )
            return
        # Must stay below the FileNotFoundError clause: that is a subclass of
        # OSError, and above it every project without a manifest would be told
        # its manifest is unreadable. Its own clause rather than a widening of
        # the one above, because the two send the reader to different fixes --
        # "you have not opted in" points a maintainer whose file is chmod 000 at
        # adding a file they already have.
        except OSError as exc:
            _fail(
                f"the manifest exists but could not be read: {exc.strerror or exc}",
                field="<unreadable>",
                as_json=as_json,
                path=manifest_path,
            )
            return
        except VolunteerManifestError as exc:
            _fail(str(exc).split(": ", 1)[-1], field=exc.field, as_json=as_json, path=manifest_path)
            return

    unenforced = [str(w.message) for w in caught if issubclass(w.category, UnenforcedManifestFieldWarning)]

    if as_json:
        click.echo(json.dumps(_report(manifest, manifest_path, effective_egress(manifest), unenforced), indent=2))
        return

    _print_report(manifest, manifest_path, effective_egress(manifest), unenforced)


@volunteer_group.command("browse")
@click.option(
    "--index",
    "index_urls",
    multiple=True,
    help="HTTPS URL of a volunteer index JSON document. Can be repeated.",
)
@click.option("--size", default=None, help="Filter by size label (e.g., 's', 'm').")
@click.option("--language", default=None, help="Filter by language topic.")
@click.option("--local-ok", "local_ok_only", is_flag=True, help="Only show projects that accept local models.")
@click.option("--budget", "budget_minutes", type=int, default=None, help="Max wall-clock minutes you will provide.")
@click.option("--verbose", is_flag=True, help="Show dropped projects with reasons.")
@click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
def browse_cmd(
    index_urls: tuple[str, ...],
    size: str | None,
    language: str | None,
    local_ok_only: bool,
    budget_minutes: int | None,
    verbose: bool,
    as_json: bool,
) -> None:
    """Browse opt-in volunteer projects from one or more indexes.

    Fetches configured indexes (HTTPS only), merges and deduplicates by repo,
    validates each project's ``.bernstein/volunteer.json``, and filters by
    your donor preferences.

    \b
    Examples::

        bernstein volunteer browse --index https://example.test/index.json
        bernstein volunteer browse --index https://a.test/i.json --index https://b.test/i.json \\
            --local-ok --language python
        bernstein volunteer browse --verbose
    """
    from bernstein.core.volunteer.registry import browse_indexes

    if not index_urls:
        _fail("no index URLs provided; use --index at least once", field="--index", as_json=as_json, path=Path("<cli>"))
        return

    joinable, dropped = browse_indexes(
        list(index_urls),
        size=size,
        language=language,
        local_ok_only=local_ok_only,
        budget_minutes=budget_minutes,
    )

    if as_json:
        payload = {
            "joinable": [
                {
                    "repo_url": r.repo_url,
                    "default_branch": r.default_branch,
                    "manifest_url": r.manifest_url,
                    "manifest_sha256": r.digest,
                    "license": r.manifest.license,
                    "local_ok": r.manifest.local_ok,
                    "max_wall_clock_minutes": r.manifest.max_wall_clock_minutes,
                    "task_label": r.manifest.task_label,
                    "topics": list(r.topics),
                }
                for r in joinable
            ],
            "dropped": [{"repo_url": d.repo_url, "reason": d.reason} for d in dropped] if verbose else [],
        }
        click.echo(json.dumps(payload, indent=2))
        return

    if not joinable:
        click.echo("No joinable projects found.")
    else:
        for r in joinable:
            click.echo(f"  {r.repo_url}")
            click.echo(f"    digest     {r.digest}")
            click.echo(f"    license    {r.manifest.license}")
            click.echo(f"    local ok   {'yes' if r.manifest.local_ok else 'no'}")
            click.echo(f"    wall clock {r.manifest.max_wall_clock_minutes} min")
            click.echo(f"    task label {r.manifest.task_label}")
            if r.topics:
                click.echo(f"    topics     {', '.join(r.topics)}")

    if verbose and dropped:
        click.echo("\nDropped:")
        for d in dropped:
            click.echo(f"  {d.repo_url}: {d.reason}")


def _report(
    manifest: VolunteerManifest,
    path: Path,
    egress: tuple[str, ...],
    unenforced: list[str],
) -> dict[str, Any]:
    """The verdict as a record, keyed the way a receipt keys it.

    ``manifest_sha256`` matches the field name the sandbox refusal record and
    the result bundle already use, so a caller can join on it without a lookup
    table.
    """
    return {
        "ok": True,
        "path": str(path),
        "manifest_sha256": manifest.digest,
        "manifest": manifest.to_canonical_dict(),
        "effective_egress": list(egress),
        "unenforced_fields": unenforced,
    }


def _print_report(
    manifest: VolunteerManifest,
    path: Path,
    egress: tuple[str, ...],
    unenforced: list[str],
) -> None:
    click.echo(f"✓ {path}")
    click.echo(f"  digest              {manifest.digest}")
    click.echo(f"  license             {manifest.license}")
    click.echo(f"  sandbox             {manifest.sandbox}")
    click.echo(f"  wall clock          {manifest.max_wall_clock_minutes} min")
    click.echo(f"  task label          {manifest.task_label}")
    click.echo(f"  local models ok     {'yes' if manifest.local_ok else 'no'}")
    click.echo(f"  allowed paths       {', '.join(manifest.allowed_paths) or 'repo-wide'}")
    for index, gate in enumerate(manifest.gates):
        click.echo(f"  gate {index + 1:<15}{gate}")
    # An empty `egress_allowlist` reads as "no network", and it is not: the
    # sandbox profile adds the package registries or the gates cannot install
    # anything. Printing what a donor will actually be able to reach is the
    # difference between a policy a maintainer wrote and one they understood.
    click.echo(f"  reachable hosts     {', '.join(egress)}")
    for message in unenforced:
        click.echo(f"! {message}", err=True)


def _fail(message: str, *, field: str, as_json: bool, path: Path) -> None:
    if as_json:
        click.echo(
            json.dumps({"ok": False, "path": str(path), "field": field, "error": message}, indent=2),
            err=True,
        )
    else:
        click.echo(f"✗ {path}", err=True)
        click.echo(f"  {field}: {message}", err=True)
    raise SystemExit(1)
