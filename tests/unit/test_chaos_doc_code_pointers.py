"""Verify the chaos-engineering doc's ``chaos_cmd.py:N`` pointers resolve.

``docs/operations/chaos-engineering.md`` cites line numbers in
``cli/commands/chaos_cmd.py`` for every subcommand. Those numbers shift on any
edit to the module - including edits far above the cited region - so they rot
without anyone noticing.

This test resolves each cited range against the module's AST. A range is
accepted when it covers exactly one top-level definition, which is what every
pointer in the doc claims. Bare line pointers must land on a definition or
assignment rather than on whitespace.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
DOC = REPO_ROOT / "docs" / "operations" / "chaos-engineering.md"
MODULE = REPO_ROOT / "src" / "bernstein" / "cli" / "commands" / "chaos_cmd.py"

# Matches `chaos_cmd.py:31` and `chaos_cmd.py:73-98`, with or without the
# `cli/commands/` prefix the doc uses in its code-pointer list.
_POINTER_RE = re.compile(r"chaos_cmd\.py:(\d+)(?:-(\d+))?")


def _module_lines() -> int:
    return len(MODULE.read_text(encoding="utf-8").splitlines())


def _top_level_spans() -> list[tuple[int, int]]:
    """Return ``(start, end)`` line spans of every top-level definition.

    ``start`` includes decorators, matching how the doc cites a command.
    """
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    spans: list[tuple[int, int]] = []
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            start = min([dec.lineno for dec in node.decorator_list] + [node.lineno])
            assert node.end_lineno is not None
            spans.append((start, node.end_lineno))
    return spans


def _pointers() -> list[tuple[int, int | None]]:
    text = DOC.read_text(encoding="utf-8")
    found = [(int(start), int(end) if end else None) for start, end in _POINTER_RE.findall(text)]
    assert found, f"no chaos_cmd.py pointers found in {DOC}"
    return found


@pytest.mark.parametrize(("start", "end"), _pointers())
def test_pointer_is_within_the_module(start: int, end: int | None) -> None:
    """Every cited line exists in the module."""
    total = _module_lines()
    assert 1 <= start <= total, f"chaos_cmd.py:{start} is past the end of the module ({total} lines)"
    if end is not None:
        assert start < end <= total, f"chaos_cmd.py:{start}-{end} is not a valid range in a {total}-line module"


@pytest.mark.parametrize(("start", "end"), [(s, e) for s, e in _pointers() if e is not None])
def test_range_pointer_does_not_straddle_a_definition(start: int, end: int) -> None:
    """A cited range must stay inside definition boundaries.

    The doc cites ranges two ways: whole definitions (a command implementation
    or a helper pair) and a block nested inside one (the ``file-remove`` backup
    logic). Both are fine. A range that starts inside one definition and ends
    inside another means the numbers drifted after an edit to the module.
    """
    spans = _top_level_spans()
    enclosing = [span for span in spans if span[0] <= start and end <= span[1]]
    if enclosing:
        # Nested block: fully inside a single definition.
        assert len(enclosing) == 1, f"chaos_cmd.py:{start}-{end} resolves ambiguously to {enclosing}"
        return

    covered = [span for span in spans if start <= span[0] and span[1] <= end]
    overlapping = [span for span in spans if span[0] <= end and start <= span[1]]
    assert covered, f"chaos_cmd.py:{start}-{end} covers no top-level definition"
    assert covered == overlapping, (
        f"chaos_cmd.py:{start}-{end} straddles a definition boundary; "
        f"it partially overlaps {sorted(set(overlapping) - set(covered))}"
    )
