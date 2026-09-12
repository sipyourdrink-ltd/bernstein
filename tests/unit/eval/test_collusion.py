"""Tests for cross-task collusion detection (issue #5398).

Acceptance criteria:
- ≥ 5 colluding pairs flagged; ≥ 5 benign pairs pass.
- Each flag names the invariant and both tasks.
- Results are deterministic and cover all 5 invariant types.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from bernstein.eval.bench.collusion import (
    CollusionFlag,
    CrossTaskCollusionDetector,
    load_pair_from_fixture,
)

FIXTURE_DIR = Path(__file__).resolve().parents[2] / "eval" / "cases" / "collusion"


def _load_fixture(name: str):
    path = FIXTURE_DIR / f"{name}.json"
    return load_pair_from_fixture(json.loads(path.read_text(encoding="utf-8")))


def _all_fixtures():
    return sorted(FIXTURE_DIR.glob("*.json"))


@pytest.fixture
def detector() -> CrossTaskCollusionDetector:
    return CrossTaskCollusionDetector()


# ---------------------------------------------------------------------------
# AC: ≥ 5 colluding pairs flagged
# ---------------------------------------------------------------------------


class TestColludingPairsFlagged:
    """All 5 colluding pairs must be flagged with the correct invariant."""

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
        assert any(f.invariant == expected_invariant for f in result.flags), (
            f"{fixture_name} expected invariant {expected_invariant}, got {[f.invariant for f in result.flags]}"
        )

    def test_at_least_five_colluding_pairs_flagged(self, detector: CrossTaskCollusionDetector) -> None:
        """Acceptance criterion: ≥ 5 colluding pairs flagged."""
        colluding = [f for f in _all_fixtures() if f.stem.startswith("colluding")]
        assert len(colluding) >= 5, f"Need ≥ 5 colluding fixtures, found {len(colluding)}"

        flagged = 0
        for path in colluding:
            pair = load_pair_from_fixture(json.loads(path.read_text(encoding="utf-8")))
            result = detector.check_pair(pair)
            if result.flags:
                flagged += 1

        assert flagged >= 5, f"Only {flagged} of {len(colluding)} colluding pairs were flagged"


# ---------------------------------------------------------------------------
# AC: ≥ 5 benign pairs pass
# ---------------------------------------------------------------------------


class TestBenignPairsPass:
    """All 5 benign pairs must NOT be flagged."""

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

        assert not result.flags, f"{fixture_name} was flagged but should not be: {[f.invariant for f in result.flags]}"

    def test_at_least_five_benign_pairs_pass(self, detector: CrossTaskCollusionDetector) -> None:
        """Acceptance criterion: ≥ 5 benign pairs pass."""
        benign = [f for f in _all_fixtures() if f.stem.startswith("benign")]
        assert len(benign) >= 5, f"Need ≥ 5 benign fixtures, found {len(benign)}"

        passed = 0
        for path in benign:
            pair = load_pair_from_fixture(json.loads(path.read_text(encoding="utf-8")))
            result = detector.check_pair(pair)
            if not result.flags:
                passed += 1

        assert passed >= 5, f"Only {passed} of {len(benign)} benign pairs passed"


# ---------------------------------------------------------------------------
# AC: Each flag names the invariant and both tasks
# ---------------------------------------------------------------------------


class TestFlagShape:
    """Every flag must name the invariant, task_a_id, and task_b_id."""

    def test_flag_names_invariant_and_both_tasks(self, detector: CrossTaskCollusionDetector) -> None:
        pair = _load_fixture("colluding-01-test-split")
        result = detector.check_pair(pair)

        assert result.flags
        flag = result.flags[0]

        assert flag.invariant == "permissive-test"
        assert flag.task_a_id == "task-a"
        assert flag.task_b_id == "task-b"
        assert flag.detail  # non-empty

    def test_flag_includes_pair_id(self, detector: CrossTaskCollusionDetector) -> None:
        pair = _load_fixture("colluding-02-shell-split")
        result = detector.check_pair(pair)

        assert result.flags
        assert result.flags[0].pair_id == "colluding-02-shell-split"


# ---------------------------------------------------------------------------
# AC: Results are deterministic
# ---------------------------------------------------------------------------


class TestDeterminism:
    """The same pair checked twice must produce identical results."""

    def test_repeated_check_is_identical(self, detector: CrossTaskCollusionDetector) -> None:
        pair = _load_fixture("colluding-03-config-widen")
        result_1 = detector.check_pair(pair)
        result_2 = detector.check_pair(pair)

        assert result_1 == result_2


# ---------------------------------------------------------------------------
# AC: Dependency detection
# ---------------------------------------------------------------------------


class TestDependencyDetection:
    """The detector correctly identifies lineage dependencies between tasks."""

    def test_has_dependency_when_task_b_reads_task_a_writes(self, detector: CrossTaskCollusionDetector) -> None:
        pair = _load_fixture("colluding-01-test-split")
        assert detector.has_dependency(pair) is True

    def test_no_dependency_when_tasks_are_unrelated(self, detector: CrossTaskCollusionDetector) -> None:
        pair = _load_fixture("benign-01-unrelated")
        assert detector.has_dependency(pair) is False
