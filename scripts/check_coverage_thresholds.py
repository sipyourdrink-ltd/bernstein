#!/usr/bin/env python3
"""TEST-012: Check per-module test coverage meets thresholds.

Runs pytest with coverage on each specified module and asserts
each meets the configured minimum (default 80%).

Usage:
    uv run python scripts/check_coverage_thresholds.py
    uv run python scripts/check_coverage_thresholds.py --threshold 70
    uv run python scripts/check_coverage_thresholds.py --module bernstein.core.lifecycle
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

# Modules to check and their minimum coverage thresholds.
# Override the global default per-module if needed.
DEFAULT_THRESHOLD: int = 80

MODULE_THRESHOLDS: dict[str, int] = {
    "bernstein.core.lifecycle": 80,
    "bernstein.core.models": 60,
    "bernstein.core.config_schema": 70,
    "bernstein.adapters.base": 60,
    "bernstein.core.task_store": 60,
}


@dataclass
class CoverageResult:
    """Coverage measurement for a single module."""

    module: str
    covered: int
    total: int
    percent: float
    threshold: int
    passed: bool


def measure_coverage(module: str, threshold: int) -> CoverageResult:
    """Run pytest with coverage for a single module and parse results.

    Args:
        module: Dotted module name (e.g. "bernstein.core.lifecycle").
        threshold: Minimum coverage percentage.

    Returns:
        CoverageResult with measured coverage.
    """
    module_path = module.replace(".", "/") + ".py"
    src_path = Path("src") / module_path

    if not src_path.exists():
        return CoverageResult(
            module=module,
            covered=0,
            total=0,
            percent=0.0,
            threshold=threshold,
            passed=False,
        )

    # Find matching test files heuristically
    module_leaf = module.split(".")[-1]
    test_patterns = [
        f"tests/unit/test_{module_leaf}.py",
        f"tests/unit/test_{module_leaf}_*.py",
    ]

    test_files: list[str] = []
    for pat in test_patterns:
        found = list(Path(".").glob(pat))
        test_files.extend(str(f) for f in found)

    if not test_files:
        return CoverageResult(
            module=module,
            covered=0,
            total=0,
            percent=0.0,
            threshold=threshold,
            passed=False,
        )

    cmd = [
        sys.executable,
        "-m",
        "pytest",
        *test_files[:5],  # Limit to avoid running too many files
        f"--cov=src/{module_path.replace('.py', '').replace('/', '.')}",
        "--cov-report=json",
        "--cov-report=term",
        "-x",
        "-q",
        "--no-header",
        "--override-ini=addopts=",
    ]

    subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=120,
    )

    # Parse coverage JSON if available
    cov_json = Path("coverage.json")
    if cov_json.exists():
        try:
            data = json.loads(cov_json.read_text())
            totals = data.get("totals", {})
            percent = totals.get("percent_covered", 0.0)
            covered = totals.get("covered_lines", 0)
            total = totals.get("num_statements", 0)
            cov_json.unlink(missing_ok=True)
            return CoverageResult(
                module=module,
                covered=covered,
                total=total,
                percent=percent,
                threshold=threshold,
                passed=percent >= threshold,
            )
        except (json.JSONDecodeError, KeyError):
            pass
        finally:
            cov_json.unlink(missing_ok=True)

    return CoverageResult(
        module=module,
        covered=0,
        total=0,
        percent=0.0,
        threshold=threshold,
        passed=False,
    )


def main() -> int:
    """Run coverage checks and report results."""
    parser = argparse.ArgumentParser(description="Check per-module test coverage thresholds")
    parser.add_argument("--threshold", type=int, default=DEFAULT_THRESHOLD, help="Global minimum coverage %%")
    parser.add_argument("--module", type=str, default=None, help="Check a single module")
    parser.add_argument("--json", dest="as_json", action="store_true", help="Output as JSON")
    args = parser.parse_args()

    if args.module:
        modules = {args.module: args.threshold}
    else:
        modules = {m: MODULE_THRESHOLDS.get(m, args.threshold) for m in MODULE_THRESHOLDS}

    results: list[CoverageResult] = []
    for mod, thresh in modules.items():
        result = measure_coverage(mod, thresh)
        results.append(result)

    if args.as_json:
        print(
            json.dumps(
                [
                    {
                        "module": r.module,
                        "covered": r.covered,
                        "total": r.total,
                        "percent": round(r.percent, 1),
                        "threshold": r.threshold,
                        "passed": r.passed,
                    }
                    for r in results
                ],
                indent=2,
            )
        )
    else:
        print("\n--- Coverage Threshold Report ---\n")
        for r in results:
            status = "PASS" if r.passed else "FAIL"
            if not r.passed:
                pass
            print(f"  [{status}] {r.module}: {r.percent:.1f}% (threshold: {r.threshold}%)")
        print()

    failed = [r for r in results if not r.passed]
    if failed:
        print(f"{len(failed)} module(s) below coverage threshold.")
        return 1
    print("All modules meet coverage thresholds.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
