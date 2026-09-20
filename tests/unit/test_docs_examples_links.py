"""Test that docs links to examples/ follow project conventions.

Regression test for #6090 - links to examples/ must use absolute GitHub URLs,
not relative paths to directories.
"""

from __future__ import annotations

import re
from pathlib import Path


def test_examples_links_use_github_urls() -> None:
    """All docs links to examples/ must use GitHub URLs, not relative paths."""
    docs_dir = Path("docs")
    violations = []

    for md_file in docs_dir.rglob("*.md"):
        content = md_file.read_text()
        # Find markdown links with relative paths to examples/
        relative_examples = re.findall(r"\[([^\]]+)\]\((\.\.\/.*examples\/[^)]*)\)", content)

        for link_text, link_path in relative_examples:
            line_num = content[: content.find(link_path)].count("\n") + 1
            violations.append(f"{md_file}:{line_num} - [{link_text}]({link_path})")

    assert not violations, "Found relative links to examples/. Use GitHub URLs instead:\n" + "\n".join(violations)


def test_self_hosted_endpoints_link_fixed() -> None:
    """Regression test for #6090 - link must not point to directory."""
    doc_path = Path("docs/operations/self-hosted-endpoints.md")
    content = doc_path.read_text()

    # Should not have the old broken relative path to directory
    assert "](../../examples/local-fleet/)" not in content, (
        "Found broken relative link to examples/local-fleet/ directory"
    )

    # Should have GitHub URL
    assert "github.com/sipyourdrink-ltd/bernstein/tree/main/examples/local-fleet" in content, (
        "Expected GitHub URL for examples/local-fleet link"
    )
