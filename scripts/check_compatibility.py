#!/usr/bin/env python3
"""
Check protocol compatibility against baseline.

This script compares current test results against the compatibility baseline
to detect regressions and breaking changes.

Usage:
    python scripts/check_compatibility.py \
        --current-results path/to/current-results.json \
        --baseline tests/protocol/compatibility-baseline.json
"""

import argparse
import json
from pathlib import Path


def _combination_key(result: dict) -> str:
    return f"{result['python']}+mcp{result['mcp']}+a2a{result['a2a']}"


def check_compatibility(current_results: dict, baseline: dict) -> dict:
    """Compare current results against baseline to detect regressions."""
    baseline_compat = baseline.get("compatibility")
    if baseline_compat is None:
        baseline_compat = {_combination_key(row): row for row in baseline.get("results", [])}
    breaking_changes = []
    new_compatibilities = []

    for result in current_results.get("results", []):
        key = _combination_key(result)
        baseline_entry = baseline_compat.get(key)

        if baseline_entry is None:
            # New combination not in baseline
            if result["status"] == "pass":
                new_compatibilities.append(key)
        else:
            # Existing baseline - check for regression
            if baseline_entry.get("status") == "pass" and result["status"] != "pass":
                breaking_changes.append(f"{key}: regression from {baseline_entry['status']} to {result['status']}")

    status = "breaking_change" if breaking_changes else "compatible"

    return {
        "ok": True,
        "status": status,
        "breaking_changes": breaking_changes,
        "new_compatibilities": new_compatibilities,
    }


def main():
    parser = argparse.ArgumentParser(description="Check protocol compatibility")
    parser.add_argument("--current-results", required=True, help="Path to current test results")
    parser.add_argument("--baseline", required=True, help="Path to baseline compatibility data")
    args = parser.parse_args()

    current_file = Path(args.current_results)
    baseline_file = Path(args.baseline)

    if not current_file.exists():
        raise FileNotFoundError(f"Current results not found: {current_file}")

    with open(current_file) as f:
        current_data = json.load(f)

    if baseline_file.exists():
        with open(baseline_file) as f:
            baseline_data = json.load(f)
    else:
        # First run - use current as baseline
        print(f"⚠️  Baseline not found. Using current results as baseline: {baseline_file}")
        baseline_file.parent.mkdir(parents=True, exist_ok=True)
        baseline_file.write_text(json.dumps(current_data, indent=2))
        return {"ok": True, "status": "compatible", "breaking_changes": [], "new_compatibilities": []}

    result = check_compatibility(current_data, baseline_data)

    if result["breaking_changes"]:
        print("❌ Breaking changes detected:")
        for change in result["breaking_changes"]:
            print(f"   - {change}")
    else:
        print("✅ No breaking changes detected")

    if result["new_compatibilities"]:
        print("✨ New compatible combinations:")
        for compat in result["new_compatibilities"]:
            print(f"   - {compat}")

    print(json.dumps(result, indent=2))
    return result


if __name__ == "__main__":
    main()
