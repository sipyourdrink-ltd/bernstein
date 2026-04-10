#!/usr/bin/env python3
"""Generate Python and TypeScript client SDKs from the Bernstein OpenAPI spec.

Reads docs/openapi.json (produced by scripts/generate_openapi.py) and writes:
  - docs/sdk/client.py       — Python SDK
  - docs/sdk/client.ts       — TypeScript SDK

Usage:
    uv run python scripts/generate_sdk.py [--spec PATH]

Options:
    --spec PATH   Path to openapi.json (default: docs/openapi.json)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument(
        "--spec",
        default=str(ROOT / "docs" / "openapi.json"),
        help="Path to openapi.json (default: docs/openapi.json)",
    )
    args = parser.parse_args()

    spec_path = Path(args.spec)
    if not spec_path.exists():
        sys.exit(
            f"OpenAPI spec not found at {spec_path}\n"
            "Run: uv run python scripts/generate_openapi.py"
        )

    try:
        from bernstein.core.sdk_generator import generate_sdk_to_file, generate_typescript_sdk_to_file
    except ImportError as exc:
        sys.exit(f"Cannot import bernstein: {exc}\nRun: uv run python scripts/generate_sdk.py")

    spec = json.loads(spec_path.read_text(encoding="utf-8"))
    out_dir = ROOT / "docs" / "sdk"

    py_path = generate_sdk_to_file(str(out_dir / "client.py"), spec)
    ts_path = generate_typescript_sdk_to_file(str(out_dir / "client.ts"), spec)

    print(f"Python SDK:     {Path(py_path).relative_to(ROOT)}")
    print(f"TypeScript SDK: {Path(ts_path).relative_to(ROOT)}")


if __name__ == "__main__":
    main()
