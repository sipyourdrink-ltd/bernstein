"""TDD test suite for Issue #5567: bench abstention scoring, lambda penalty, and confident-error rate.

Acceptance criteria:
- test_abstained_instance_scores_above_a_wrong_one
- test_resolve_rate_excludes_abstentions_from_the_denominator
- test_confident_error_rate_counts_only_wrong_attempts
- test_bundle_records_lambda_and_the_three_rates
- test_compare_ranks_by_expected_value_and_prints_resolve_rate
"""

from __future__ import annotations

from pathlib import Path

import pytest
from benchmarks.swe_bench.metrics import InstanceResult, aggregate
from click.testing import CliRunner

from bernstein.eval.bench.bench_cli import bench_group
from bernstein.eval.bench.bundle import SubmissionBundle, TaskResult


def test_abstained_instance_scores_above_a_wrong_one() -> None:
    """An abstained instance gets score 0.0, which is higher than a wrong attempt (-lambda = -1.0)."""
    # 1. Test via aggregate metrics
    results_resolved = [
        InstanceResult(
            instance_id="inst_1",
            scenario_name="sc_test",
            status="resolved",
            resolved=True,
            wall_time_s=10.0,
            total_tokens=100,
            total_cost_usd=0.01,
        )
    ]
    results_abstained = [
        InstanceResult(
            instance_id="inst_2",
            scenario_name="sc_test",
            status="abstained",
            resolved=False,
            wall_time_s=2.0,
            total_tokens=20,
            total_cost_usd=0.002,
        )
    ]
    results_wrong = [
        InstanceResult(
            instance_id="inst_3",
            scenario_name="sc_test",
            status="failed",
            resolved=False,
            wall_time_s=15.0,
            total_tokens=150,
            total_cost_usd=0.02,
        )
    ]

    summary_resolved = aggregate(results_resolved)
    summary_abstained = aggregate(results_abstained)
    summary_wrong = aggregate(results_wrong)

    assert summary_resolved.expected_value == 1.0
    assert summary_abstained.expected_value == 0.0
    assert summary_wrong.expected_value == -1.0
    assert summary_abstained.expected_value > summary_wrong.expected_value

    # 2. Test via SubmissionBundle task scoring
    r_res = TaskResult(
        task_id="t1",
        task_hash="h1",
        receipt={"run_id": "1"},
        passed=True,
        score=1.0,
        abstained=False,
    )
    r_abs = TaskResult(
        task_id="t2",
        task_hash="h2",
        receipt={"run_id": "2"},
        passed=False,
        score=0.0,
        abstained=True,
        abstention_reason="Cannot verify proof offline",
    )
    r_fail = TaskResult(
        task_id="t3",
        task_hash="h3",
        receipt={"run_id": "3"},
        passed=False,
        score=0.0,
        abstained=False,
    )

    bundle_res = SubmissionBundle(
        suite_hash="sh1",
        suite_version="v1",
        task_results=[r_res],
        scheduler_config={},
        lambda_penalty=1.0,
    )
    bundle_abs = SubmissionBundle(
        suite_hash="sh1",
        suite_version="v1",
        task_results=[r_abs],
        scheduler_config={},
        lambda_penalty=1.0,
    )
    bundle_fail = SubmissionBundle(
        suite_hash="sh1",
        suite_version="v1",
        task_results=[r_fail],
        scheduler_config={},
        lambda_penalty=1.0,
    )

    assert bundle_res.expected_value == 1.0
    assert bundle_abs.expected_value > bundle_fail.expected_value
    assert bundle_abs.expected_value == 0.0
    assert bundle_fail.expected_value == -1.0


def test_resolve_rate_excludes_abstentions_from_the_denominator() -> None:
    """Resolve rate is resolved / (total - skipped - abstained), preserving backward compatibility."""
    # 10 tasks: 4 resolved, 2 abstained, 2 failed, 2 skipped
    results: list[InstanceResult] = []
    for i in range(4):
        results.append(
            InstanceResult(
                instance_id=f"r_{i}",
                scenario_name="sc1",
                status="resolved",
                resolved=True,
                wall_time_s=1.0,
                total_tokens=10,
                total_cost_usd=0.0,
            )
        )
    for i in range(2):
        results.append(
            InstanceResult(
                instance_id=f"a_{i}",
                scenario_name="sc1",
                status="abstained",
                resolved=False,
                wall_time_s=1.0,
                total_tokens=10,
                total_cost_usd=0.0,
            )
        )
    for i in range(2):
        results.append(
            InstanceResult(
                instance_id=f"f_{i}",
                scenario_name="sc1",
                status="failed",
                resolved=False,
                wall_time_s=1.0,
                total_tokens=10,
                total_cost_usd=0.0,
            )
        )
    for i in range(2):
        results.append(
            InstanceResult(
                instance_id=f"s_{i}",
                scenario_name="sc1",
                status="skipped",
                resolved=False,
                wall_time_s=1.0,
                total_tokens=10,
                total_cost_usd=0.0,
            )
        )

    summary = aggregate(results)
    # total=10, skipped=2, abstained=2, attempted = 10 - 2 - 2 = 6
    # resolved = 4 -> resolve_rate = 4 / 6 = 2/3 ≈ 0.6667
    assert summary.total_instances == 10
    assert summary.skipped == 2
    assert summary.abstained == 2
    assert summary.resolved == 4
    assert summary.failed == 2
    assert summary.attempted_instances == 6
    assert pytest.approx(summary.resolve_rate, 0.001) == 4 / 6
    assert pytest.approx(summary.abstain_rate, 0.001) == 2 / 8  # 2 abstained out of 8 non-skipped


def test_confident_error_rate_counts_only_wrong_attempts() -> None:
    """Confident-error rate = wrong / (wrong + resolved), ignoring abstentions and skips."""
    # 3 resolved, 1 failed, 1 error, 5 abstained
    results: list[InstanceResult] = []
    for i in range(3):
        results.append(
            InstanceResult(
                instance_id=f"r_{i}",
                scenario_name="sc1",
                status="resolved",
                resolved=True,
                wall_time_s=1.0,
                total_tokens=10,
                total_cost_usd=0.0,
            )
        )
    results.append(
        InstanceResult(
            instance_id="f_0",
            scenario_name="sc1",
            status="failed",
            resolved=False,
            wall_time_s=1.0,
            total_tokens=10,
            total_cost_usd=0.0,
        )
    )
    results.append(
        InstanceResult(
            instance_id="e_0",
            scenario_name="sc1",
            status="error",
            resolved=False,
            wall_time_s=1.0,
            total_tokens=10,
            total_cost_usd=0.0,
        )
    )
    for i in range(5):
        results.append(
            InstanceResult(
                instance_id=f"a_{i}",
                scenario_name="sc1",
                status="abstained",
                resolved=False,
                wall_time_s=1.0,
                total_tokens=10,
                total_cost_usd=0.0,
            )
        )

    summary = aggregate(results)
    # wrong = 1 failed + 1 error = 2
    # resolved = 3
    # confident_error_rate = 2 / (2 + 3) = 2/5 = 0.4
    assert summary.resolved == 3
    assert summary.failed == 1
    assert summary.errors == 1
    assert summary.abstained == 5
    assert pytest.approx(summary.confident_error_rate, 0.001) == 0.4
    assert pytest.approx(summary.resolve_rate, 0.001) == 3 / 5  # 3 / (10 - 5) = 3/5 = 0.6


def test_bundle_records_lambda_and_the_three_rates(tmp_path: Path) -> None:
    """SubmissionBundle serializes lambda_penalty, resolve_rate, abstain_rate, confident_error_rate, and Brier score."""
    task_results = [
        TaskResult(
            task_id="t1",
            task_hash="h1",
            receipt={"id": 1},
            passed=True,
            score=1.0,
            abstained=False,
            confidence=0.9,
        ),
        TaskResult(
            task_id="t2",
            task_hash="h2",
            receipt={"id": 2},
            passed=False,
            score=0.0,
            abstained=True,
            abstention_reason="Declined: test incomplete",
            confidence=0.1,
        ),
        TaskResult(
            task_id="t3",
            task_hash="h3",
            receipt={"id": 3},
            passed=False,
            score=0.0,
            abstained=False,
            confidence=0.8,
        ),
    ]

    bundle = SubmissionBundle(
        suite_hash="suite_hash_123",
        suite_version="golden-v1",
        task_results=task_results,
        scheduler_config={"scheduler": "default"},
        lambda_penalty=1.5,
    )

    assert bundle.lambda_penalty == 1.5
    assert bundle.abstained_count == 1
    assert pytest.approx(bundle.resolve_rate, 0.001) == 1 / 2  # 1 resolved / 2 attempted (t1, t3)
    assert pytest.approx(bundle.abstain_rate, 0.001) == 1 / 3  # 1 abstained / 3 tasks
    assert pytest.approx(bundle.confident_error_rate, 0.001) == 1 / 2  # 1 wrong (t3) / (1 wrong + 1 resolved)
    # Expected value = (1 * 1.0 + 1 * 0.0 + 1 * -1.5) / 3 = -0.5 / 3 = -0.1667
    assert pytest.approx(bundle.expected_value, 0.001) == -0.5 / 3

    # Round trip serialization
    bundle_path = tmp_path / "bundle.json"
    bundle.save(bundle_path)

    loaded = SubmissionBundle.load(bundle_path)
    assert loaded.lambda_penalty == 1.5
    assert loaded.abstained_count == 1
    assert pytest.approx(loaded.resolve_rate, 0.001) == bundle.resolve_rate
    assert pytest.approx(loaded.abstain_rate, 0.001) == bundle.abstain_rate
    assert pytest.approx(loaded.confident_error_rate, 0.001) == bundle.confident_error_rate
    assert pytest.approx(loaded.expected_value, 0.001) == bundle.expected_value
    assert loaded.brier_score >= 0.0

    # Ensure to_dict contains new fields
    d = bundle.to_dict()
    assert d["lambda_penalty"] == 1.5
    assert "resolve_rate" in d
    assert "abstain_rate" in d
    assert "confident_error_rate" in d
    assert "expected_value" in d
    assert "brier_score" in d


def test_compare_ranks_by_expected_value_and_prints_resolve_rate(tmp_path: Path) -> None:
    """bernstein bench compare ranks two bundles by expected value under lambda and displays rates."""
    tasks_a = [
        TaskResult(task_id=f"t{i}", task_hash=f"h{i}", receipt={"i": i}, passed=True, score=1.0) for i in range(4)
    ] + [TaskResult(task_id="t4", task_hash="h4", receipt={"i": 4}, passed=False, score=0.0)]

    tasks_b = [
        TaskResult(task_id=f"t{i}", task_hash=f"h{i}", receipt={"i": i}, passed=True, score=1.0) for i in range(3)
    ] + [
        TaskResult(
            task_id=f"t{i}",
            task_hash=f"h{i}",
            receipt={"i": i},
            passed=False,
            score=0.0,
            abstained=True,
        )
        for i in range(3, 5)
    ]

    bundle_a = SubmissionBundle(
        suite_hash="same_suite_hash",
        suite_version="golden-v1",
        task_results=tasks_a,
        scheduler_config={"scheduler": "model_a"},
        lambda_penalty=2.0,
    )
    bundle_b = SubmissionBundle(
        suite_hash="same_suite_hash",
        suite_version="golden-v1",
        task_results=tasks_b,
        scheduler_config={"scheduler": "model_a"},
        lambda_penalty=2.0,
    )

    path_a = tmp_path / "bundle_a.json"
    path_b = tmp_path / "bundle_b.json"
    bundle_a.save(path_a)
    bundle_b.save(path_b)

    runner = CliRunner()
    result = runner.invoke(bench_group, ["compare", str(path_a), str(path_b), "--penalty", "2.0"])
    assert result.exit_code == 0
    output = result.output
    assert "1. bundle_b.json" in output
    assert "2. bundle_a.json" in output
    assert "resolve rate" in output.lower()
    assert "abstain rate" in output.lower()
    assert "confident error" in output.lower()


class TestCompareCommandKeepsBothSurfaces:
    """`bench compare` grew a second definition and lost the first (#5567/#5568).

    Both the expected-value options from #5567 and the harness-fingerprint
    guard from #5568 register the ``compare`` subcommand. Click's
    ``add_command`` is a dict assignment, so a second registration under the
    same name silently replaces the first -- dropping whichever landed
    earlier, with no error at import or at run time. These pin that one
    command carries both surfaces.
    """

    def test_compare_is_registered_exactly_once(self) -> None:
        from bernstein.eval.bench import bench_cli

        source = Path(bench_cli.__file__).read_text(encoding="utf-8")
        assert source.count('@bench_group.command(name="compare")') == 1
        assert source.count("def bench_compare(") == 1

    def test_compare_exposes_both_option_families(self) -> None:
        from bernstein.eval.bench.bench_cli import bench_group

        compare = bench_group.commands["compare"]
        options = {opt for param in compare.params for opt in getattr(param, "opts", [])}

        # #5568: the harness-drift guard must survive.
        assert "--allow-harness-drift" in options
        # #5567: the expected-value penalty must be reachable.
        assert "--penalty" in options
        assert "--lambda" in options


class TestLambdaPenaltyIsCoveredByTheBundleHash:
    """lambda reorders `bench compare`, so a bundle must commit to it (#5567)."""

    @staticmethod
    def _bundle(**over: object) -> SubmissionBundle:
        kwargs: dict[str, object] = {
            "suite_hash": "suite-hash",
            "suite_version": "1.0.0",
            "submitted_at": "2026-01-01T00:00:00Z",
            "scheduler_config": {"parallelism": 1},
            "task_results": [],
        }
        kwargs.update(over)
        return SubmissionBundle(**kwargs)  # type: ignore[arg-type]

    def test_a_non_default_lambda_changes_the_hash(self) -> None:
        default = self._bundle()
        penalised = self._bundle()
        penalised.lambda_penalty = 2.0
        penalised._bundle_hash = None

        assert penalised.bundle_hash() != default.bundle_hash()

    def test_the_default_lambda_leaves_published_hashes_untouched(self) -> None:
        """A bundle written before the field existed must keep its hash."""
        default = self._bundle()
        explicit = self._bundle()
        explicit.lambda_penalty = 1.0
        explicit._bundle_hash = None

        assert explicit.bundle_hash() == default.bundle_hash()


class TestSkippedTasksLeaveEveryDenominator:
    """`bundle.py` must agree with `metrics.py` on skipped tasks (#5567 review).

    `benchmarks/swe_bench/metrics.py` excludes `status == "skipped"` from every
    denominator (`non_skipped = total - skipped`). `SubmissionBundle` had no
    notion of a skipped task at all, so one was scored as a wrong answer by
    `wrong_count` and inflated the denominators of `resolve_rate`,
    `expected_value` and `brier_score`.
    """

    @staticmethod
    def _r(task_id: str, *, passed: bool = False, abstained: bool = False, skipped: bool = False) -> TaskResult:
        return TaskResult(
            task_id=task_id,
            task_hash="h",
            receipt={},
            passed=passed,
            score=1.0 if passed else 0.0,
            abstained=abstained,
            skipped=skipped,
        )

    def _bundle(self, results: list[TaskResult]) -> SubmissionBundle:
        return SubmissionBundle(
            suite_hash="suite-hash",
            suite_version="1.0.0",
            submitted_at="2026-01-01T00:00:00Z",
            scheduler_config={"parallelism": 1},
            task_results=results,
        )

    def test_a_skipped_task_is_not_a_wrong_answer(self) -> None:
        bundle = self._bundle([self._r("a", passed=True), self._r("b", skipped=True)])

        assert bundle.wrong_count == 0
        assert bundle.skipped_count == 1
        assert bundle.evaluated_count == 1

    def test_skipped_tasks_leave_every_denominator(self) -> None:
        bundle = self._bundle(
            [
                self._r("a", passed=True),
                self._r("b"),
                self._r("c", abstained=True),
                self._r("d", skipped=True),
            ]
        )

        assert bundle.attempted_count == 2       # 3 evaluated - 1 abstained
        assert bundle.resolve_rate == 0.5        # 1 resolved / 2 attempted
        assert abs(bundle.abstain_rate - 1 / 3) < 1e-9
        assert bundle.expected_value == 0.0      # (1 - 1) / 3 evaluated

    def test_it_agrees_with_the_swe_bench_aggregator(self) -> None:
        """The two scorers must not disagree about the same run."""
        bundle = self._bundle(
            [
                self._r("a", passed=True),
                self._r("b"),
                self._r("c", abstained=True),
                self._r("d", skipped=True),
            ]
        )
        summary = aggregate(
            [
                InstanceResult(
                    instance_id=i,
                    scenario_name="s",
                    status=status,
                    resolved=(status == "resolved"),
                    wall_time_s=1.0,
                    total_tokens=1,
                    total_cost_usd=0.0,
                )
                for i, status in (("a", "resolved"), ("b", "failed"), ("c", "abstained"), ("d", "skipped"))
            ]
        )

        assert bundle.resolve_rate == summary.resolve_rate
        assert abs(bundle.abstain_rate - summary.abstain_rate) < 1e-9
        assert abs(bundle.expected_value - summary.expected_value) < 1e-9

    def test_an_entirely_skipped_bundle_does_not_divide_by_zero(self) -> None:
        bundle = self._bundle([self._r("a", skipped=True), self._r("b", skipped=True)])

        assert bundle.evaluated_count == 0
        assert bundle.resolve_rate == 0.0
        assert bundle.expected_value == 0.0
        assert bundle.brier_score == 0.0

    def test_a_bundle_with_nothing_skipped_keeps_the_hash_it_had(self) -> None:
        """`skipped` is omitted from `to_dict` when False, as `lambda_penalty` is."""
        result = self._r("a", passed=True)

        assert "skipped" not in result.to_dict()
