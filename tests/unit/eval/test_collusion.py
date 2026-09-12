"""Tests for cross-task collusion detection (issue #5463).

Acceptance criteria:
- >= 5 colluding pairs flagged; >= 5 benign pairs pass.
- Each flag names the invariant and both tasks.
- Results are deterministic and cover all 5 invariant types.
- Flags only when the union violates and neither half does.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.eval.bench.collusion import (
    CrossTaskCollusionDetector,
    load_pair_from_fixture,
)

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "eval" / "cases" / "collusion"


def _load_fixture(name: str):
    path = FIXTURE_DIR / f"{name}.json"
    return load_pair_from_fixture(json.loads(path.read_text(encoding="utf-8-sig")))


def _all_fixtures():
    return sorted(FIXTURE_DIR.glob("*.json"))


@pytest.fixture
def detector() -> CrossTaskCollusionDetector:
    return CrossTaskCollusionDetector()


class TestColludingPairsFlagged:
    COLLUDING_FIXTURES = [
        ("colluding-01-test-split", "permissive-test"),
        ("colluding-02-shell-split", "dangerous-shell-split"),
        ("colluding-03-config-widen", "config-permission-widen"),
        ("colluding-04-gate-disable", "gate-disable"),
        ("colluding-05-sensitive-access", "sensitive-file-access-split"),
    ]

    @pytest.mark.parametrize("fixture_name,expected_invariant", COLLUDING_FIXTURES)
    def test_colluding_pair_is_flagged(
        self,
        detector: CrossTaskCollusionDetector,
        fixture_name: str,
        expected_invariant: str,
    ) -> None:
        pair = _load_fixture(fixture_name)
        result = detector.check_pair(pair)
        assert result.flags, f"{fixture_name} was not flagged but should be"
        assert any(f.invariant == expected_invariant for f in result.flags)

    def test_at_least_five_colluding_pairs_flagged(
        self, detector: CrossTaskCollusionDetector
    ) -> None:
        colluding = [f for f in _all_fixtures() if f.stem.startswith("colluding")]
        assert len(colluding) >= 5
        for path in colluding:
            pair = load_pair_from_fixture(
                json.loads(path.read_text(encoding="utf-8-sig"))
            )
            result = detector.check_pair(pair)
            assert result.flags, f"{path.stem} should be flagged, got {result.flags}"

    def test_colluding_pair_is_flagged_when_swapped(
        self, detector: CrossTaskCollusionDetector
    ) -> None:
        pair = _load_fixture("colluding-01-test-split")
        swapped_pair = type(pair)(
            pair_id=pair.pair_id + "-swapped",
            task_a=pair.task_b,
            task_b=pair.task_a,
            expected_type=pair.expected_type,
            expected_invariant=pair.expected_invariant,
        )
        result = detector.check_pair(swapped_pair)
        assert result.flags, "Swapped colluding pair was not flagged"


class TestBenignPairsPass:
    BENIGN_FIXTURES = [
        "benign-01-unrelated",
        "benign-02-real-test",
        "benign-03-docs-fix",
        "benign-04-utility-use",
        "benign-05-refactor",
    ]

    @pytest.mark.parametrize("fixture_name", BENIGN_FIXTURES)
    def test_benign_pair_is_not_flagged(
        self,
        detector: CrossTaskCollusionDetector,
        fixture_name: str,
    ) -> None:
        pair = _load_fixture(fixture_name)
        result = detector.check_pair(pair)
        assert not result.flags

    def test_at_least_five_benign_pairs_pass(
        self, detector: CrossTaskCollusionDetector
    ) -> None:
        benign = [f for f in _all_fixtures() if f.stem.startswith("benign")]
        assert len(benign) >= 5
        passed = 0
        for path in benign:
            pair = load_pair_from_fixture(
                json.loads(path.read_text(encoding="utf-8-sig"))
            )
            if not detector.check_pair(pair).flags:
                passed += 1
        assert passed >= 5


class TestFlagShape:
    def test_flag_names_invariant_and_both_tasks(
        self, detector: CrossTaskCollusionDetector
    ) -> None:
        pair = _load_fixture("colluding-01-test-split")
        result = detector.check_pair(pair)
        assert result.flags
        flag = result.flags[0]
        assert flag.invariant == "permissive-test"
        assert flag.task_a_id == "task-a"
        assert flag.task_b_id == "task-b"

    def test_flag_includes_pair_id(
        self, detector: CrossTaskCollusionDetector
    ) -> None:
        pair = _load_fixture("colluding-02-shell-split")
        result = detector.check_pair(pair)
        assert result.flags
        assert result.flags[0].pair_id == "colluding-02-shell-split"


class TestDeterminism:
    def test_repeated_check_is_identical(
        self, detector: CrossTaskCollusionDetector
    ) -> None:
        pair = _load_fixture("colluding-03-config-widen")
        assert detector.check_pair(pair) == detector.check_pair(pair)


class TestDependencyDetection:
    def test_has_dependency_when_task_b_reads_task_a_writes(
        self, detector: CrossTaskCollusionDetector
    ) -> None:
        pair = _load_fixture("colluding-01-test-split")
        assert detector.has_dependency(pair) is True

    def test_no_dependency_when_tasks_are_unrelated(
        self, detector: CrossTaskCollusionDetector
    ) -> None:
        pair = _load_fixture("benign-01-unrelated")
        assert detector.has_dependency(pair) is False

    def test_no_flags_without_dependency(
        self, detector: CrossTaskCollusionDetector
    ) -> None:
        pair = _load_fixture("colluding-01-test-split")
        no_dep_pair = type(pair)(
            pair_id=pair.pair_id + "-no-dep",
            task_a=pair.task_a,
            task_b=type(pair.task_b)(
                task_id=pair.task_b.task_id,
                writes=pair.task_b.writes,
                reads=(),
            ),
        )
        result = detector.check_pair(no_dep_pair)
        assert not result.flags
