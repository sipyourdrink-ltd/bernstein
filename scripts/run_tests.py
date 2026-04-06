#!/usr/bin/env python3
"""Run each test file in a separate subprocess to prevent memory leaks.

pytest keeps references to test objects for the entire session. With 2000+
tests this can grow to 100+ GB. Running each file in its own process caps
memory at whatever a single file needs (~200MB max).

Usage:
    python scripts/run_tests.py              # run all unit tests (parallel by default)
    python scripts/run_tests.py -x           # stop on first failure
    python scripts/run_tests.py -k adapter   # filter by keyword
    python scripts/run_tests.py --parallel 4 # run 4 files at once
    python scripts/run_tests.py --parallel 1 # force sequential execution
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path


def _default_workers() -> int:
    """Pick a sensible default worker count: min(cpu_count, 8), at least 1."""
    cpus = os.cpu_count() or 1
    return min(cpus, 8)


def discover_test_files(test_dir: Path, keyword: str | None = None) -> list[Path]:
    """Find all test_*.py files, optionally filtered by keyword."""
    files = sorted(test_dir.glob("test_*.py"))
    if keyword:
        files = [f for f in files if keyword in f.stem]
    return files


def run_file(path: Path, extra_args: list[str]) -> tuple[Path, int, float, str]:
    """Run a single test file in a subprocess. Returns (path, exitcode, duration, output)."""
    cmd = [sys.executable, "-m", "pytest", str(path), "-x", "-q", "--tb=short", "-p", "no:cacheprovider", "-s", *extra_args]
    start = time.monotonic()
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    duration = time.monotonic() - start
    output = result.stdout + result.stderr
    return path, result.returncode, duration, output


def run_sequential(files: list[Path], extra_args: list[str], fail_fast: bool) -> int:
    """Run test files one by one."""
    passed = 0
    failed = 0
    total_duration = 0.0

    for i, path in enumerate(files, 1):
        label = f"[{i}/{len(files)}] {path.name}"
        try:
            _fpath, code, duration, output = run_file(path, extra_args)
        except subprocess.TimeoutExpired:
            print(f"  TIMEOUT {label} (>120s)")
            failed += 1
            if fail_fast:
                break
            continue

        total_duration += duration
        if code == 0:
            passed += 1
            # Extract test count from output
            last_line = [ln for ln in output.strip().split("\n") if ln.strip()][-1] if output.strip() else ""
            print(f"  PASS {label} ({duration:.1f}s) {last_line}")
        elif code == 5:
            # No tests collected — not a failure
            passed += 1
            print(f"  SKIP {label} (no tests)")
        else:
            failed += 1
            print(f"  FAIL {label} ({duration:.1f}s)")
            # Show failure details
            for line in output.strip().split("\n"):
                if line.strip():
                    print(f"       {line}")
            if fail_fast:
                break

    print(f"\n{'=' * 60}")
    print(f"Files: {passed} passed, {failed} failed, {len(files)} total")
    print(f"Time:  {total_duration:.1f}s")
    return 1 if failed else 0


def run_parallel(files: list[Path], extra_args: list[str], workers: int, fail_fast: bool) -> int:
    """Run test files in parallel using concurrent.futures."""
    from concurrent.futures import ProcessPoolExecutor, as_completed

    passed = 0
    failed = 0
    done = 0
    total = len(files)
    abort = False
    wall_start = time.monotonic()

    print(f"  Workers: {workers}")

    with ProcessPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(run_file, f, extra_args): f for f in files}
        for future in as_completed(futures):
            if abort:
                future.cancel()
                continue
            try:
                fpath, code, duration, output = future.result(timeout=360)
            except Exception as exc:
                fpath = futures[future]
                done += 1
                print(f"  ERROR [{done}/{total}] {fpath.name}: {exc}")
                failed += 1
                if fail_fast:
                    abort = True
                    for f in futures:
                        f.cancel()
                continue

            done += 1
            if code == 0 or code == 5:
                passed += 1
                status = "SKIP" if code == 5 else "PASS"
                last_line = ""
                if code == 0:
                    last_line = [ln for ln in output.strip().split("\n") if ln.strip()][-1] if output.strip() else ""
                suffix = "(no tests)" if code == 5 else f"({duration:.1f}s) {last_line}"
                print(f"  {status} [{done}/{total}] {fpath.name} {suffix}")
            else:
                failed += 1
                print(f"  FAIL [{done}/{total}] {fpath.name} ({duration:.1f}s)")
                for line in output.strip().split("\n")[-5:]:
                    if line.strip():
                        print(f"       {line}")
                if fail_fast:
                    abort = True
                    for f in futures:
                        f.cancel()

    wall_time = time.monotonic() - wall_start
    print(f"\n{'=' * 60}")
    print(f"Files: {passed} passed, {failed} failed, {total} total")
    print(f"Wall:  {wall_time:.1f}s ({workers} workers)")
    return 1 if failed else 0


def discover_affected_files(base: str) -> list[Path]:
    """Use test_impact.py to find test files affected by changed sources."""
    impact_script = Path(__file__).parent / "test_impact.py"
    if not impact_script.exists():
        print(f"test_impact.py not found at {impact_script}")
        sys.exit(1)

    result = subprocess.run(
        [sys.executable, str(impact_script), "--print-paths", "--base", base],
        capture_output=True,
        text=True,
    )
    paths = [Path(p.strip()) for p in result.stdout.splitlines() if p.strip()]
    return sorted(paths)


def main() -> None:
    default_workers = _default_workers()
    parser = argparse.ArgumentParser(description="Run tests in isolated subprocesses")
    parser.add_argument("-x", "--fail-fast", action="store_true", help="Stop on first failure")
    parser.add_argument("-k", "--keyword", help="Filter test files by keyword")
    parser.add_argument(
        "--parallel",
        type=int,
        default=default_workers,
        help=f"Number of parallel workers (1=sequential, default={default_workers})",
    )
    parser.add_argument("--test-dir", default="tests/unit", help="Test directory")
    parser.add_argument(
        "--affected",
        nargs="?",
        const="HEAD",
        metavar="BASE",
        help="Run only tests affected by changes since BASE (default: HEAD = staged+unstaged)",
    )
    parser.add_argument("extra", nargs="*", help="Extra args passed to pytest")
    args = parser.parse_args()

    workers: int = max(1, args.parallel)

    if args.affected is not None:
        files = discover_affected_files(args.affected)
        if args.keyword:
            files = [f for f in files if args.keyword in f.stem]
        if not files:
            print("No affected tests found — nothing to run")
            sys.exit(0)
        print(f"Running {len(files)} affected test files (each in its own process)")
        print(f"{'=' * 60}")
        if workers > 1:
            code = run_parallel(files, args.extra, workers, args.fail_fast)
        else:
            code = run_sequential(files, args.extra, args.fail_fast)
        sys.exit(code)

    test_dir = Path(args.test_dir)
    if not test_dir.exists():
        print(f"Test directory not found: {test_dir}")
        sys.exit(1)

    files = discover_test_files(test_dir, args.keyword)
    if not files:
        print("No test files found")
        sys.exit(0)

    mode = f"parallel ({workers} workers)" if workers > 1 else "sequential"
    print(f"Running {len(files)} test files {mode} (each in its own process)")
    print(f"{'=' * 60}")

    if workers > 1:
        code = run_parallel(files, args.extra, workers, args.fail_fast)
    else:
        code = run_sequential(files, args.extra, args.fail_fast)

    sys.exit(code)


if __name__ == "__main__":
    main()
