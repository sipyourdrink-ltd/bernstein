"""Covered/Partial rows in the NIST AI RMF map must cite real modules (#4915).

First-slice acceptance for the mapping document: walk every subcategory
table in ``docs/compliance/nist-ai-rmf-mapping.md``. A row whose verdict is
``Covered`` or ``Partial`` must name at least one ``src/...`` path that
exists in the tree. ``Not-covered`` rows may use an em-dash module cell.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
MAPPING = REPO_ROOT / "docs" / "compliance" / "nist-ai-rmf-mapping.md"

_SRC_PATH_RE = re.compile(r"`(src/[^`]+)`")
_VERDICT_RE = re.compile(r"\b(Covered|Partial|Not-covered)\b")
_SUBCATEGORY_RE = re.compile(r"^\| `(GOVERN|MAP|MEASURE|MANAGE)-\d+\.\d+` \|")

# Expected AI RMF 1.0 Core subcategory counts (NIST.AI.100-1 tables 1–4).
_EXPECTED_COUNTS = {
    "GOVERN": 19,
    "MAP": 18,
    "MEASURE": 22,
    "MANAGE": 13,
}


def _data_rows(text: str) -> list[tuple[str, str, str]]:
    """Return ``(subcategory_id, modules_cell, verdict)`` for each map row."""
    rows: list[tuple[str, str, str]] = []
    for line in text.splitlines():
        if not _SUBCATEGORY_RE.match(line):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 5:
            pytest.fail(f"malformed mapping row (need 5 columns): {line!r}")
        subcategory, _outcome, _mechanism, modules, verdict_cell = cells[:5]
        sub_id = subcategory.strip().strip("`")
        verdict_match = _VERDICT_RE.search(verdict_cell)
        if verdict_match is None:
            pytest.fail(f"{sub_id}: verdict cell {verdict_cell!r} is not Covered/Partial/Not-covered")
        rows.append((sub_id, modules, verdict_match.group(1)))
    return rows


def test_mapping_document_exists() -> None:
    assert MAPPING.is_file(), f"missing {MAPPING.relative_to(REPO_ROOT)}"


def test_all_core_subcategories_are_present() -> None:
    rows = _data_rows(MAPPING.read_text(encoding="utf-8"))
    by_fn: dict[str, list[str]] = {"GOVERN": [], "MAP": [], "MEASURE": [], "MANAGE": []}
    for sub_id, _modules, _verdict in rows:
        fn = sub_id.split("-", 1)[0]
        by_fn[fn].append(sub_id)
    for fn, expected in _EXPECTED_COUNTS.items():
        assert len(by_fn[fn]) == expected, f"{fn}: expected {expected} rows, found {len(by_fn[fn])}: {by_fn[fn]}"
    assert len(rows) == sum(_EXPECTED_COUNTS.values())


def test_covered_and_partial_rows_cite_existing_modules() -> None:
    rows = _data_rows(MAPPING.read_text(encoding="utf-8"))
    failures: list[str] = []
    for sub_id, modules_cell, verdict in rows:
        paths = _SRC_PATH_RE.findall(modules_cell)
        if verdict in {"Covered", "Partial"}:
            if not paths:
                failures.append(f"{sub_id} ({verdict}): no src/ module cited")
                continue
            for rel in paths:
                if not (REPO_ROOT / rel).exists():
                    failures.append(f"{sub_id} ({verdict}): missing {rel}")
        elif verdict == "Not-covered" and paths:
            # Not-covered may still cite aspirational paths only if they exist;
            # dangling paths are still a docs bug.
            for rel in paths:
                if not (REPO_ROOT / rel).exists():
                    failures.append(f"{sub_id} (Not-covered): missing {rel}")
    assert not failures, "NIST AI RMF mapping module drift:\n" + "\n".join(failures)


def test_tldr_counts_match_table() -> None:
    text = MAPPING.read_text(encoding="utf-8")
    rows = _data_rows(text)
    tallies = {"Covered": 0, "Partial": 0, "Not-covered": 0}
    for _sub, _mod, verdict in rows:
        tallies[verdict] += 1
    for verdict, count in tallies.items():
        pattern = rf"\| {re.escape(verdict)} \| {count} \|"
        assert re.search(pattern, text), f"TL;DR count for {verdict} should be {count}"


def test_partial_not_folded_into_not_covered() -> None:
    """Half-satisfied rows must use Partial (decision recorded in the doc)."""
    text = MAPPING.read_text(encoding="utf-8")
    assert "**Decision (stated for reviewers):**" in text
    assert "half-satisfied rows use **Partial**" in text
    rows = _data_rows(text)
    assert any(v == "Partial" for _, _, v in rows)
    assert any(v == "Not-covered" for _, _, v in rows)
    assert any(v == "Covered" for _, _, v in rows)
