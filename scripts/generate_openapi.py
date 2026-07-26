#!/usr/bin/env python3
"""Regenerate the committed OpenAPI snapshot from the Bernstein FastAPI app.

Run this after changing any API route, model, or schema. The snapshot is a
build input, not a report: it renders the published REST reference and seeds
``scripts/generate_sdk.py``, so a stale file ships a stale client.

``tests/unit/test_openapi_snapshot_drift.py`` fails when the snapshot and the
app disagree, which is the reminder to run this.

Usage:
    uv run python scripts/generate_openapi.py

Output:
    docs/reference/openapi.json  - full OpenAPI 3.1 spec; committed to the
                                   repo and served by Redoc.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Ensure the project root is on the import path when running from any directory.
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))

# The single source of truth for where the snapshot lives. The drift guard
# reads this constant, so the two cannot point at different files.
SPEC_PATH = ROOT / "docs" / "reference" / "openapi.json"


def main() -> None:
    try:
        from bernstein.core.server import create_app
    except ImportError as exc:
        sys.exit(f"Cannot import bernstein: {exc}\nRun this script with: uv run python scripts/generate_openapi.py")

    app = create_app()
    spec = app.openapi()

    SPEC_PATH.write_text(json.dumps(spec, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    paths = len(spec.get("paths", {}))
    schemas = len(spec.get("components", {}).get("schemas", {}))
    print(f"Written {SPEC_PATH.relative_to(ROOT)}  ({paths} paths, {schemas} schemas)")


if __name__ == "__main__":
    main()
