#!/usr/bin/env python3
"""Print how many of the 21 auditor questions the evidence answers.

The suite under ``tests/integration/conformance/auditor/`` asks one
recorded run the questions somebody actually asks after an incident.
Most of them do not have an answer yet, and each unanswered one is a
strict ``xfail`` naming the work that would change that. The score is
the point, so it has to be quotable without reading the file::

    uv run python scripts/auditor_scoreboard.py

Exit code is pytest's, so this doubles as the suite's own gate: a
question that used to be answered and no longer is fails the run.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import TYPE_CHECKING, cast

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

REPO_ROOT = Path(__file__).resolve().parents[1]
SUITE_PATH = "tests/integration/conformance/auditor"
SCOREBOARD_ENV_VAR = "BERNSTEIN_AUDITOR_SCOREBOARD"


def render(payload: Mapping[str, object]) -> str:
    """Render a scoreboard payload as human-readable lines.

    Args:
        payload: The JSON the suite wrote - ``total``, ``answered`` and
            ``outcomes``.

    Returns:
        The rendered scoreboard, headline line last so it survives a
        truncated terminal scroll.
    """
    total = int(cast("int", payload["total"]))
    outcomes = cast("Mapping[str, str]", payload.get("outcomes") or {})
    answered = cast("Sequence[int]", payload.get("answered") or [])
    lines: list[str] = []
    for number in sorted(int(n) for n in outcomes):
        mark = "answered" if outcomes[str(number)] == "answered" else "unanswered"
        lines.append(f"  Q{number:<3} {mark}")
    lines.append(f"asked {len(outcomes)}/{total}, answered {len(answered)}/{total}")
    return "\n".join(lines)


def _run_suite(scoreboard_path: Path, pytest_args: list[str]) -> int:
    """Run the conformance suite with the scoreboard hook armed."""
    env = os.environ.copy()
    env[SCOREBOARD_ENV_VAR] = str(scoreboard_path)
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", SUITE_PATH, *pytest_args],
        cwd=str(REPO_ROOT),
        env=env,
        check=False,
    )
    return proc.returncode


def main(argv: list[str] | None = None) -> int:
    """Run the suite and print ``n/21``. Returns pytest's exit code."""
    parser = argparse.ArgumentParser(description="Score the auditor conformance suite.")
    parser.add_argument(
        "--json",
        dest="json_path",
        type=Path,
        default=None,
        help="Also write the machine-readable scoreboard here.",
    )
    parser.add_argument(
        "pytest_args",
        nargs=argparse.REMAINDER,
        help="Extra arguments passed through to pytest.",
    )
    args = parser.parse_args(argv)

    with tempfile.TemporaryDirectory() as tmp:
        scoreboard_path = Path(tmp) / "scoreboard.json"
        exit_code = _run_suite(scoreboard_path, list(args.pytest_args))
        if not scoreboard_path.is_file():
            print("ERROR: the suite did not report a scoreboard", file=sys.stderr)
            return exit_code or 1
        raw = scoreboard_path.read_text(encoding="utf-8")

    payload = json.loads(raw)
    print(render(payload))
    if args.json_path is not None:
        args.json_path.parent.mkdir(parents=True, exist_ok=True)
        args.json_path.write_text(raw, encoding="utf-8")
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
