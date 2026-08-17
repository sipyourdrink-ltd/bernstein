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
