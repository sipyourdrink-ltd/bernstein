"""Regression tests verifying docs/adapters/capability_contract.md matches STRATEGY_MATRIX."""

from __future__ import annotations

import re
from pathlib import Path

from bernstein.adapters._contract import strategy_for

DOC_PATH = Path(__file__).resolve().parents[2] / "docs" / "adapters" / "capability_contract.md"
TABLE_ROW_RE = re.compile(
    r"^\|\s*`(?P<adapter>[^`]+)`\s*\|\s*(?P<resume>[^|]+)\s*\|\s*(?P<dangerous>[^|]+)\s*\|\s*(?P<channel>[^|]+)\s*\|"
)


def _parse_doc_table() -> list[tuple[str, str, str, str]]:
    text = DOC_PATH.read_text(encoding="utf-8")
    rows: list[tuple[str, str, str, str]] = []
    for line in text.splitlines():
        m = TABLE_ROW_RE.match(line.strip())
        if m:
            rows.append(
                (
                    m.group("adapter").strip(),
                    m.group("resume").strip(),
                    m.group("dangerous").strip(),
                    m.group("channel").strip(),
                )
            )
    return rows


def test_capability_contract_doc_table_matches_strategy_matrix() -> None:
    """Every row in docs/adapters/capability_contract.md must match STRATEGY_MATRIX."""
    rows = _parse_doc_table()
    assert rows, f"No table rows found in {DOC_PATH}"

    mismatches: list[str] = []
    for adapter, resume, dangerous, channel in rows:
        strat = strategy_for(adapter).to_dict()
        expected_resume = strat["resume"]
        expected_dangerous = strat["dangerous_mode"]
        expected_channel = strat["event_channel"]

        if (resume, dangerous, channel) != (expected_resume, expected_dangerous, expected_channel):
            mismatches.append(
                f"{adapter}: doc=({resume}, {dangerous}, {channel}) != "
                f"expected=({expected_resume}, {expected_dangerous}, {expected_channel})"
            )

    assert not mismatches, "Doc table drifts from STRATEGY_MATRIX:\n" + "\n".join(mismatches)
