#!/usr/bin/env python3
"""Generate docs/openapi.json from the Bernstein FastAPI server definition.

Run this script after changing any API route, model, or schema to keep the
hosted reference at docs/api-reference.html in sync.

Usage:
    uv run python scripts/generate_openapi.py

Output:
    docs/openapi.json  — full OpenAPI 3.1 spec; committed to repo and served
                         by Redoc at docs/api-reference.html.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Ensure the project root is on the import path when running from any directory.
ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))


def main() -> None:
    try:
        from bernstein.core.server import create_app
    except ImportError as exc:
        sys.exit(f"Cannot import bernstein: {exc}\nRun this script with: uv run python scripts/generate_openapi.py")

    app = create_app()
    spec = app.openapi()

    out = ROOT / "docs" / "openapi.json"
    out.write_text(json.dumps(spec, indent=2, ensure_ascii=False), encoding="utf-8")

    paths = len(spec.get("paths", {}))
    schemas = len(spec.get("components", {}).get("schemas", {}))
    print(f"Written {out.relative_to(ROOT)}  ({paths} paths, {schemas} schemas)")


if __name__ == "__main__":
    main()
