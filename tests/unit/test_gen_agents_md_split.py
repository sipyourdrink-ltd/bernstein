"""Regression tests for ``scripts/gen_agents_md.py`` section splitting.

The legacy module-map generator rewrites only the ``## Module map`` section of
``AGENTS.md`` in place. It previously ended that section at a ``\\n---\\n``
separator only; the canonical ``bernstein agents-md`` output carries no such
rules, so ``_split_agents_md`` treated the module map as running to EOF and an
``--update`` overwrote every following section. These tests pin the fixed
behaviour: the section is bounded at the next top-level ``## `` heading (or a
``---`` separator, whichever comes first), and the partition is lossless.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_PATH = REPO_ROOT / "scripts" / "gen_agents_md.py"


@pytest.fixture
def gen_module():
    """Load scripts/gen_agents_md.py as a module without executing main()."""
    spec = importlib.util.spec_from_file_location("gen_agents_md_under_test", SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    yield module
    sys.modules.pop(spec.name, None)


_CANONICAL = (
    "# demo - AGENTS.md\n\n"
    "## Overview\n\nsome overview\n\n"
    "## Module map\n\n"
    "### `src/demo/` - core\n\n| File | Purpose |\n|------|---------|\n| `a.py` | thing |\n\n"
    "## Build & test\n\n```\nuv sync\n```\n\n"
    "## Setup\n\ndo the setup\n"
)


def test_split_stops_at_next_heading_and_preserves_trailing_sections(gen_module) -> None:
    parts = gen_module._split_agents_md(_CANONICAL)
    assert parts is not None
    before, body, after = parts
    # Module-map body is bounded at the next top-level heading...
    assert body.lstrip().startswith("## Module map")
    assert "## Build & test" not in body
    assert "## Setup" not in body
    # ...and every following section survives in ``after``.
    assert "## Build & test" in after
    assert "## Setup" in after
    # Sub-headings (###) inside the module map are not treated as boundaries.
    assert "### `src/demo/`" in body
    # The partition is lossless.
    assert before + body + after == _CANONICAL


def test_split_still_honours_dash_separator(gen_module) -> None:
    text = "## Module map\n\nmap body\n\n---\n\n## Later\n\nx\n"
    parts = gen_module._split_agents_md(text)
    assert parts is not None
    _before, body, after = parts
    assert body.rstrip().endswith("map body")
    assert after.startswith("\n---\n")


def test_split_returns_none_without_module_map(gen_module) -> None:
    assert gen_module._split_agents_md("# doc\n\n## Overview\n\nno map here\n") is None
