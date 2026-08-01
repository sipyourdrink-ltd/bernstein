#!/usr/bin/env python3
"""Render the RPM spec at a release version and build the source RPM.

``packaging/rpm/bernstein.spec`` carries whatever version was last committed.
The release chain has to ship the *tag* version, so this renderer is the one
place that binds the two together: it rewrites ``Version:``, records the
release in ``%changelog``, and hands the resulting SRPM to ``rpmbuild -bs``.

The rendering half is pure, so the version binding is testable without an
rpmbuild on the machine (macOS developer boxes usually have none).

Usage::

    python3 scripts/build_copr_srpm.py --version v3.13.0 --outdir /tmp/copr
    python3 scripts/build_copr_srpm.py --version v3.13.0 --render-only
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from datetime import UTC, date, datetime
from pathlib import Path

SPEC_PATH = Path("packaging/rpm/bernstein.spec")
CHANGELOG_AUTHOR = "Bernstein release automation <alex@alexchernysh.com>"
CHANGELOG_MARKER = "%changelog\n"

VERSION_LINE_RE = re.compile(r"(?m)^Version:(?P<pad>[ \t]+)\S+[ \t]*$")
RPM_VERSION_RE = re.compile(r"[0-9][0-9A-Za-z.~+_]*")

_WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
_MONTHS = (
    "Jan",
    "Feb",
    "Mar",
    "Apr",
    "May",
    "Jun",
    "Jul",
    "Aug",
    "Sep",
    "Oct",
    "Nov",
    "Dec",
)


def rpm_version(tag_or_version: str) -> str:
    """Return an RPM-legal ``Version:`` field for a release tag.

    RPM forbids ``-`` in ``Version:``; ``~`` is the pre-release separator that
    sorts *before* the matching final release, which is what a ``-rc1`` tag
    means.
    """
    version = tag_or_version.strip().removeprefix("v")
    if not version:
        msg = f"{tag_or_version!r} carries no release version"
        raise ValueError(msg)
    version = version.replace("-", "~")
    if not RPM_VERSION_RE.fullmatch(version):
        msg = f"{tag_or_version!r} is not an RPM-legal version (got {version!r})"
        raise ValueError(msg)
    return version


def changelog_stamp(day: date) -> str:
    """Return the RPM ``%changelog`` date stamp for ``day``.

    Built from explicit tables rather than ``strftime`` so the stamp cannot
    change with the runner's locale, and so a wrong weekday - which makes
    rpmbuild warn on every build - is impossible.
    """
    return f"{_WEEKDAYS[day.weekday()]} {_MONTHS[day.month - 1]} {day.day:02d} {day.year}"


def render_spec(spec_text: str, version: str, build_date: date) -> str:
    """Return ``spec_text`` bound to ``version``, with a changelog entry."""
    release_version = rpm_version(version)

    rendered, replaced = VERSION_LINE_RE.subn(
        lambda match: f"Version:{match.group('pad')}{release_version}",
        spec_text,
        count=1,
    )
    if replaced != 1:
        msg = "spec has no `Version:` line to bind to the release"
        raise ValueError(msg)

    entry_header = f"* {changelog_stamp(build_date)} {CHANGELOG_AUTHOR} - {release_version}-1"
    if CHANGELOG_MARKER not in rendered:
        return f"{rendered.rstrip()}\n\n{CHANGELOG_MARKER}{entry_header}\n- Release {release_version}\n"

    head, _, tail = rendered.partition(CHANGELOG_MARKER)
    if tail.startswith(f"{entry_header}\n"):
        # Re-rendering the same release must not stack duplicate entries.
        return rendered
    return f"{head}{CHANGELOG_MARKER}{entry_header}\n- Release {release_version}\n\n{tail}"


def build_srpm(spec_text: str, outdir: Path) -> Path:
    """Build a source RPM from ``spec_text`` and return its path."""
    outdir.mkdir(parents=True, exist_ok=True)
    spec_file = outdir / SPEC_PATH.name
    spec_file.write_text(spec_text, encoding="utf-8")

    for stale in outdir.glob("*.src.rpm"):
        stale.unlink()

    # Fixed argv, no shell.
    result = subprocess.run(
        [
            "rpmbuild",
            "-bs",
            "--define",
            f"_topdir {outdir}",
            "--define",
            f"_sourcedir {outdir}",
            "--define",
            f"_srcrpmdir {outdir}",
            str(spec_file),
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    # rpmbuild chatter belongs in the job log, not on stdout: stdout carries
    # the SRPM path so the caller can consume it directly.
    print(result.stdout, file=sys.stderr, end="")
    print(result.stderr, file=sys.stderr, end="")
    if result.returncode != 0:
        msg = f"rpmbuild -bs failed with exit code {result.returncode}"
        raise RuntimeError(msg)

    built = sorted(outdir.glob("*.src.rpm"))
    if len(built) != 1:
        msg = f"expected exactly one SRPM in {outdir}, found {[p.name for p in built]}"
        raise RuntimeError(msg)
    return built[0]


def main(argv: list[str] | None = None) -> int:
    """CLI entrypoint."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, help="Release tag or version, e.g. v3.13.0")
    parser.add_argument("--spec", type=Path, default=SPEC_PATH, help="Spec file to render")
    parser.add_argument("--outdir", type=Path, default=Path("dist-rpm"), help="Build directory")
    parser.add_argument(
        "--render-only",
        action="store_true",
        help="Print the rendered spec instead of building the SRPM.",
    )
    parser.add_argument(
        "--date",
        default=None,
        help="Changelog date as YYYY-MM-DD (default: today in UTC).",
    )
    args = parser.parse_args(argv)

    build_date = date.fromisoformat(str(args.date)) if args.date else datetime.now(tz=UTC).date()
    spec_path: Path = args.spec
    rendered = render_spec(spec_path.read_text(encoding="utf-8"), str(args.version), build_date)

    if bool(args.render_only):
        print(rendered, end="")
        return 0

    print(build_srpm(rendered, args.outdir))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
