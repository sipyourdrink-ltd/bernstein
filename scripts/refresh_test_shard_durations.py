#!/usr/bin/env python3
"""Refresh ``tests/fixtures/ci/test-shard-durations.json`` from a CI run's logs.

Harvests per-file wall times from ``scripts/run_tests.py`` PASS/FAIL lines in a
GitHub Actions run log zip, maps basenames onto ``tests/unit/**/test_*.py``
paths, and writes the committed durations document consumed by ``shard_files``.

Usage:
    uv run python scripts/refresh_test_shard_durations.py --run-id 33330849513
    uv run python scripts/refresh_test_shard_durations.py --run-id 33330849513 --repo sipyourdrink-ltd/bernstein

Basename collisions (duplicate ``test_*.py`` names under different dirs) get
the average of that basename's observed timings assigned to every colliding
path — good enough to keep them off the long pole until a
``run_tests.py --record-durations`` pass records unambiguous relative paths.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import zipfile
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(Path(__file__).resolve().parent))

from run_tests import (  # noqa: E402
    DEFAULT_SHARD_DURATIONS_REL,
    default_shard_durations_path,
    write_shard_durations,
)

_RESULT_LINE = re.compile(r"(?:PASS|FAIL|NO TESTS)\s+\[\d+/\d+\]\s+(\S+\.py)\s+\(([\d.]+)s\)")


def _download_run_logs(repo: str, run_id: int, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    with dest.open("wb") as handle:
        subprocess.check_call(
            [
                "gh",
                "api",
                f"repos/{repo}/actions/runs/{run_id}/logs",
                "-H",
                "Accept: application/vnd.github+json",
            ],
            stdout=handle,
        )


def _iter_ubuntu_shard_logs(zf: zipfile.ZipFile) -> list[str]:
    names: list[str] = []
    for name in zf.namelist():
        if not name.endswith(".txt") or name.endswith("system.txt"):
            continue
        lowered = name.lower()
        if "ubuntu" in lowered and "shard" in lowered:
            names.append(name)
    return names


def harvest_basename_durations(log_zip: Path) -> dict[str, list[float]]:
    """Return ``{basename: [seconds, ...]}`` from ubuntu shard job logs."""
    by_name: dict[str, list[float]] = defaultdict(list)
    with zipfile.ZipFile(log_zip) as zf:
        for name in _iter_ubuntu_shard_logs(zf):
            text = zf.read(name).decode("utf-8", errors="replace")
            for match in _RESULT_LINE.finditer(text):
                by_name[match.group(1)].append(float(match.group(2)))
    return dict(by_name)


def map_basenames_to_paths(
    by_name: dict[str, list[float]],
    test_root: Path,
) -> dict[str, float]:
    """Map harvested basename timings onto repo-relative POSIX paths."""
    name_to_paths: dict[str, list[str]] = defaultdict(list)
    for path in sorted(test_root.rglob("test_*.py")):
        rel = path.resolve().relative_to(REPO_ROOT.resolve()).as_posix()
        name_to_paths[path.name].append(rel)

    durations: dict[str, float] = {}
    for name, samples in by_name.items():
        if not samples:
            continue
        average = sum(samples) / len(samples)
        for rel in name_to_paths.get(name, []):
            durations[rel] = average
    return durations


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-id", type=int, required=True, help="GitHub Actions run id")
    parser.add_argument(
        "--repo",
        default="sipyourdrink-ltd/bernstein",
        help="owner/name of the repository that produced the run",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=default_shard_durations_path(),
        help=f"durations JSON to write (default: {DEFAULT_SHARD_DURATIONS_REL})",
    )
    parser.add_argument(
        "--test-dir",
        type=Path,
        default=REPO_ROOT / "tests" / "unit",
        help="test tree used to resolve basenames to paths",
    )
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        log_zip = Path(tmp) / "logs.zip"
        print(f"Downloading logs for {args.repo} run {args.run_id}...")
        _download_run_logs(args.repo, args.run_id, log_zip)
        by_name = harvest_basename_durations(log_zip)

    durations = map_basenames_to_paths(by_name, args.test_dir)
    write_shard_durations(args.output, durations, merge=False)
    print(
        json.dumps(
            {
                "output": str(args.output),
                "basenames_harvested": len(by_name),
                "paths_written": len(durations),
                "total_seconds": round(sum(durations.values()), 1),
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
