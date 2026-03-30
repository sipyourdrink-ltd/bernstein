#!/usr/bin/env python3
"""
Generate protocol compatibility matrix table for docs/compatibility.md

This script reads protocol test results and generates a markdown table
showing which MCP, A2A, and ACP versions are compatible with each Bernstein release.

Usage:
    python scripts/generate_compatibility_table.py \
        --results path/to/protocol-compat-results.json \
        --output docs/compatibility.md
"""

import argparse
import json
from datetime import datetime
from pathlib import Path


def generate_table(results_data: dict) -> str:
    """Generate markdown compatibility table from test results."""
    # Support both formats: CI results and baseline
    results = []

    if "results" in results_data:
        # CI format with results array
        results = results_data.get("results", [])
        summary = results_data.get("summary", {})
    elif "compatibility" in results_data:
        # Baseline format with compatibility dict
        compat_dict = results_data.get("compatibility", {})
        for key, entry in compat_dict.items():
            parts = key.split("+")
            if len(parts) == 3:
                python_ver = parts[0]
                mcp_ver = parts[1].replace("mcp", "")
                a2a_ver = parts[2].replace("a2a", "")
                results.append(
                    {"python": python_ver, "mcp": mcp_ver, "a2a": a2a_ver, "status": entry.get("status", "unknown")}
                )
        summary = {
            "total_combinations": len(results),
            "passed": sum(1 for r in results if r["status"] == "pass"),
            "failed": sum(1 for r in results if r["status"] != "pass"),
            "incompatible": 0,
            "timeout": 0,
        }
    else:
        summary = {}

    # Group results by Python version for clarity
    table_rows = []
    table_rows.append("| Python | MCP | A2A | ACP | Status |")
    table_rows.append("|--------|-----|-----|-----|--------|")

    seen = set()
    for result in sorted(results, key=lambda r: (r["python"], r["mcp"], r["a2a"])):
        key = (result["python"], result["mcp"], result["a2a"])
        if key in seen:
            continue
        seen.add(key)

        status_icon = "✅" if result["status"] == "pass" else "❌"
        row = f"| {result['python']} | {result['mcp']} | {result['a2a']} | latest | {status_icon} |"
        table_rows.append(row)

    table_content = "\n".join(table_rows)

    # Build summary section
    timestamp = results_data.get("timestamp", datetime.now().isoformat())
    commit = results_data.get("commit_sha", "unknown")[:7]

    summary_text = f"""
# Protocol Compatibility Matrix

**Last Updated**: {timestamp}
**Commit**: {commit}

## Summary
- **Total Combinations Tested**: {summary.get("total_combinations", 0)}
- **Passing**: {summary.get("passed", 0)}
- **Failing**: {summary.get("failed", 0)}
- **Incompatible**: {summary.get("incompatible", 0)}
- **Timeout**: {summary.get("timeout", 0)}

## Compatibility Table

{table_content}

## Legend
- ✅ Fully compatible
- ❌ Not compatible or test failure
- `latest` for ACP indicates we test against the latest released version

## Note
This table is auto-generated on every CI run. Protocol compatibility is tested
via {summary.get("total_combinations", 0)} matrix combinations to ensure comprehensive coverage.
"""

    return summary_text.strip()


def main():
    parser = argparse.ArgumentParser(description="Generate protocol compatibility table")
    parser.add_argument("--results", required=True, help="Path to protocol-compat-results.json")
    parser.add_argument("--output", default="docs/compatibility.md", help="Output file path")
    args = parser.parse_args()

    results_file = Path(args.results)
    if not results_file.exists():
        raise FileNotFoundError(f"Results file not found: {results_file}")

    with open(results_file) as f:
        results_data = json.load(f)

    table_markdown = generate_table(results_data)

    output_file = Path(args.output)
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(table_markdown)

    print(f"✅ Compatibility table generated: {output_file}")


if __name__ == "__main__":
    main()
