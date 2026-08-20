#!/usr/bin/env python3
"""Report transitive pins in ``docs/requirements.txt`` a fresh resolve would move.

Why this exists
---------------
#3999 gates the compiled file against the constraints its ``.in`` declares.
That check is offline by design: it proves every *direct* requirement is
present and inside its declared bound, and says nothing about the other 39
entries, which are transitive and which nothing ever re-resolves.

A transitive pin can sit at a yanked release, or one a direct dependency's
own metadata no longer permits, and every existing gate still reports green.
The direct-constraint check passes because the four direct requirements are
fine; the docs build passes because the pinned wheels are still downloadable.
The staleness is invisible until a build breaks or somebody regenerates by
hand (#4001).

Answering it means reaching a live index, which is why this is a weekly
scheduled job rather than a per-PR gate. A blocking gate that can go red
because an index hiccuped stops being a blocking gate, and a non-blocking
gate is another green tick that proves nothing.

What it does NOT do
-------------------
It does not change any pin, and it does not open a pull request. The report
names which packages moved and by how much; deciding whether to take them is
a judgement that depends on things this script cannot see.

That is the line between this and the manifest regeneration in #4053: a
manifest is a pure function of ``pyproject.toml`` with one right answer, so
that one is automated end to end. A resolution is not, and a weekly PR
touching 39 pins is read by nobody.

Usage:
    python scripts/check_docs_requirements_staleness.py
    python scripts/check_docs_requirements_staleness.py --json

Exit codes:
    0  every pin matches what a fresh resolve produces
    1  at least one pin moved (each named on stdout)
    2  the recompile could not be run or its output could not be parsed
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Final

REPO_ROOT: Final = Path(__file__).resolve().parent.parent
REQUIREMENTS_IN: Final = REPO_ROOT / "docs" / "requirements.in"
REQUIREMENTS_TXT: Final = REPO_ROOT / "docs" / "requirements.txt"

#: The compiler invocation, matching the one ``docs/requirements.in``
#: documents and ``docs/requirements.txt``'s own header records. Held to the
#: ``.in`` header by a test, because a recompile built on a command that does
#: not reproduce the committed file reports every package as moved on its
#: first run (#3995).
PIP_COMPILE_ARGV: Final = (
    "uv",
    "run",
    "--python",
    "3.13",
    "--with",
    "pip-tools",
    "--",
    "pip-compile",
    "--generate-hashes",
    "--strip-extras",
    "--quiet",
    "--output-file",
)

#: A pinned line in a ``--generate-hashes`` output. Hash continuations are
#: indented and deliberately do not match.
_PIN_RE: Final = re.compile(r"^(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)==(?P<version>[^\s\\;]+)")


@dataclass(frozen=True)
class PinDrift:
    """One package whose committed pin differs from a fresh resolve."""

    package: str
    committed: str | None
    resolved: str | None

    @property
    def kind(self) -> str:
        if self.committed is None:
            return "added"
        if self.resolved is None:
            return "removed"
        return "moved"

    def render(self) -> str:
        if self.committed is None:
            return f"  {self.package}: absent from the committed file, a fresh resolve adds {self.resolved}"
        if self.resolved is None:
            return f"  {self.package}: pinned at {self.committed}, a fresh resolve no longer includes it"
        return f"  {self.package}: pinned at {self.committed}, a fresh resolve produces {self.resolved}"


def parse_pins(text: str) -> dict[str, str]:
    """Package name -> pinned version, from a compiled requirements file."""
    pins: dict[str, str] = {}
    for raw in text.splitlines():
        if not raw or raw[0].isspace() or raw.lstrip().startswith("#"):
            continue
        match = _PIN_RE.match(raw.strip())
        if match is not None:
            pins[match.group("name").lower().replace("_", "-")] = match.group("version")
    return pins


def diff_pins(committed: dict[str, str], resolved: dict[str, str]) -> list[PinDrift]:
    """Every package the two resolutions disagree about, name-sorted."""
    return [
        PinDrift(package=name, committed=committed.get(name), resolved=resolved.get(name))
        for name in sorted(set(committed) | set(resolved))
        if committed.get(name) != resolved.get(name)
    ]


def recompile(destination: Path) -> str:
    """Resolve ``docs/requirements.in`` fresh against a live index."""
    result = subprocess.run(
        [*PIP_COMPILE_ARGV, str(destination), str(REQUIREMENTS_IN)],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=600,
    )
    if result.returncode != 0:
        msg = f"pip-compile exited {result.returncode}:\n{result.stderr.strip()}"
        raise RuntimeError(msg)
    return destination.read_text(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--json", action="store_true", help="Emit the drift list as JSON.")
    args = parser.parse_args(argv)

    committed = parse_pins(REQUIREMENTS_TXT.read_text(encoding="utf-8"))
    if not committed:
        print(f"error: no pins parsed from {REQUIREMENTS_TXT}", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory() as tmp:
        try:
            fresh = recompile(Path(tmp) / "resolved.txt")
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            print(f"error: could not recompile: {exc}", file=sys.stderr)
            return 2

    resolved = parse_pins(fresh)
    if not resolved:
        print("error: the recompile produced no pins", file=sys.stderr)
        return 2

    drifts = diff_pins(committed, resolved)

    if args.json:
        print(json.dumps([{**asdict(d), "kind": d.kind} for d in drifts], indent=2))
        return 1 if drifts else 0

    if not drifts:
        print(f"OK       all {len(committed)} pins match a fresh resolve")
        return 0

    print(f"STALE    {len(drifts)} of {len(committed)} pins differ from a fresh resolve:")
    for drift in drifts:
        print(drift.render())
    print()
    print("These are transitive unless named in docs/requirements.in. Taking them is a")
    print("judgement call, not a mechanical update, which is why this reports rather")
    print("than opening a pull request. To accept them all:")
    print("    uv run --python 3.13 --with pip-tools -- pip-compile --generate-hashes \\")
    print("      --strip-extras --output-file docs/requirements.txt docs/requirements.in")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
