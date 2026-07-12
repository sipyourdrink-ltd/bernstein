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
    python scripts/run_tests.py --coverage   # collect coverage and emit coverage.json
    python scripts/run_tests.py --shard 1/4  # run only shard 1 of 4 (CI fan-out)
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import cast

_TEST_REQUIRED_PREFIXES = (
    ".github/workflows/",
    "scripts/",
    "src/",
    "tests/",
)

DEFAULT_TEST_FILE_TIMEOUT_SECONDS = 300
TEST_FILE_TIMEOUT_ENV = "BERNSTEIN_TEST_FILE_TIMEOUT_SECONDS"

# A heavily-parallel shard can transiently exhaust the OS thread table, which
# surfaces as this CPython error rather than a genuine test failure. A single
# serial retry distinguishes the environmental flake from a real regression.
_THREAD_EXHAUSTION_MARKER = "RuntimeError: can't start new thread"


def test_file_timeout_seconds() -> int:
    """Return the per-file subprocess timeout in seconds."""
    raw = os.environ.get(TEST_FILE_TIMEOUT_ENV)
    if raw is None or raw == "":
        return DEFAULT_TEST_FILE_TIMEOUT_SECONDS
    try:
        timeout = int(raw)
    except ValueError as exc:
        raise ValueError(f"{TEST_FILE_TIMEOUT_ENV} must be an integer number of seconds") from exc
    if timeout < 1:
        raise ValueError(f"{TEST_FILE_TIMEOUT_ENV} must be at least 1 second")
    return timeout


def _default_workers() -> int:
    """Pick a sensible default worker count: min(cpu_count, 8), at least 1."""
    cpus = os.cpu_count() or 1
    return min(cpus, 8)


def discover_test_files(test_dir: Path, keyword: str | None = None) -> list[Path]:
    """Find all test_*.py files recursively, optionally filtered by keyword."""
    files = sorted(test_dir.rglob("test_*.py"))
    if keyword:
        files = [f for f in files if keyword in f.stem]
    return files


def parse_shard_spec(spec: str) -> tuple[int, int]:
    """Parse a ``i/N`` shard spec into ``(shard_index, shard_count)``.

    ``shard_index`` is 1-based and must satisfy ``1 <= i <= N``; ``N`` must be
    a positive integer. Raises ``ValueError`` on any malformed or out-of-range
    input so the CLI fails loudly rather than silently running the wrong slice.
    """
    parts = spec.split("/")
    if len(parts) != 2:
        raise ValueError(f"shard spec must be 'i/N' (got {spec!r})")
    try:
        shard_index = int(parts[0])
        shard_count = int(parts[1])
    except ValueError as exc:
        raise ValueError(f"shard spec parts must be integers (got {spec!r})") from exc
    if shard_count < 1:
        raise ValueError(f"shard count must be >= 1 (got {shard_count})")
    if not 1 <= shard_index <= shard_count:
        raise ValueError(f"shard index {shard_index} out of range 1..{shard_count}")
    return shard_index, shard_count


def shard_files(files: list[Path], shard_index: int, shard_count: int) -> list[Path]:
    """Return the deterministic 1-based ``shard_index`` of ``shard_count`` shards.

    Partition by position modulo ``shard_count`` over the (already sorted)
    ``files`` list: shard ``i`` owns every file whose index ``j`` satisfies
    ``j % shard_count == i - 1``. This is:

    - **deterministic + stable** - no hashing, no salt; the same inputs always
      yield the same slice across runs and machines (the repo's determinism
      contract);
    - **complete + disjoint** - every file lands in exactly one shard;
    - **balanced** - shard sizes differ by at most one;
    - **order-preserving** - each shard is a subsequence of ``files``.
    """
    if shard_count < 1:
        raise ValueError(f"shard count must be >= 1 (got {shard_count})")
    if not 1 <= shard_index <= shard_count:
        raise ValueError(f"shard index {shard_index} out of range 1..{shard_count}")
    return [f for j, f in enumerate(files) if j % shard_count == shard_index - 1]


def run_file(path: Path, extra_args: list[str], coverage: bool = False) -> tuple[Path, int, float, str]:
    """Run a single test file in a subprocess. Returns (path, exitcode, duration, output).

    When ``coverage`` is True, the process is wrapped in ``coverage run`` with a
    parallel-safe data file so that many subprocesses can be combined later.
    """
    if coverage:
        cmd = [
            sys.executable,
            "-m",
            "coverage",
            "run",
            "--parallel-mode",
            "-m",
            "pytest",
            str(path),
            "-x",
            "-q",
            "--tb=short",
            "-p",
            "no:cacheprovider",
            "-s",
            *extra_args,
        ]
    else:
        cmd = [
            sys.executable,
            "-m",
            "pytest",
            str(path),
            "-x",
            "-q",
            "--tb=short",
            "-p",
            "no:cacheprovider",
            "-s",
            *extra_args,
        ]
    start = time.monotonic()
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=test_file_timeout_seconds())
    duration = time.monotonic() - start
    output = result.stdout + result.stderr
    return path, result.returncode, duration, output


def retry_on_thread_exhaustion(
    path: Path,
    extra_args: list[str],
    code: int,
    output: str,
    coverage: bool = False,
) -> tuple[int, float, str] | None:
    """Re-run *path* once serially when it failed from OS thread exhaustion.

    Returns the retry ``(code, duration, output)`` when the original failure
    carried the thread-exhaustion marker, otherwise ``None`` (no retry). The
    retry runs the same isolated subprocess as ``run_file``; because the caller
    invokes it serially, the transient thread pressure has cleared by then.
    """
    if code == 0 or _THREAD_EXHAUSTION_MARKER not in output:
        return None
    _path, retry_code, retry_duration, retry_output = run_file(path, extra_args, coverage=coverage)
    return retry_code, retry_duration, retry_output


def _print_failure_summary(output: str) -> None:
    """Print the pytest failure summary from subprocess output.

    Extracts the 'FAILURES' section and 'short test summary' rather than
    dumping everything (which can be 1000+ lines with -s / no-capture).
    """
    lines = output.strip().split("\n")
    extracted = _extract_failure_sections(lines)
    if not extracted:
        for line in lines[-30:]:
            if line.strip():
                print(f"       {line}")
        return
    for line in extracted:
        print(f"       {line}")


def _extract_failure_sections(lines: list[str]) -> list[str]:
    """Extract FAILURES and short test summary sections from output lines."""
    result: list[str] = []
    in_section = False
    for line in lines:
        stripped = line.strip()
        if ("FAILURES" in stripped and "===" in stripped) or "short test summary" in stripped:
            in_section = True
        if in_section:
            result.append(line)
            if len(result) > 80:
                result.append("... (truncated)")
                break
    return result


def _report_file_result(label: str, code: int, duration: float, output: str) -> bool:
    """Report a single file result. Returns True if passed/skipped."""
    if code == 0:
        last_line = [ln for ln in output.strip().split("\n") if ln.strip()][-1] if output.strip() else ""
        print(f"  PASS {label} ({duration:.1f}s) {last_line}")
        return True
    if code == 5:
        print(f"  SKIP {label} (no tests)")
        return True
    print(f"  FAIL {label} ({duration:.1f}s)")
    _print_failure_summary(output)
    return False


def run_sequential(files: list[Path], extra_args: list[str], fail_fast: bool, coverage: bool = False) -> int:
    """Run test files one by one."""
    passed = 0
    failed = 0
    total_duration = 0.0

    for i, path in enumerate(files, 1):
        label = f"[{i}/{len(files)}] {path.name}"
        try:
            _fpath, code, duration, output = run_file(path, extra_args, coverage=coverage)
        except subprocess.TimeoutExpired as exc:
            print(f"  TIMEOUT {label} (>{exc.timeout:g}s)")
            failed += 1
            if fail_fast:
                break
            continue

        retry = retry_on_thread_exhaustion(path, extra_args, code, output, coverage=coverage)
        if retry is not None:
            print(f"  RETRIED (thread exhaustion) {label}")
            code, duration, output = retry

        total_duration += duration
        if _report_file_result(label, code, duration, output):
            passed += 1
        else:
            failed += 1
            if fail_fast:
                break

    print(f"\n{'=' * 60}")
    print(f"Files: {passed} passed, {failed} failed, {len(files)} total")
    print(f"Time:  {total_duration:.1f}s")
    return 1 if failed else 0


def run_parallel(
    files: list[Path], extra_args: list[str], workers: int, fail_fast: bool, coverage: bool = False
) -> int:
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
        futures = {pool.submit(run_file, f, extra_args, coverage): f for f in files}
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

            retry = retry_on_thread_exhaustion(fpath, extra_args, code, output, coverage=coverage)
            if retry is not None:
                print(f"  RETRIED (thread exhaustion) {fpath.name}")
                code, duration, output = retry

            done += 1
            label = f"[{done}/{total}] {fpath.name}"
            if _report_file_result(label, code, duration, output):
                passed += 1
            else:
                failed += 1
                if fail_fast:
                    abort = True
                    for f in futures:
                        f.cancel()

    wall_time = time.monotonic() - wall_start
    print(f"\n{'=' * 60}")
    print(f"Files: {passed} passed, {failed} failed, {total} total")
    print(f"Wall:  {wall_time:.1f}s ({workers} workers)")
    return 1 if failed else 0


def _report_empty_selection(shard: tuple[int, int] | None, context: str) -> None:
    """Print a clear message when the selected file set is empty.

    An empty shard (N greater than the file count, or a small affected set
    split across many shards) is a legitimate no-op that must exit 0 - not a
    discovery failure. The message disambiguates the two for CI log readers.
    """
    if shard is not None:
        print(f"No {context}test files in shard {shard[0]}/{shard[1]} - nothing to run (empty shard)")
    else:
        suffix = "affected tests found" if context else "test files found"
        print(f"No {suffix} - nothing to run")


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
    if result.returncode != 0:
        print(result.stderr.strip() or result.stdout.strip() or "test_impact.py failed")
        sys.exit(result.returncode)
    paths = [Path(p.strip()) for p in result.stdout.splitlines() if p.strip()]
    return sorted(paths)


def discover_changed_files(base: str) -> list[str]:
    """Return repo-relative changed paths for empty affected-set decisions."""
    root = Path(__file__).parent.parent
    try:
        if base == "HEAD":
            unstaged = subprocess.run(
                ["git", "diff", "--name-only", "HEAD"],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.splitlines()
            staged = subprocess.run(
                ["git", "diff", "--name-only", "--cached"],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.splitlines()
            untracked = subprocess.run(
                ["git", "ls-files", "--others", "--exclude-standard"],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.splitlines()
            return sorted({path for path in [*unstaged, *staged, *untracked] if path})
        try:
            return subprocess.run(
                ["git", "diff", "--name-only", f"{base}...HEAD"],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.splitlines()
        except subprocess.CalledProcessError as exc:
            if exc.returncode != 128:
                raise
            return subprocess.run(
                ["git", "diff", "--name-only", f"{base}..HEAD"],
                cwd=root,
                capture_output=True,
                text=True,
                check=True,
            ).stdout.splitlines()
    except subprocess.CalledProcessError as exc:
        print(exc.stderr.strip() or f"Unable to inspect changed files against {base}")
        sys.exit(exc.returncode)


def changed_files_require_tests(changed_files: list[str]) -> bool:
    """Return True when an empty affected set must fail closed."""
    return any(Path(path).as_posix().startswith(_TEST_REQUIRED_PREFIXES) for path in changed_files)


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
    parser.add_argument(
        "--coverage",
        action="store_true",
        help="Collect coverage per subprocess and emit coverage.json at the repo root",
    )
    parser.add_argument(
        "--shard",
        metavar="i/N",
        help=(
            "Run only shard i of N (1-based, e.g. '1/4'). The discovered file "
            "list is partitioned deterministically so reruns are reproducible "
            "and the union of all N shards covers every file exactly once."
        ),
    )
    parser.add_argument("extra", nargs="*", help="Extra args passed to pytest")
    args = parser.parse_args()

    workers: int = max(1, args.parallel)

    shard: tuple[int, int] | None = None
    if args.shard is not None:
        try:
            shard = parse_shard_spec(args.shard)
        except ValueError as exc:
            print(f"Invalid --shard {args.shard!r}: {exc}")
            sys.exit(2)

    if args.affected is not None:
        affected_files = discover_affected_files(args.affected)
        files = affected_files
        if args.keyword:
            files = [f for f in files if args.keyword in f.stem]
        if shard is not None:
            files = shard_files(files, *shard)
        if not files:
            if not affected_files:
                changed_files = discover_changed_files(args.affected)
                if changed_files_require_tests(changed_files):
                    print("No affected tests found for code or workflow changes; failing closed.")
                    for changed_file in changed_files:
                        print(f"  {changed_file}")
                    sys.exit(1)
            _report_empty_selection(shard, context="affected ")
            sys.exit(0)
        shard_label = f" [shard {shard[0]}/{shard[1]}]" if shard else ""
        print(f"Running {len(files)} affected test files{shard_label} (each in its own process)")
        print(f"{'=' * 60}")
        if workers > 1:
            code = run_parallel(files, args.extra, workers, args.fail_fast, args.coverage)
        else:
            code = run_sequential(files, args.extra, args.fail_fast, args.coverage)
        if args.coverage:
            _finalize_coverage()
        sys.exit(code)

    test_dir = Path(args.test_dir)
    if not test_dir.exists():
        print(f"Test directory not found: {test_dir}")
        sys.exit(1)

    files = discover_test_files(test_dir, args.keyword)
    if shard is not None:
        files = shard_files(files, *shard)
    if not files:
        _report_empty_selection(shard, context="")
        sys.exit(0)

    mode = f"parallel ({workers} workers)" if workers > 1 else "sequential"
    shard_label = f" [shard {shard[0]}/{shard[1]}]" if shard else ""
    print(f"Running {len(files)} test files{shard_label} {mode} (each in its own process)")
    print(f"{'=' * 60}")

    if workers > 1:
        code = run_parallel(files, args.extra, workers, args.fail_fast, args.coverage)
    else:
        code = run_sequential(files, args.extra, args.fail_fast, args.coverage)

    if args.coverage:
        _finalize_coverage()

    sys.exit(code)


def _finalize_coverage() -> None:
    """Combine per-subprocess coverage data and emit coverage.json."""
    try:
        subprocess.run(
            [sys.executable, "-m", "coverage", "combine"],
            check=False,
            capture_output=True,
        )
        subprocess.run(
            [sys.executable, "-m", "coverage", "json", "-o", "coverage.json"],
            check=False,
            capture_output=True,
        )
        if Path("coverage.json").exists():
            try:
                data: object = json.loads(Path("coverage.json").read_text(encoding="utf-8"))
                if not isinstance(data, dict):
                    return
                root = cast("dict[str, object]", data)
                totals_raw = root.get("totals")
                if not isinstance(totals_raw, dict):
                    return
                totals = cast("dict[str, object]", totals_raw)
                pct = totals.get("percent_covered")
                if isinstance(pct, int | float | str):
                    print(f"\nCoverage: {float(pct):.2f}%")
            except (json.JSONDecodeError, OSError, ValueError):
                pass
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"  WARNING: coverage finalization failed: {exc}")


if __name__ == "__main__":
    main()
