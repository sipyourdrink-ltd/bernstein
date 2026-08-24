#!/usr/bin/env python3
"""Wait until a release is resolvable from the index ``pip`` actually reads.

Why this exists
---------------
The RPM install smokes build a spec whose ``%install`` runs
``pip install bernstein==VERSION``. PyPI can lag a publish by a short
window, so the workflow waits first, to keep "not yet visible" from being
reported as "the RPM is broken".

That wait used to poll ``https://pypi.org/pypi/bernstein/<version>/json``.
The JSON API and the simple index are separate surfaces with separate
caches and separate propagation, and ``pip`` resolves against the simple
index. A green wait therefore proved nothing about the step that followed
it: on the v3.15.1 publish the wait passed and two of four chroots then
failed with ``No matching distribution found for bernstein==3.15.1``
(#3815).

So the poll asks the resolver surface, and asks it the precise question
the next step will ask: not "does the project page mention this version"
but "is there a distribution file for exactly this version". A project
page that lists the version in some other form is not enough, because it
is not enough for ``pip`` either.

A timeout stays fatal. An index that has not converged within the budget
is worth a human look, and the exit message says which of the two cases
it is so the run summary stays readable.
"""

from __future__ import annotations

import argparse
import re
import sys
import time
import urllib.error
import urllib.request
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable
    from typing import TextIO

DEFAULT_INDEX_URL = "https://pypi.org/simple/bernstein/"
DEFAULT_ATTEMPTS = 30
DEFAULT_DELAY_SECONDS = 10.0
DEFAULT_PROJECT = "bernstein"


def _normalize(version: str) -> str:
    """Normalize a version the way a distribution filename spells it.

    ``-`` is the field separator in a wheel filename, so packaging tools
    escape any ``-`` inside the version itself to ``_`` (PEP 427). Compare
    against that spelling rather than the tag's.
    """
    return version.strip().lower().replace("-", "_")


def _distribution_filenames(project: str, version: str) -> tuple[re.Pattern[str], ...]:
    """The filename shapes that prove ``project==version`` is installable."""
    name = re.escape(project)
    ver = re.escape(_normalize(version))
    return (
        # Wheel: name-version-pytag-abitag-platformtag.whl
        re.compile(rf"\b{name}-{ver}-[^/\"']*\.whl\b", re.IGNORECASE),
        # Source distribution: name-version.tar.gz
        re.compile(rf"\b{name}-{ver}\.tar\.gz\b", re.IGNORECASE),
    )


def index_has_version(body: str, project: str, version: str) -> bool:
    """True when the simple-index body offers a file for exactly ``version``.

    Matching on the distribution filename is what makes this stricter than
    the JSON API check it replaced: the project page mentioning the version
    anywhere in prose or in a neighbouring version's filename does not
    satisfy it.
    """
    return any(pattern.search(body) for pattern in _distribution_filenames(project, version))


def _fetch(url: str, timeout: float) -> str | None:
    """Return the index body, or ``None`` when it could not be read."""
    request = urllib.request.Request(
        url,
        headers={"Accept": "text/html", "User-Agent": "bernstein-release-gate"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.read().decode("utf-8", errors="replace")
    except (urllib.error.URLError, OSError, ValueError):
        return None


def wait_for_visibility(
    version: str,
    *,
    index_url: str = DEFAULT_INDEX_URL,
    project: str = DEFAULT_PROJECT,
    attempts: int = DEFAULT_ATTEMPTS,
    delay: float = DEFAULT_DELAY_SECONDS,
    sleep: Callable[[float], object] = time.sleep,
    stream: TextIO = sys.stderr,
) -> int:
    """Poll ``index_url`` until ``version`` is resolvable. Return an exit code."""
    reachable = False
    for attempt in range(1, attempts + 1):
        body = _fetch(index_url, timeout=30.0)
        if body is not None:
            reachable = True
            if index_has_version(body, project, version):
                print(f"{project} {version} is resolvable from {index_url}", file=stream)
                return 0
        if attempt < attempts:
            sleep(delay)

    if reachable:
        print(
            f"::error::PyPI index propagation timeout: {project} {version} never became resolvable "
            f"from {index_url} within the wait budget ({attempts} attempts). The index was reachable "
            f"but PyPI index propagation has not converged. This is an index propagation delay, "
            f"not a packaging defect in {project}.",
            file=stream,
        )
    else:
        print(
            f"::error::{index_url} could not be read on any of {attempts} attempts, "
            f"so visibility of {project} {version} was never established. Treat this "
            f"as an index or network fault, not as a broken build.",
            file=stream,
        )
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("version", help="Release version to wait for, without a leading 'v'.")
    parser.add_argument("--index-url", default=DEFAULT_INDEX_URL, help="Simple-index URL for the project.")
    parser.add_argument("--project", default=DEFAULT_PROJECT, help="Project name as it appears in filenames.")
    parser.add_argument("--attempts", type=int, default=DEFAULT_ATTEMPTS, help="Number of polls before giving up.")
    parser.add_argument("--delay", type=float, default=DEFAULT_DELAY_SECONDS, help="Seconds between polls.")
    args = parser.parse_args(argv)

    return wait_for_visibility(
        args.version,
        index_url=args.index_url,
        project=args.project,
        attempts=args.attempts,
        delay=args.delay,
    )


if __name__ == "__main__":
    raise SystemExit(main())
