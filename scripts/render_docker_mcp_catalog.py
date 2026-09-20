#!/usr/bin/env python3
"""Render packaging/docker-mcp/server.yaml with release commit substituted.

Reads the template server.yaml and replaces source.commit with the given
40-hex commit hash. Used by .github/workflows/publish.yml to generate the
catalog payload at release time so the listing carries the actual release
commit.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path


def render_catalog_yaml(template_text: str, release_commit: str) -> str:
    """Replace source.commit in template with release_commit.
    
    Args:
        template_text: Original server.yaml content
        release_commit: 40-character hex SHA to substitute
        
    Returns:
        Rendered YAML with substituted commit
        
    Raises:
        ValueError: If release_commit is not 40 lowercase hex chars or if
            template has no source.commit field
    """
    if not re.fullmatch(r"[0-9a-f]{40}", release_commit):
        raise ValueError(
            f"release_commit must be 40 lowercase hex characters, got: {release_commit!r}"
        )
    
    # Match the exact format: "  commit: <40hex>"
    pattern = r"^(  commit:\s*)[0-9a-f]{40}(\s*)$"
    match = re.search(pattern, template_text, re.MULTILINE)
    
    if not match:
        raise ValueError(
            "template has no valid source.commit field (expected '  commit: <40hex>')"
        )
    
    # Replace preserving whitespace
    rendered = re.sub(
        pattern,
        rf"\g<1>{release_commit}\g<2>",
        template_text,
        count=1,
        flags=re.MULTILINE,
    )
    
    return rendered


def main() -> int:
    """CLI entry point."""
    if len(sys.argv) != 3:
        print(
            "Usage: render_docker_mcp_catalog.py <template-path> <release-commit>",
            file=sys.stderr,
        )
        return 2
    
    template_path = Path(sys.argv[1])
    release_commit = sys.argv[2]
    
    try:
        template_text = template_path.read_text(encoding="utf-8")
        rendered = render_catalog_yaml(template_text, release_commit)
        print(rendered, end="")
        return 0
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1
    except OSError as e:
        print(f"error reading template: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
