"""
TDD tests for bernstein-bench rotation, private holdout, and contamination check (Issue #5459).

Acceptance criteria covered:
1. Versioned manifests with content hash; bundles carry version and holdout hash.
2. Private holdout set is never published; its hash is published so results can be pinned;
   the holdout runner refuses to write results anywhere public by construction.
3. Task admission gate: reference solution is fingerprinted (n-gram) and rejected when
   found verbatim/overlapping in public code hosting; verdict is recorded.
4. Rotation: when the public-set pass rate exceeds 0.9 across three consecutive baselines,
   rotation is due; saturation is tracked across baselines.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from bernstein.eval.bench.bundle import SubmissionBundle
from bernstein.eval.bench.contamination import (
    ContaminationVerdict,
    admit_task,
    check_solution_contamination,
)
from bernstein.eval.bench.leaderboard import Leaderboard, LeaderboardEntry
from bernstein.eval.bench.rotation import RotationStatus, check_suite_saturation
from bernstein.eval.bench.runner import (
    BenchRunner,
    HoldoutBenchRunner,
    HoldoutIsolationError,
    MockReplayAdapter,
)
from bernstein.eval.bench.suite import BenchSuite, BenchTask

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sample_public_tasks() -> list[BenchTask]:
    return [
        BenchTask(
            id="pub_task_1",
            description="Public Task 1",
            steps=("step 1",),
            assertions=({"kind": "file_exists"},),
            category="io",
        ),
        BenchTask(
            id="pub_task_2",
            description="Public Task 2",
            steps=("step 1", "step 2"),
            assertions=({"kind": "syntax_valid"},),
            category="refactor",
        ),
    ]


@pytest.fixture()
def sample_holdout_tasks() -> list[BenchTask]:
    return [
        BenchTask(
            id="holdout_task_1",
            description="Private Holdout Task 1",
            steps=("private step 1",),
            assertions=({"kind": "secret_verified"},),
            category="security",
        ),
        BenchTask(
            id="holdout_task_2",
            description="Private Holdout Task 2",
            steps=("private step 2",),
            assertions=({"kind": "tamper_detected"},),
            category="security",
        ),
    ]


# ===========================================================================
# 1. Versioned manifests and holdout hash binding
# ===========================================================================


class TestVersionedManifestsAndHoldoutBinding:
    """AC-1: Versioned manifests; bundles carry version and holdout hash."""

    def test_suite_with_holdout_computes_deterministic_holdout_hash(
        self, sample_public_tasks: list[BenchTask], sample_holdout_tasks: list[BenchTask]
    ) -> None:
        suite1 = BenchSuite(
            version="golden-v2",
            tasks=sample_public_tasks,
            holdout_tasks=sample_holdout_tasks,
        )
        suite2 = BenchSuite(
            version="golden-v2",
            tasks=sample_public_tasks,
            holdout_tasks=sample_holdout_tasks,
        )
        assert suite1.holdout_hash != ""
        assert suite1.holdout_hash == suite2.holdout_hash
        assert suite1.suite_hash == suite2.suite_hash

    def test_changing_holdout_task_changes_holdout_hash_and_suite_hash(
        self, sample_public_tasks: list[BenchTask], sample_holdout_tasks: list[BenchTask]
    ) -> None:
        suite = BenchSuite(
            version="golden-v2",
            tasks=sample_public_tasks,
            holdout_tasks=sample_holdout_tasks,
        )
        mutated_holdout = [
            sample_holdout_tasks[0],
            BenchTask(
                id="holdout_task_2_mod",
                description="Modified Holdout",
                steps=("new step",),
                assertions=(),
            ),
        ]
        suite_mod = BenchSuite(
            version="golden-v2",
            tasks=sample_public_tasks,
            holdout_tasks=mutated_holdout,
        )
        assert suite_mod.holdout_hash != suite.holdout_hash
        assert suite_mod.suite_hash != suite.suite_hash

    def test_suite_manifest_serialisation_never_exposes_holdout_tasks(
        self, sample_public_tasks: list[BenchTask], sample_holdout_tasks: list[BenchTask]
    ) -> None:
        suite = BenchSuite(
            version="golden-v2",
            tasks=sample_public_tasks,
            holdout_tasks=sample_holdout_tasks,
        )
        d = suite.to_dict()
        assert d["holdout_hash"] == suite.holdout_hash
        assert len(d["tasks"]) == len(sample_public_tasks)
        # Ensure holdout task IDs or descriptions are NOT serialized to the public manifest dict
        for task_dict in d["tasks"]:
            assert "holdout" not in task_dict["id"]
        assert "holdout_tasks" not in d

    def test_bundle_carries_version_and_holdout_hash(
        self, sample_public_tasks: list[BenchTask], sample_holdout_tasks: list[BenchTask]
    ) -> None:
        suite = BenchSuite(
            version="golden-v2",
            tasks=sample_public_tasks,
            holdout_tasks=sample_holdout_tasks,
        )
        adapter = MockReplayAdapter()
        runner = BenchRunner(suite=suite, adapter=adapter, scheduler_config={"scheduler": "deterministic"})
        bundle = runner.run()

        assert bundle.suite_version == "golden-v2"
        assert bundle.holdout_hash == suite.holdout_hash

        # Check serialization round-trip
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "bundle.json"
            bundle.save(p)
            loaded = SubmissionBundle.load(p)
            assert loaded.suite_version == "golden-v2"
            assert loaded.holdout_hash == suite.holdout_hash
            assert loaded.bundle_hash() == bundle.bundle_hash()


# ===========================================================================
# 2. Private holdout runner and isolation enforcement
# ===========================================================================


class TestPrivateHoldoutIsolation:
    """AC-2: Local-only runner path for holdout; refuses public emission by construction."""

    def test_holdout_runner_executes_private_tasks(
        self, sample_public_tasks: list[BenchTask], sample_holdout_tasks: list[BenchTask]
    ) -> None:
        suite = BenchSuite(
            version="golden-v2",
            tasks=sample_public_tasks,
            holdout_tasks=sample_holdout_tasks,
        )
        adapter = MockReplayAdapter()
        runner = HoldoutBenchRunner(suite=suite, adapter=adapter, scheduler_config={})
        bundle = runner.run()

        assert bundle.suite_version == "golden-v2"
        assert bundle.holdout_hash == suite.holdout_hash
        assert len(bundle.task_results) == len(sample_holdout_tasks)
        task_ids = {r.task_id for r in bundle.task_results}
        assert task_ids == {"holdout_task_1", "holdout_task_2"}

    def test_holdout_runner_refuses_public_emission_by_construction(
        self, sample_public_tasks: list[BenchTask], sample_holdout_tasks: list[BenchTask]
    ) -> None:
        suite = BenchSuite(
            version="golden-v2",
            tasks=sample_public_tasks,
            holdout_tasks=sample_holdout_tasks,
        )
        adapter = MockReplayAdapter()
        runner = HoldoutBenchRunner(suite=suite, adapter=adapter, scheduler_config={})

        with tempfile.TemporaryDirectory() as tmp:
            public_path = Path(tmp) / "public_export" / "holdout_bundle.json"
            # Attempting to export to a public path or with publish=True must raise HoldoutIsolationError
            with pytest.raises(HoldoutIsolationError, match="refuse.*public"):
                runner.save_result(runner.run(), public_path, is_public=True)

    def test_holdout_runner_fails_if_no_holdout_tasks(self, sample_public_tasks: list[BenchTask]) -> None:
        suite = BenchSuite(version="golden-v1", tasks=sample_public_tasks)
        adapter = MockReplayAdapter()
        runner = HoldoutBenchRunner(suite=suite, adapter=adapter, scheduler_config={})
        with pytest.raises(ValueError, match="No holdout tasks"):
            runner.run()


# ===========================================================================
# 3. Admission gate: Contamination check (n-gram fingerprinting)
# ===========================================================================


class TestContaminationCheckAdmission:
    """AC-3: Contamination check runs at admission with a recorded verdict."""

    def test_verbatim_public_solution_is_rejected_as_contaminated(self) -> None:
        public_corpus = [
            "def solve_problem():\n    return sum(x for x in range(10) if x % 2 == 0)",
            "import os\ndef read_config(path):\n    with open(path) as f:\n        return f.read()",
        ]

        solution = "def solve_problem():\n    return sum(x for x in range(10) if x % 2 == 0)"
        verdict = check_solution_contamination(solution, public_corpus, n=5, threshold=0.8)

        assert isinstance(verdict, ContaminationVerdict)
        assert verdict.is_contaminated is True
        assert verdict.overlap_score >= 0.8
        assert len(verdict.matched_ngrams) > 0

    def test_novel_solution_passes_contamination_check(self) -> None:
        public_corpus = [
            "def calculate_tax(income, rate):\n    return income * rate",
            "class AgentManager:\n    def __init__(self):\n        self.agents = []",
        ]

        novel_solution = """
        def deterministic_receipt_validator(journal_bytes, spine_hash, threshold=0.95):
            computed = hashlib.sha256(journal_bytes + spine_hash.encode()).hexdigest()
            return computed.startswith("0000") and threshold > 0.5
        """
        verdict = check_solution_contamination(novel_solution, public_corpus, n=5, threshold=0.8)

        assert verdict.is_contaminated is False
        assert verdict.overlap_score < 0.8

    def test_admit_task_records_verdict(self) -> None:
        task = BenchTask(
            id="novel_task",
            description="A brand new task",
            steps=("run novel step",),
            assertions=({"kind": "assert_novel"},),
        )
        public_corpus = ["def existing_code(): pass"]
        novel_solution = "def brand_new_unique_algorithm(data):\n    return [d * 42 for d in data]"

        admitted, verdict = admit_task(task, novel_solution, public_corpus, n=5, threshold=0.8)
        assert admitted is True
        assert verdict.is_contaminated is False

        # Now test rejected admission
        contaminated_solution = "def existing_code(): pass"
        admitted_bad, verdict_bad = admit_task(task, contaminated_solution, public_corpus, n=2, threshold=0.8)
        assert admitted_bad is False
        assert verdict_bad.is_contaminated is True


# ===========================================================================
# 4. Rotation tracking and saturation across baselines
# ===========================================================================


class TestRotationAndSaturationTracking:
    """AC-4: When public-set pass rate exceeds 0.9 across 3 consecutive baselines, rotation is due."""

    def _make_dummy_entry(self, pass_rate: float, ts: float) -> LeaderboardEntry:
        return LeaderboardEntry(
            bundle_hash=f"bundle_{ts}",
            suite_hash="suite_hash_v1",
            suite_version="golden-v1",
            overall_score=pass_rate,
            pass_rate=pass_rate,
            num_tasks=10,
            submitted_at=ts,
        )

    def test_saturation_detected_after_three_consecutive_high_pass_rates(self) -> None:
        baselines = [
            self._make_dummy_entry(0.85, 1000.0),
            self._make_dummy_entry(0.92, 2000.0),
            self._make_dummy_entry(0.95, 3000.0),
            self._make_dummy_entry(0.91, 4000.0),
        ]
        status = check_suite_saturation(baselines, threshold=0.9, consecutive_required=3)

        assert isinstance(status, RotationStatus)
        assert status.rotation_due is True
        assert status.consecutive_count == 3
        assert status.recent_pass_rates == (0.92, 0.95, 0.91)
        assert "saturated" in status.reason.lower() or "rotation" in status.reason.lower()

    def test_rotation_not_due_when_pass_rate_drops(self) -> None:
        baselines = [
            self._make_dummy_entry(0.95, 1000.0),
            self._make_dummy_entry(0.95, 2000.0),
            self._make_dummy_entry(0.88, 3000.0),  # drop below 0.9
            self._make_dummy_entry(0.95, 4000.0),
        ]
        status = check_suite_saturation(baselines, threshold=0.9, consecutive_required=3)

        assert status.rotation_due is False
        assert status.consecutive_count == 1

    def test_rotation_not_due_with_insufficient_baselines(self) -> None:
        baselines = [
            self._make_dummy_entry(0.95, 1000.0),
            self._make_dummy_entry(0.95, 2000.0),
        ]
        status = check_suite_saturation(baselines, threshold=0.9, consecutive_required=3)
        assert status.rotation_due is False
        assert status.consecutive_count == 2

    def test_leaderboard_reports_rotation_status(self) -> None:
        lb = Leaderboard(suite_hash="suite_hash_v1", suite_version="golden-v1")
        lb.add_entry(self._make_dummy_entry(0.95, 1000.0))
        lb.add_entry(self._make_dummy_entry(0.95, 2000.0))
        lb.add_entry(self._make_dummy_entry(0.95, 3000.0))

        status = lb.check_rotation_due(threshold=0.9, consecutive_required=3)
        assert status.rotation_due is True
        md = lb.to_markdown()
        assert "ROTATION DUE" in md or "Rotation" in md
