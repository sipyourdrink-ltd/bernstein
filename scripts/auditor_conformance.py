#!/usr/bin/env python3
"""Regenerate and score the auditor conformance suite.

Two subcommands:

``regenerate``
    Re-run the recorded scenario and rewrite
    ``tests/conformance/auditor/fixture/``. The fixture is produced by
    running the scenario through the production writers; it is never
    edited by hand.

``score``
    Run the vectors and print how many of the 21 questions the exported
    bundle answers, e.g. ``auditor conformance: 1/21``. The exit code is
    pytest's, so a vector that stops passing fails the build rather than
    only lowering a number nobody reads.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.conformance.auditor import recorder  # noqa: E402
from tests.conformance.auditor.scoreboard import REPORT_PATH_ENV, read_report  # noqa: E402

VECTOR_DIR = Path("tests/conformance/auditor")
VECTOR_GLOB = "test_*vectors.py"
SCORE_PLUGIN = "tests.conformance.auditor.scoreboard"


def vector_files() -> tuple[str, ...]:
    """Return every vector module under :data:`VECTOR_DIR`, in path order.

    The modules were listed here by hand, and two of the four that existed
    answered questions the score never ran: a vector file had to be named in
    this script as well as written, and the second half was easy to forget.
    Nothing distinguishes a vector module except its name, so the directory
    listing is the list: a file added under that name is scored, and a file
    that disappears makes the accompanying test fail rather than quietly
    lowering the number.
    """
    return tuple(sorted(str(path.relative_to(ROOT).as_posix()) for path in (ROOT / VECTOR_DIR).glob(VECTOR_GLOB)))


def regenerate(destination: Path) -> int:
    """Re-record the scenario into *destination* and report what was written."""
    recording = recorder.record(destination)
    print(f"recorded {recording.run_id} into {recording.bundle_root}")
    for name in sorted(path.name for path in recording.bundle_root.iterdir()):
        print(f"  bundle/{name}")
    print(f"  trust/{recording.trust_anchor.name}")
    return 0


def score() -> int:
    """Run the vectors, print ``n/21``, and return pytest's exit code."""
    configured = os.environ.get(REPORT_PATH_ENV)
    with tempfile.TemporaryDirectory() as scratch:
        report_path = Path(configured) if configured else Path(scratch) / "score.json"
        env = dict(os.environ)
        env[REPORT_PATH_ENV] = str(report_path)
        env["UV_NO_SYNC"] = "1"
        completed = subprocess.run(
            [
                sys.executable,
                "-m",
                "pytest",
                *vector_files(),
                "-q",
                "--no-cov",
                "-p",
                SCORE_PLUGIN,
            ],
            cwd=ROOT,
            env=env,
            check=False,
        )
        if not report_path.is_file():
            print("auditor conformance: no score report was produced", file=sys.stderr)
            return completed.returncode or 1
        result = read_report(report_path)
    print(f"auditor conformance: {result}")
    if result.failed:
        print(f"unanswered after running: {list(result.failed)}")
    return completed.returncode


def main(argv: list[str] | None = None) -> int:
    """Parse arguments and dispatch. Returns the process exit code."""
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)

    regen = sub.add_parser("regenerate", help="re-record the scenario and rewrite the fixture")
    regen.add_argument(
        "--destination",
        type=Path,
        default=ROOT / recorder.FIXTURE_RELATIVE_PATH,
        help="where to write the recording (default: the committed fixture)",
    )
    sub.add_parser("score", help="run the vectors and print n/21")

    args = parser.parse_args(argv)
    if args.command == "regenerate":
        return regenerate(args.destination)
    return score()


if __name__ == "__main__":
    sys.exit(main())
