"""Tests for the evaluation harness — multiplicative scoring, task eval, persistence."""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.eval.golden import GoldenTask
from bernstein.eval.harness import (
    EvalHarness,
    EvalResult,
    EvalTier,
    TaskEvalResult,
)
from bernstein.eval.judge import JudgeVerdict
from bernstein.eval.metrics import EvalScoreComponents, TierScores
from bernstein.eval.taxonomy import FailureCategory, FailureRecord, FailureTaxonomy, classify_failure
from bernstein.eval.telemetry import AgentTelemetry

# ---------------------------------------------------------------------------
# EvalTier
# ---------------------------------------------------------------------------


class TestEvalTier:
    def test_smoke_tier(self) -> None:
        assert EvalTier("smoke") == EvalTier.SMOKE

    def test_standard_tier(self) -> None:
        assert EvalTier("standard") == EvalTier.STANDARD

    def test_full_tier(self) -> None:
        assert EvalTier("full") == EvalTier.FULL


# ---------------------------------------------------------------------------
# TaskEvalResult
# ---------------------------------------------------------------------------


class TestTaskEvalResult:
    def test_defaults(self) -> None:
        r = TaskEvalResult(task_id="t1", tier="smoke")
        assert r.task_id == "t1"
        assert r.tier == "smoke"
        assert r.passed is False
        assert r.telemetry is None
        assert r.judge_verdict is None
        assert r.failure is None
        assert r.duration_s == 0.0
        assert r.cost_usd == 0.0

    def test_passed_result(self) -> None:
        r = TaskEvalResult(task_id="t2", tier="standard", passed=True, cost_usd=0.12)
        assert r.passed is True
        assert r.cost_usd == 0.12


# ---------------------------------------------------------------------------
# EvalResult
# ---------------------------------------------------------------------------


class TestEvalResult:
    def test_default_result(self) -> None:
        r = EvalResult(score=0.75)
        assert r.score == 0.75
        assert r.tier == "smoke"
        assert r.tasks_evaluated == 0
        assert r.multiplicative_components is None
        assert r.per_tier is None
        assert r.failures == []
        assert r.task_results == []

    def test_to_dict_minimal(self) -> None:
        r = EvalResult(score=0.5, tier="standard", tasks_evaluated=10)
        d = r.to_dict()
        assert d["score"] == 0.5
        assert d["tier"] == "standard"
        assert d["tasks_evaluated"] == 10
        assert "multiplicative_components" not in d
        assert "per_tier" not in d
        assert "failures" not in d

    def test_to_dict_with_multiplicative(self) -> None:
        mc = EvalScoreComponents(
            task_success=0.85,
            code_quality=0.78,
            efficiency=0.90,
            reliability=1.0,
            safety=1.0,
        )
        r = EvalResult(
            score=mc.final_score,
            tier="full",
            tasks_evaluated=20,
            multiplicative_components=mc,
        )
        d = r.to_dict()
        assert "multiplicative_components" in d
        assert d["multiplicative_components"]["task_success"] == 0.85
        assert d["multiplicative_components"]["reliability"] == 1.0

    def test_to_dict_with_per_tier(self) -> None:
        pt = TierScores(smoke=1.0, standard=0.8, stretch=0.6, adversarial=0.4)
        r = EvalResult(score=0.7, per_tier=pt)
        d = r.to_dict()
        assert d["per_tier"]["smoke"] == 1.0
        assert d["per_tier"]["adversarial"] == 0.4

    def test_to_dict_with_failures(self) -> None:
        failures = [
            FailureRecord(
                task_id="t1",
                category=FailureCategory.CONTEXT_MISS,
                details="Missing context",
            ),
        ]
        r = EvalResult(score=0.5, failures=failures)
        d = r.to_dict()
        assert len(d["failures"]) == 1
        assert d["failures"][0]["taxonomy"] == "context_miss"
        assert d["failures"][0]["task"] == "t1"

    def test_to_dict_with_cost(self) -> None:
        r = EvalResult(score=0.8, cost_total=2.34)
        d = r.to_dict()
        assert d["cost_total"] == 2.34

    def test_to_dict_zero_cost_omitted(self) -> None:
        r = EvalResult(score=0.8, cost_total=0.0)
        d = r.to_dict()
        assert "cost_total" not in d


# ---------------------------------------------------------------------------
# EvalHarness — init and golden loading
# ---------------------------------------------------------------------------


class TestEvalHarnessInit:
    def test_init_defaults(self, tmp_path: Path) -> None:
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()
        h = EvalHarness(state_dir)
        assert h._state_dir == state_dir
        assert h._repo_root == tmp_path
        assert h._golden_dir == state_dir / "eval" / "golden"
        assert h._runs_dir == state_dir / "eval" / "runs"

    def test_init_custom_dirs(self, tmp_path: Path) -> None:
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()
        golden = tmp_path / "my_golden"
        runs = tmp_path / "my_runs"
        h = EvalHarness(state_dir, golden_dir=golden, runs_dir=runs)
        assert h._golden_dir == golden
        assert h._runs_dir == runs


class TestLoadGoldenTasks:
    def test_load_from_empty_dir(self, tmp_path: Path) -> None:
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()
        golden = state_dir / "eval" / "golden"
        golden.mkdir(parents=True)
        h = EvalHarness(state_dir)
        tasks = h.load_golden_tasks()
        assert tasks == []

    def test_load_smoke_tasks(self, tmp_path: Path) -> None:
        state_dir = tmp_path / ".sdd"
        golden = state_dir / "eval" / "golden" / "smoke"
        golden.mkdir(parents=True)
        (golden / "001-test.md").write_text(
            "---\nid: smoke-001\ntitle: Add docstring\nrole: backend\n---\nAdd a docstring.\n"
        )
        h = EvalHarness(state_dir)
        tasks = h.load_golden_tasks(tier_filter="smoke")
        assert len(tasks) == 1
        assert tasks[0].id == "smoke-001"
        assert tasks[0].tier == "smoke"


# ---------------------------------------------------------------------------
# EvalHarness — evaluate_task
# ---------------------------------------------------------------------------


class TestEvaluateTask:
    def _make_golden(self, *, task_id: str = "gt-001", tier: str = "smoke") -> GoldenTask:
        return GoldenTask(
            id=task_id,
            tier=tier,
            title="Test task",
            description="Do something.",
            max_duration_s=300,
        )

    def test_passing_task(self, tmp_path: Path) -> None:
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()
        h = EvalHarness(state_dir)
        task = self._make_golden()

        telemetry = {
            "task_id": "gt-001",
            "completion_signals_checked": 2,
            "completion_signals_passed": 2,
            "tests_failed": 0,
            "duration_s": 60.0,
            "cost_usd": 0.10,
        }
        result = h.evaluate_task(task, telemetry_raw=telemetry)
        assert result.passed is True
        assert result.failure is None
        assert result.telemetry is not None
        assert result.telemetry.completion_signals_passed == 2

    def test_failing_task_incomplete_signals(self, tmp_path: Path) -> None:
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()
        h = EvalHarness(state_dir)
        task = self._make_golden()

        telemetry = {
            "task_id": "gt-001",
            "completion_signals_checked": 3,
            "completion_signals_passed": 1,
            "tests_failed": 0,
        }
        result = h.evaluate_task(task, telemetry_raw=telemetry)
        assert result.passed is False
        assert result.failure is not None
        assert result.failure.category == FailureCategory.INCOMPLETE

    def test_failing_taFAKE_STRIPE_TEST_KEY_REPLACED(self, tmp_path: Path) -> None:
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()
        h = EvalHarness(state_dir)
        task = self._make_golden()

        telemetry = {
            "task_id": "gt-001",
            "completion_signals_checked": 2,
            "completion_signals_passed": 2,
            "tests_failed": 1,
        }
        result = h.evaluate_task(task, telemetry_raw=telemetry)
        assert result.passed is False
        assert result.failure is not None
        assert result.failure.category == FailureCategory.TEST_REGRESSION

    def test_failing_task_timeout(self, tmp_path: Path) -> None:
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()
        h = EvalHarness(state_dir)
        task = self._make_golden(task_id="gt-timeout")

        telemetry = {
            "task_id": "gt-timeout",
            "completion_signals_checked": 1,
            "completion_signals_passed": 0,
            "tests_failed": 0,
            "duration_s": 600.0,
        }
        result = h.evaluate_task(task, telemetry_raw=telemetry)
        assert result.passed is False
        assert result.failure is not None
        assert result.failure.category == FailureCategory.TIMEOUT

    def test_failing_task_scope_creep(self, tmp_path: Path) -> None:
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()
        h = EvalHarness(state_dir)
        task = GoldenTask(
            id="gt-scope",
            tier="smoke",
            title="Scoped task",
            description="Only modify allowed files.",
            owned_files=["src/foo.py"],
            max_duration_s=300,
        )
        telemetry = {
            "task_id": "gt-scope",
            "completion_signals_checked": 1,
            "completion_signals_passed": 0,
            "tests_failed": 0,
            "files_modified": ["src/foo.py", "src/bar.py"],
        }
        result = h.evaluate_task(task, telemetry_raw=telemetry)
        assert result.passed is False
        assert result.failure is not None
        assert result.failure.category == FailureCategory.SCOPE_CREEP

    def test_no_telemetry(self, tmp_path: Path) -> None:
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()
        h = EvalHarness(state_dir)
        task = self._make_golden()
        result = h.evaluate_task(task)
        assert result.passed is False
        assert result.telemetry is not None
        assert result.telemetry.task_id == "gt-001"

    def test_with_judge_verdict(self, tmp_path: Path) -> None:
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()
        h = EvalHarness(state_dir)
        task = self._make_golden()

        telemetry = {
            "task_id": "gt-001",
            "completion_signals_checked": 1,
            "completion_signals_passed": 1,
            "tests_failed": 0,
        }
        verdict = JudgeVerdict(correctness=4, style=3, test_coverage=4, safety=5, verdict="PASS")
        result = h.evaluate_task(task, telemetry_raw=telemetry, judge_verdict=verdict)
        assert result.passed is True
        assert result.judge_verdict is verdict


# ---------------------------------------------------------------------------
# EvalHarness — compute_multiplicative_score
# ---------------------------------------------------------------------------


class TestMultiplicativeScoring:
    def _make_passing_result(self, task_id: str = "t1", tier: str = "smoke", cost: float = 0.10) -> TaskEvalResult:
        return TaskEvalResult(
            task_id=task_id,
            tier=tier,
            passed=True,
            cost_usd=cost,
            telemetry=AgentTelemetry(
                task_id=task_id,
                duration_s=60.0,
                cost_usd=cost,
                completion_signals_checked=1,
                completion_signals_passed=1,
            ),
            judge_verdict=JudgeVerdict(correctness=4, style=4, test_coverage=4, safety=4, verdict="PASS"),
        )

    def _make_failing_result(self, task_id: str = "f1", tier: str = "smoke") -> TaskEvalResult:
        return TaskEvalResult(
            task_id=task_id,
            tier=tier,
            passed=False,
            telemetry=AgentTelemetry(task_id=task_id, duration_s=30.0),
        )

    def test_all_pass(self, tmp_path: Path) -> None:
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()
        h = EvalHarness(state_dir)
        results = [self._make_passing_result(f"t{i}") for i in range(5)]
        run = h.compute_multiplicative_score(results)
        assert run.score > 0
        assert run.multiplicative_components is not None
        assert run.multiplicative_components.task_success == 1.0
        assert run.multiplicative_components.reliability == 1.0
        assert run.multiplicative_components.safety == 1.0
        assert run.tasks_evaluated == 5

    def test_empty_results(self, tmp_path: Path) -> None:
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()
        h = EvalHarness(state_dir)
        run = h.compute_multiplicative_score([])
        assert run.score == 0.0
        assert run.tasks_evaluated == 0

    def test_mixed_pass_fail(self, tmp_path: Path) -> None:
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()
        h = EvalHarness(state_dir)
        results = [
            self._make_passing_result("t1"),
            self._make_failing_result("t2"),
        ]
        run = h.compute_multiplicative_score(results)
        assert run.multiplicative_components is not None
        assert run.multiplicative_components.task_success == 0.5

    def test_crashes_degrade_reliability(self, tmp_path: Path) -> None:
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()
        h = EvalHarness(state_dir)
        results = [self._make_passing_result()]
        run = h.compute_multiplicative_score(results, crash_count=5)
        assert run.multiplicative_components is not None
        assert run.multiplicative_components.reliability < 1.0

    def test_test_regression_zeros_safety(self, tmp_path: Path) -> None:
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()
        h = EvalHarness(state_dir)

        # Add a test regression failure to the taxonomy
        failing = TaskEvalResult(
            task_id="f1",
            tier="smoke",
            passed=False,
            failure=FailureRecord(
                task_id="f1",
                category=FailureCategory.TEST_REGRESSION,
                details="Broke tests",
            ),
        )
        h.taxonomy.add(failing.failure)  # type: ignore[arg-type]

        results = [self._make_passing_result(), failing]
        run = h.compute_multiplicative_score(results)
        assert run.multiplicative_components is not None
        assert run.multiplicative_components.safety == 0.0
        assert run.score == 0.0

    def test_per_tier_scores(self, tmp_path: Path) -> None:
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()
        h = EvalHarness(state_dir)
        results = [
            self._make_passing_result("s1", tier="smoke"),
            self._make_passing_result("s2", tier="smoke"),
            self._make_failing_result("st1", tier="standard"),
        ]
        run = h.compute_multiplicative_score(results)
        assert run.per_tier is not None
        assert run.per_tier.smoke == 1.0
        assert run.per_tier.standard == 0.0

    def test_cost_total(self, tmp_path: Path) -> None:
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()
        h = EvalHarness(state_dir)
        results = [
            self._make_passing_result("t1", cost=0.50),
            self._make_passing_result("t2", cost=0.30),
        ]
        run = h.compute_multiplicative_score(results)
        assert abs(run.cost_total - 0.80) < 0.01


# ---------------------------------------------------------------------------
# EvalHarness — persistence
# ---------------------------------------------------------------------------


class TestPersistence:
    def test_save_and_load_run(self, tmp_path: Path) -> None:
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()
        h = EvalHarness(state_dir)

        mc = EvalScoreComponents(
            task_success=0.9,
            code_quality=0.8,
            efficiency=0.85,
            reliability=1.0,
            safety=1.0,
        )
        pt = TierScores(smoke=1.0, standard=0.8, stretch=0.6, adversarial=0.4)
        result = EvalResult(
            score=mc.final_score,
            tier="full",
            tasks_evaluated=20,
            multiplicative_components=mc,
            per_tier=pt,
            cost_total=2.50,
        )

        path = h.save_run(result)
        assert path.exists()

        data = json.loads(path.read_text())
        assert data["tasks_evaluated"] == 20
        assert "multiplicative_components" in data
        assert data["per_tier"]["smoke"] == 1.0

    def test_load_previous_run(self, tmp_path: Path) -> None:
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()
        h = EvalHarness(state_dir)

        result = EvalResult(score=0.72, tier="full", tasks_evaluated=15)
        h.save_run(result)

        prev = h.load_previous_run()
        assert prev is not None
        assert prev.score == 0.72
        assert prev.tasks_evaluated == 15

    def test_load_previous_run_empty(self, tmp_path: Path) -> None:
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()
        h = EvalHarness(state_dir)
        assert h.load_previous_run() is None

    def test_load_previous_run_with_components(self, tmp_path: Path) -> None:
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()
        h = EvalHarness(state_dir)

        mc = EvalScoreComponents(
            task_success=0.85,
            code_quality=0.78,
            efficiency=0.90,
            reliability=1.0,
            safety=1.0,
        )
        pt = TierScores(smoke=1.0, standard=0.7, stretch=0.5, adversarial=0.3)
        result = EvalResult(
            score=mc.final_score,
            tier="full",
            tasks_evaluated=25,
            multiplicative_components=mc,
            per_tier=pt,
            cost_total=3.00,
        )
        h.save_run(result)

        prev = h.load_previous_run()
        assert prev is not None
        assert prev.multiplicative_components is not None
        assert prev.multiplicative_components.task_success == 0.85
        assert prev.per_tier is not None
        assert prev.per_tier.smoke == 1.0


# ---------------------------------------------------------------------------
# Taxonomy access
# ---------------------------------------------------------------------------


class TestTaxonomyAccess:
    def test_taxonomy_property(self, tmp_path: Path) -> None:
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()
        h = EvalHarness(state_dir)
        assert isinstance(h.taxonomy, FailureTaxonomy)
        assert h.taxonomy.total == 0

    def test_taxonomy_accumulates(self, tmp_path: Path) -> None:
        state_dir = tmp_path / ".sdd"
        state_dir.mkdir()
        h = EvalHarness(state_dir)

        task = GoldenTask(id="acc-1", tier="smoke", title="Test", description="Test")
        telemetry = {
            "task_id": "acc-1",
            "completion_signals_checked": 1,
            "completion_signals_passed": 0,
            "tests_failed": 0,
        }
        h.evaluate_task(task, telemetry_raw=telemetry)
        assert h.taxonomy.total == 1


# ---------------------------------------------------------------------------
# Taxonomy classification — classify_failure priority ordering
# ---------------------------------------------------------------------------


class TestClassifyFailure:
    def test_test_regression_highest_priority(self) -> None:
        rec = classify_failure(task_id="t1", tests_regressed=True, timed_out=True, scope_violated=True)
        assert rec.category == FailureCategory.TEST_REGRESSION
        assert rec.severity == "critical"

    def test_timeout(self) -> None:
        rec = classify_failure(task_id="t2", timed_out=True)
        assert rec.category == FailureCategory.TIMEOUT
        assert rec.severity == "high"

    def test_scope_creep(self) -> None:
        rec = classify_failure(task_id="t3", scope_violated=True, files_involved=["extra.py"])
        assert rec.category == FailureCategory.SCOPE_CREEP
        assert rec.files_involved == ["extra.py"]

    def test_conflict(self) -> None:
        rec = classify_failure(task_id="t4", conflict_detected=True)
        assert rec.category == FailureCategory.CONFLICT

    def test_hallucination(self) -> None:
        rec = classify_failure(task_id="t5", compile_error=True)
        assert rec.category == FailureCategory.HALLUCINATION

    def test_orientation_miss(self) -> None:
        rec = classify_failure(task_id="t6", orientation_ratio=0.75)
        assert rec.category == FailureCategory.ORIENTATION_MISS
        assert "75%" in rec.details

    def test_orientation_miss_boundary(self) -> None:
        rec = classify_failure(task_id="t6b", orientation_ratio=0.5)
        assert rec.category != FailureCategory.ORIENTATION_MISS

    def test_incomplete(self) -> None:
        rec = classify_failure(task_id="t7", signals_incomplete=True)
        assert rec.category == FailureCategory.INCOMPLETE

    def test_context_miss_default(self) -> None:
        rec = classify_failure(task_id="t8")
        assert rec.category == FailureCategory.CONTEXT_MISS
        assert rec.severity == "medium"

    def test_custom_details(self) -> None:
        rec = classify_failure(task_id="t9", timed_out=True, details="Ran for 10 minutes")
        assert rec.details == "Ran for 10 minutes"

    def test_priority_timeout_over_scope(self) -> None:
        rec = classify_failure(task_id="t10", timed_out=True, scope_violated=True)
        assert rec.category == FailureCategory.TIMEOUT

    def test_priority_scope_over_hallucination(self) -> None:
        rec = classify_failure(task_id="t11", scope_violated=True, compile_error=True)
        assert rec.category == FailureCategory.SCOPE_CREEP


# ---------------------------------------------------------------------------
# Taxonomy drift tracking
# ---------------------------------------------------------------------------


class TestTaxonomyDrift:
    def test_drift_detects_new_failures(self) -> None:
        prev = FailureTaxonomy()
        curr = FailureTaxonomy()
        curr.add(FailureRecord(task_id="t1", category=FailureCategory.TIMEOUT))
        deltas = curr.drift(prev)
        assert deltas == {"timeout": 1}

    def test_drift_detects_improvements(self) -> None:
        prev = FailureTaxonomy()
        prev.add(FailureRecord(task_id="t1", category=FailureCategory.SCOPE_CREEP))
        prev.add(FailureRecord(task_id="t2", category=FailureCategory.SCOPE_CREEP))
        curr = FailureTaxonomy()
        deltas = curr.drift(prev)
        assert deltas == {"scope_creep": -2}

    def test_drift_empty_both(self) -> None:
        assert FailureTaxonomy().drift(FailureTaxonomy()) == {}

    def test_drift_mixed_changes(self) -> None:
        prev = FailureTaxonomy()
        prev.add(FailureRecord(task_id="t1", category=FailureCategory.TIMEOUT))
        prev.add(FailureRecord(task_id="t2", category=FailureCategory.TIMEOUT))
        curr = FailureTaxonomy()
        curr.add(FailureRecord(task_id="t3", category=FailureCategory.TIMEOUT))
        curr.add(FailureRecord(task_id="t4", category=FailureCategory.HALLUCINATION))
        deltas = curr.drift(prev)
        assert deltas["timeout"] == -1
        assert deltas["hallucination"] == 1
