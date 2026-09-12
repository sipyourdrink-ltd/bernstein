"""The capability-contract doc table must agree with ``STRATEGY_MATRIX``.

``docs/adapters/capability_contract.md`` carries an operator-facing adapter
table (adapter, resume, dangerous mode, event channel). The authoritative
declaration is :data:`bernstein.adapters._contract.STRATEGY_MATRIX`, rendered
by :func:`strategy_table`. The doc table is hand-maintained, so correcting a
matrix row without editing the doc silently drifts the two apart -- which is
what happened to ``copilot``, ``kilo`` and ``letta_code`` (#5597).

These tests make that class of drift a CI failure instead of a discovery.
"""

from __future__ import annotations

import re
from pathlib import Path

from bernstein.adapters._contract import strategy_table

#: The doc table's row shape: ``| `name` | resume | dangerous | channel |``.
_ROW = re.compile(
    r"^\|\s*`(?P<adapter>[a-z0-9_]+)`"
    r"\s*\|\s*(?P<resume>[^|]+?)"
    r"\s*\|\s*(?P<dangerous_mode>[^|]+?)"
    r"\s*\|\s*(?P<event_channel>[^|]+?)\s*\|\s*$"
)

_DOC = Path(__file__).resolve().parents[3] / "docs" / "adapters" / "capability_contract.md"


def _doc_rows() -> dict[str, dict[str, str]]:
    """Return the doc table's adapter rows, keyed by adapter name."""
    rows: dict[str, dict[str, str]] = {}
    for line in _DOC.read_text(encoding="utf-8").splitlines():
        match = _ROW.match(line)
        if match is None:
            continue
        rows[match.group("adapter")] = {
            axis: match.group(axis) for axis in ("resume", "dangerous_mode", "event_channel")
        }
    return rows


def _matrix_rows() -> dict[str, dict[str, str]]:
    """Return the authoritative matrix rows, keyed by adapter name."""
    return {row["adapter"]: dict(row) for row in strategy_table()}


def test_doc_table_is_parsed_at_all() -> None:
    """A regex that matches nothing would make every other check vacuous."""
    rows = _doc_rows()
    assert len(rows) > 20, f"parsed only {len(rows)} adapter rows from {_DOC}"


def test_every_doc_row_names_a_declared_adapter() -> None:
    """A doc row for an adapter with no matrix entry is stale by definition."""
    unknown = sorted(set(_doc_rows()) - set(_matrix_rows()))
    assert not unknown, (
        f"{_DOC.name} documents adapters absent from STRATEGY_MATRIX: {unknown}. "
        "Remove the row, or declare the adapter in the matrix."
    )


def test_doc_table_agrees_with_the_strategy_matrix() -> None:
    """Every adapter present in both must carry identical axis values.

    Adapters declared in the matrix but not yet listed in the doc are not
    failures here -- the doc is an operator-facing excerpt, and completing it
    is a separate question from keeping the rows it does carry truthful.
    """
    doc, matrix = _doc_rows(), _matrix_rows()
    drift = {
        adapter: {
            axis: (doc[adapter][axis], matrix[adapter][axis])
            for axis in ("resume", "dangerous_mode", "event_channel")
            if doc[adapter][axis] != matrix[adapter][axis]
        }
        for adapter in sorted(doc.keys() & matrix.keys())
    }
    drift = {adapter: axes for adapter, axes in drift.items() if axes}
    assert not drift, (
        f"{_DOC.name} disagrees with STRATEGY_MATRIX (doc value, matrix value): {drift}. "
        "The matrix is authoritative; update the doc table."
    )
