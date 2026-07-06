"""Guard: deprecated v1 lineage writers have no construction sites in src/.

Issue #2292 AC4 - v1 ``LineageRecorder`` and persistence
``LineageWriter`` must have zero remaining construction sites in
``src/``. New artifact-provenance writes go through
:class:`bernstein.core.lineage.spine.LineageSpine` at the single adapter
write boundary. This test scans the shipped source tree (not tests, not
docstrings) so a regression that reintroduces a v1 writer construction
fails CI.
"""

from __future__ import annotations

import ast
from pathlib import Path

_SRC = Path(__file__).resolve().parents[3] / "src" / "bernstein"

_FORBIDDEN_CTORS = {"LineageRecorder", "LineageWriter"}


def _construction_sites() -> list[str]:
    hits: list[str] = []
    for py in _SRC.rglob("*.py"):
        try:
            tree = ast.parse(py.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            # Direct call: LineageWriter(...) / LineageRecorder(...)
            if isinstance(func, ast.Name) and func.id in _FORBIDDEN_CTORS:
                hits.append(f"{py}:{node.lineno}: {func.id}(...)")
            # Factory call: LineageWriter.for_run(...)
            if (
                isinstance(func, ast.Attribute)
                and isinstance(func.value, ast.Name)
                and func.value.id in _FORBIDDEN_CTORS
                and func.attr in {"for_run", "create"}
            ):
                hits.append(f"{py}:{node.lineno}: {func.value.id}.{func.attr}(...)")
    return hits


def test_no_v1_writer_construction_in_src() -> None:
    sites = _construction_sites()
    assert sites == [], (
        "deprecated v1 lineage writers must not be constructed in src/; "
        "route writes through LineageSpine instead:\n" + "\n".join(sites)
    )
