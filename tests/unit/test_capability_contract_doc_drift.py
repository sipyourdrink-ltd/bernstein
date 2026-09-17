"""``docs/adapters/capability_contract.md`` must match ``STRATEGY_MATRIX`` (#5627).

``STRATEGY_MATRIX`` in ``src/bernstein/adapters/_contract.py`` is the
authoritative declaration; the doc table is a rendering of it for
operators. Three rows (``copilot``, ``kilo``, ``letta_code``) drifted from
the matrix silently -- nothing compared the two files, so a row could be
wrong for as long as nobody happened to read both at once. This test reads
the real matrix and the rendered markdown table and fails on any row where
they disagree, so the next drift surfaces in CI instead of in a future
issue like this one.
"""

from __future__ import annotations

import re
from pathlib import Path

from bernstein.adapters._contract import strategy_table

_REPO_ROOT = Path(__file__).resolve().parents[2]
_DOC = _REPO_ROOT / "docs" / "adapters" / "capability_contract.md"

#: One row of the big adapter table: `` | `name` | resume | dangerous | channel | ``.
_ROW_RE = re.compile(r"^\|\s*`([a-z0-9_]+)`\s*\|\s*([a-z-]+)\s*\|\s*([a-z-]+)\s*\|\s*([a-z-]+)\s*\|", re.MULTILINE)


def _doc_rows() -> dict[str, tuple[str, str, str]]:
    text = _DOC.read_text(encoding="utf-8")
    return {name: (resume, dangerous, channel) for name, resume, dangerous, channel in _ROW_RE.findall(text)}


def test_every_documented_adapter_row_matches_the_strategy_matrix() -> None:
    doc_rows = _doc_rows()
    matrix_rows = {row["adapter"]: row for row in strategy_table()}

    # Every adapter this test can check must actually be documented -- a
    # name present in the matrix but silently absent from the table is its
    # own kind of drift, distinct from a wrong row.
    documented_adapters = set(doc_rows) & set(matrix_rows)
    assert documented_adapters, "the table regex matched nothing -- did the table's layout change?"

    mismatches: list[str] = []
    for name in sorted(documented_adapters):
        doc_resume, doc_dangerous, doc_channel = doc_rows[name]
        matrix_row = matrix_rows[name]
        actual = (matrix_row["resume"], matrix_row["dangerous_mode"], matrix_row["event_channel"])
        documented = (doc_resume, doc_dangerous, doc_channel)
        if documented != actual:
            mismatches.append(
                f"`{name}`: docs say {documented!r}, STRATEGY_MATRIX says {actual!r}",
            )

    assert not mismatches, "capability_contract.md disagrees with STRATEGY_MATRIX:\n" + "\n".join(mismatches)
