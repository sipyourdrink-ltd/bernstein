"""The shipped authority corpus, checked as data (#5452 piece 2).

These pin the invariants the loader and piece 3's receipt-fidelity test lean on:
the file name equals the case id, ids are unique, and every case attempts exactly
one action above its declared level. Data-only -- no adapter is run here.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.eval.bench.authority_levels import AuthorityLevel

# These tests glob the shipped corpus under src/, so they scan the source tree
# and must declare themselves whole-tree guards (tests/unit/test_whole_tree_guards_are_marked.py).
pytestmark = pytest.mark.whole_tree_guard

_CASES_DIR = Path(__file__).resolve().parents[4] / "src" / "bernstein" / "eval" / "cases" / "authority"


def _cases() -> list[tuple[Path, dict]]:
    return [(p, json.loads(p.read_text(encoding="utf-8"))) for p in sorted(_CASES_DIR.glob("*.json"))]


def test_the_corpus_ships_twenty_cases_four_per_level() -> None:
    cases = _cases()
    assert len(cases) == 20
    per_level: dict[str, int] = {}
    for _p, data in cases:
        per_level[data["declared_level"]] = per_level.get(data["declared_level"], 0) + 1
    assert per_level == {"L0": 4, "L1": 4, "L2": 4, "L3": 4, "L4": 4}


def test_the_file_name_equals_the_case_id() -> None:
    """Load-bearing: load_authority_cases and piece 3's test read <id>.json."""
    mismatches = [(p.name, data["id"]) for p, data in _cases() if p.stem != data["id"]]
    assert mismatches == []


def test_case_ids_are_unique() -> None:
    ids = [data["id"] for _p, data in _cases()]
    assert len(set(ids)) == len(ids)


def test_every_case_attempts_one_action_above_its_declared_level() -> None:
    """The corpus is a containment corpus: each case declares a level and attempts
    an action requiring a strictly higher one. L5 is only ever a required level."""
    for p, data in _cases():
        declared = AuthorityLevel.from_str(data["declared_level"])
        required = AuthorityLevel.from_str(data["attempted_action"]["required_level"])
        assert required > declared, f"{p.name}: required {required.name} !> declared {declared.name}"
        assert declared != AuthorityLevel.L5, f"{p.name}: L5 must never be declared"


def test_category_matches_the_declared_level() -> None:
    for p, data in _cases():
        assert data["category"] == f"authority_{data['declared_level'].lower()}", p.name


def test_every_expected_outcome_is_blocked_by_policy() -> None:
    """This is a containment corpus; the v2 approval/permitted cases are a separate issue."""
    for p, data in _cases():
        contained = next(a for a in data["assertions"] if a["kind"] == "authority_contained")
        assert contained["expected_outcome"] == "blocked_by_policy", p.name
