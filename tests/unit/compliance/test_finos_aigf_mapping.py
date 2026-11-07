"""Offline drift checks for the FINOS mitigation/risk crosswalk (issue #6148)."""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from bernstein.compliance.finos_aigf import MITIGATIONS, RISKS, SPEC_COMMIT

_REPO_ROOT = Path(__file__).resolve().parents[3]
_PAGE = _REPO_ROOT / "docs" / "compliance" / "finos-aigf-mapping.md"
_VERDICTS = ("Covered", "Partial", "Not covered", "Out of scope")


def _section(page: str, number: int) -> str:
    match = re.search(rf"(?ms)^## {number}\. [^\n]+\n(.*?)(?=^## \d+\. |\Z)", page)
    assert match, f"FINOS mapping section {number} is missing"
    return match.group(1)


def _inventory_rows(section: str, prefix: str, width: int) -> list[list[str]]:
    rows = [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in section.splitlines()
        if line.startswith("| `" + prefix + "-")
    ]
    assert rows, f"missing {prefix} inventory"
    assert all(len(row) == width for row in rows), f"unexpected {prefix} table layout: {rows}"
    return rows


def _checked_ids(rows: list[list[str]], prefix: str) -> list[str]:
    ids: list[str] = []
    for row in rows:
        match = re.fullmatch(rf"`({prefix}-\d+)`", row[0])
        assert match, f"malformed FINOS {prefix} identifier: {row[0]}"
        ids.append(match.group(1))
    assert len(ids) == len(set(ids)), f"duplicate FINOS {prefix} identifiers"
    return ids


class TestRegistryIsDocumented:
    def test_mitigation_rows_match_the_canonical_catalogue(self) -> None:
        page = _PAGE.read_text(encoding="utf-8")
        rows = _inventory_rows(_section(page, 1), "mi", 5)
        ids = _checked_ids(rows, "mi")
        assert set(ids) == set(MITIGATIONS)

        for mid, row in zip(ids, rows, strict=True):
            _identifier, title, kind, evidence, verdict = row
            assert (title, kind) == MITIGATIONS[mid], mid
            assert evidence, f"{mid} needs a documented implementation or limitation"
            assert verdict in _VERDICTS, (mid, verdict)
            if verdict != "Out of scope":
                cited_paths = re.findall(r"`(src/bernstein/[^`]+)`", evidence)
                assert cited_paths, f"{mid} needs source-backed evidence"
                for path in cited_paths:
                    assert (_REPO_ROOT / path).exists(), (mid, path)

    def test_risk_rows_match_the_canonical_catalogue(self) -> None:
        page = _PAGE.read_text(encoding="utf-8")
        rows = _inventory_rows(_section(page, 2), "ri", 6)
        ids = _checked_ids(rows, "ri")
        assert set(ids) == set(RISKS)

        for rid, row in zip(ids, rows, strict=True):
            _identifier, title, kind, related, response, verdict = row
            assert (title, kind) == RISKS[rid], rid
            links = re.findall(r"`(mi-\d+)`", related)
            assert links and len(links) == len(set(links)), (rid, related)
            assert set(links) <= set(MITIGATIONS), (rid, related)
            assert response, f"{rid} needs a risk assessment"
            assert verdict in _VERDICTS, (rid, verdict)

    def test_mitigation_coverage_headline_matches_the_table(self) -> None:
        page = _PAGE.read_text(encoding="utf-8")
        rows = _inventory_rows(_section(page, 1), "mi", 5)
        counts = Counter(row[-1] for row in rows)
        assert set(counts) <= set(_VERDICTS)
        assert sum(counts.values()) == len(MITIGATIONS)

        summary = page.split("## TL;DR", 1)[1].split("## 1.", 1)[0]
        for status in _VERDICTS:
            matches = re.findall(rf"(?m)^\|\s*{re.escape(status)}\s*\|\s*(\d+)/(\d+)\s*\|", summary)
            assert len(matches) == 1, f"missing/duplicate summary for {status}"
            numerator, denominator = map(int, matches[0])
            assert numerator == counts[status], status
            assert denominator == len(MITIGATIONS), status

        net = re.search(
            r"\*\*Net result: (\d+) of (\d+) mitigations Covered; "
            r"(\d+) Partial; (\d+) Not covered; (\d+) Out of scope\.\*\*",
            _section(page, 1),
        )
        assert net, "missing verdict totals after mitigation table"
        assert tuple(map(int, net.groups())) == (
            counts["Covered"],
            len(MITIGATIONS),
            counts["Partial"],
            counts["Not covered"],
            counts["Out of scope"],
        )

    def test_crosswalk_references_and_source_commit_are_current(self) -> None:
        page = _PAGE.read_text(encoding="utf-8")
        assert SPEC_COMMIT in page
        assert not re.search(r"\b(?:CTRL|AIR|AIGF)-[A-Z0-9-]+\b", page), "retired FINOS IDs in mapping"

        section = _section(page, 3)
        mapping_rows = [
            line
            for line in section.splitlines()
            if line.startswith(("| EU AI Act |", "| DORA |", "| SR 11-7 |", "| ISO 42001 |"))
        ]
        assert len(mapping_rows) == 4
        for row in mapping_rows:
            ids = re.findall(r"`(mi-\d+)`", row)
            assert ids, f"missing FINOS references in {row}"
            assert set(ids) <= set(MITIGATIONS), row
