"""
Unit tests for benchmark bundle cost accounting, comparison, and budget gate (#5464).

Acceptance Criteria:
1. Per-task tokens, cost and wall time in TaskResult and SubmissionBundle with schema version.
2. bernstein bench compare reports cost deltas next to pass-rate deltas.
3. --budget stops the run when exceeded and writes a refusal receipt.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import click
import pytest
from click.testing import CliRunner

from bernstein.eval.bench.bench_cli import bench_group
from bernstein.eval.bench.bundle import (
    BUNDLE_SCHEMA_VERSION,
    REFUSED_STATUS,
    SubmissionBundle,
    TaskResult,
    harness_fingerprint,
)
from bernstein.eval.bench.compare import compare_bundles
from bernstein.eval.bench.runner import BenchRunner, MockReplayAdapter
from bernstein.eval.bench.suite import BenchSuite, BenchTask


def _make_sample_bundle(
    *,
    version: str = "golden-v1",
    task_specs: list[dict],
    scheduler_cfg: dict | None = None,
) -> SubmissionBundle:
    task_results = [
        TaskResult(
            task_id=ts["id"],
            task_hash=f"hash_{ts['id']}",
            receipt={"journal_head": "jhead", "spine_head": "shead"},
            passed=ts.get("passed", True),
            score=ts.get("score", 1.0),
            tokens=ts.get("tokens", 100),
            cost_usd=ts.get("cost_usd", 0.01),
            duration_seconds=ts.get("duration_seconds", 1.5),
        )
        for ts in task_specs
    ]
    return SubmissionBundle(
        suite_hash="suite_hash_123",
        suite_version=version,
        task_results=task_results,
        scheduler_config=scheduler_cfg or {"scheduler": "default"},
    )


class TestBundleCostAccounting:
    """Test cost, token, and duration fields in bundle schema."""

    def test_task_result_fields(self) -> None:
        tr = TaskResult(
            task_id="t1",
            task_hash="thash1",
            receipt={"run_id": "r1"},
            passed=True,
            score=1.0,
            tokens=250,
            cost_usd=0.005,
            duration_seconds=2.1,
        )
        assert tr.tokens == 250
        assert tr.cost_usd == 0.005
        assert tr.duration_seconds == 2.1

        d = tr.to_dict()
        assert d["tokens"] == 250
        assert d["cost_usd"] == 0.005
        assert d["duration_seconds"] == 2.1

    def test_submission_bundle_aggregated_cost_metrics(self) -> None:
        bundle = _make_sample_bundle(
            task_specs=[
                {
                    "id": "task_1",
                    "passed": True,
                    "score": 1.0,
                    "tokens": 100,
                    "cost_usd": 0.02,
                    "duration_seconds": 1.0,
                },
                {
                    "id": "task_2",
                    "passed": False,
                    "score": 0.0,
                    "tokens": 200,
                    "cost_usd": 0.03,
                    "duration_seconds": 2.0,
                },
            ]
        )

        assert bundle.total_tokens == 300
        assert bundle.total_cost_usd == pytest.approx(0.05)
        assert bundle.total_duration_seconds == pytest.approx(3.0)

        d = bundle.to_dict()
        assert d["total_tokens"] == 300
        assert d["total_cost_usd"] == pytest.approx(0.05)
        assert d["total_duration_seconds"] == pytest.approx(3.0)

    def test_bundle_save_load_round_trip_preserves_cost_and_hash(self, tmp_path: Path) -> None:
        bundle = _make_sample_bundle(
            task_specs=[
                {
                    "id": "task_1",
                    "passed": True,
                    "score": 1.0,
                    "tokens": 150,
                    "cost_usd": 0.015,
                    "duration_seconds": 1.2,
                },
            ]
        )
        p = tmp_path / "bundle.json"
        bundle.save(p)

        loaded = SubmissionBundle.load(p)
        assert loaded.bundle_hash() == bundle.bundle_hash()
        assert loaded.total_tokens == 150
        assert loaded.total_cost_usd == pytest.approx(0.015)
        assert loaded.task_results[0].tokens == 150
        assert loaded.task_results[0].cost_usd == pytest.approx(0.015)


class TestPreExistingBundlesStillLoad:
    """The cost fields are bound into the bundle hash only when set. A bundle
    written before they existed carries none, and its stored hash was computed
    without them; emitting zeros on reload would fail its own integrity check."""

    def test_a_bundle_written_before_the_cost_fields_existed_loads_hashes_and_verifies(self, tmp_path: Path) -> None:
        """The fixture is the document a pre-#5464 writer produced, not one the
        new ``to_dict()`` produced: no ``tokens``/``cost_usd``/``duration_seconds``
        on the task, no ``total_*`` on the bundle. Only the four digests are
        computed here, by that writer's own (unchanged) rules. ``load`` raises
        on a stored-hash mismatch, so the first assertion is the load itself."""
        from bernstein.eval.bench.verifier import BenchVerifier, VerificationStatus

        def canon(obj: object) -> bytes:
            return json.dumps(obj, sort_keys=True, separators=(",", ":")).encode()

        suite = BenchSuite(
            version="legacy-v1",
            tasks=[BenchTask(id="t1", description="written before #5464", steps=(), assertions=())],
        )
        receipt = {"journal_head": "j" * 64, "spine_head": "s" * 64, "run_id": "legacy-run", "events": []}
        task_row = {
            "task_id": "t1",
            "task_hash": suite.tasks[0].content_hash(),
            "receipt": receipt,
            "receipt_hash": hashlib.sha256(canon(receipt)).hexdigest(),
            "passed": True,
            "score": 1.0,
            "harness_output": {"note": "legacy"},
        }
        payload = {
            "suite_hash": suite.suite_hash,
            "suite_version": "legacy-v1",
            "submitted_at": 1700000000.0,
            "scheduler_config": {"scheduler": "default"},
            "task_results": [task_row],
        }
        legacy = {
            "bundle_hash": hashlib.sha256(canon(payload)).hexdigest(),
            **payload,
            "harness_fingerprint": harness_fingerprint({"scheduler": "default"}),
            "overall_score": 1.0,
            "pass_rate": 1.0,
            "signature": "",
            "signer_fingerprint": "",
        }
        assert not {"total_tokens", "total_cost_usd", "total_duration_seconds"} & legacy.keys()
        assert not {"tokens", "cost_usd", "duration_seconds"} & task_row.keys()
        p = tmp_path / "legacy.json"
        p.write_text(json.dumps(legacy, indent=2, sort_keys=True), encoding="utf-8")

        loaded = SubmissionBundle.load(p)
        assert loaded.bundle_hash() == legacy["bundle_hash"]
        # Re-saving emits no zeros: the task record keeps the legacy key set.
        assert loaded.to_dict()["task_results"][0].keys() == task_row.keys()
        assert loaded.total_cost_usd == 0.0
        result = BenchVerifier(suite=suite, adapter=MockReplayAdapter()).verify(loaded)
        assert result.status is VerificationStatus.MATCH, result.report()

    def test_bundle_with_resource_metrics_binds_them_into_the_hash(self) -> None:
        a = _make_sample_bundle(task_specs=[{"id": "t1", "cost_usd": 0.01}])
        b = _make_sample_bundle(task_specs=[{"id": "t1", "cost_usd": 0.02}])
        assert a.bundle_hash() != b.bundle_hash()


class TestBundleComparison:
    """Test comparing two bundles for score, pass-rate, and cost deltas."""

    def test_compare_bundles_refuses_differing_harness_fingerprints(self) -> None:
        """The public API enforces the same invariant the CLI checks."""
        bundle_a = _make_sample_bundle(task_specs=[], scheduler_cfg={"scheduler": "a"})
        bundle_b = _make_sample_bundle(task_specs=[], scheduler_cfg={"scheduler": "b"})
        with pytest.raises(ValueError, match="differing harness fingerprints"):
            compare_bundles(bundle_a, bundle_b)

    def test_compare_bundles_counts_refusals_by_receipt_status(self) -> None:
        """Refusal detection uses receipt.status, not harness_output.refusal."""
        refused = TaskResult(
            task_id="t1",
            task_hash="h",
            receipt={"status": "refused", "refusal_reason": "budget_exceeded"},
            passed=False,
            score=0.0,
        )
        bundle_a = _make_sample_bundle(task_specs=[])
        bundle_a.task_results = (refused,)
        bundle_b = _make_sample_bundle(task_specs=[])
        cmp = compare_bundles(bundle_a, bundle_b)
        assert cmp.refused_a == 1
        assert cmp.refused_b == 0

    def test_compare_bundles_metrics(self) -> None:
        bundle_a = _make_sample_bundle(
            task_specs=[
                {"id": "t1", "passed": True, "score": 1.0, "tokens": 100, "cost_usd": 0.04, "duration_seconds": 2.0},
                {"id": "t2", "passed": False, "score": 0.0, "tokens": 100, "cost_usd": 0.04, "duration_seconds": 2.0},
            ]
        )
        bundle_b = _make_sample_bundle(
            task_specs=[
                {"id": "t1", "passed": True, "score": 1.0, "tokens": 80, "cost_usd": 0.02, "duration_seconds": 1.0},
                {"id": "t2", "passed": True, "score": 1.0, "tokens": 80, "cost_usd": 0.02, "duration_seconds": 1.0},
            ]
        )

        cmp = compare_bundles(bundle_a, bundle_b)
        assert cmp.pass_rate_delta == pytest.approx(0.5)  # 50% -> 100%
        assert cmp.score_delta == pytest.approx(0.5)
        assert cmp.cost_delta_usd == pytest.approx(-0.04)  # $0.08 -> $0.04
        assert cmp.cost_delta_percent == pytest.approx(-50.0)
        assert cmp.tokens_delta == -40
        assert cmp.duration_delta_seconds == pytest.approx(-2.0)

        md = cmp.to_markdown()
        assert "Cost" in md
        assert "$0.08" in md
        assert "$0.04" in md

    def test_cli_compare_command(self, tmp_path: Path) -> None:
        b1 = _make_sample_bundle(
            task_specs=[
                {"id": "t1", "passed": True, "score": 1.0, "tokens": 100, "cost_usd": 0.05, "duration_seconds": 1.0}
            ]
        )
        b2 = _make_sample_bundle(
            task_specs=[
                {"id": "t1", "passed": True, "score": 1.0, "tokens": 80, "cost_usd": 0.03, "duration_seconds": 0.8}
            ]
        )

        p1 = tmp_path / "b1.json"
        p2 = tmp_path / "b2.json"
        b1.save(p1)
        b2.save(p2)

        runner = CliRunner()
        result = runner.invoke(bench_group, ["compare", str(p1), str(p2)])
        assert result.exit_code == 0, result.output
        # main's ranking output is untouched ...
        assert "Harness fingerprint" in result.output
        assert "1. " in result.output
        # ... and the resource deltas follow it, b relative to a.
        assert "Cost     : $0.0500 -> $0.0300 (-0.0200, -40.0%)" in result.output
        assert "Tokens   : 100 -> 80 (-20)" in result.output

        as_json = runner.invoke(bench_group, ["compare", str(p1), str(p2), "--format", "json"])
        assert as_json.exit_code == 0, as_json.output
        # stdout is the report and nothing else; the harness verdict went to stderr.
        assert json.loads(as_json.stdout)["cost_delta_usd"] == pytest.approx(-0.02)
        assert "Harness fingerprint" in as_json.stderr

        as_md = runner.invoke(bench_group, ["compare", str(p1), str(p2), "--format", "markdown"])
        assert as_md.exit_code == 0, as_md.output
        assert as_md.stdout.startswith("# Benchmark Bundle Comparison")
        assert "| **Cost (USD)** |" in as_md.stdout

    def test_cli_compare_refuses_harness_drift_for_every_format(self, tmp_path: Path) -> None:
        """A cost delta across differing harness settings is as meaningless as a
        score delta; the refusal main's compare makes has to gate the report too."""
        b1 = _make_sample_bundle(task_specs=[{"id": "t1"}], scheduler_cfg={"scheduler": "default"})
        b2 = _make_sample_bundle(task_specs=[{"id": "t1"}], scheduler_cfg={"scheduler": "other"})
        p1, p2 = tmp_path / "b1.json", tmp_path / "b2.json"
        b1.save(p1)
        b2.save(p2)
        for fmt in ("text", "markdown", "json"):
            result = CliRunner().invoke(bench_group, ["compare", str(p1), str(p2), "--format", fmt])
            assert result.exit_code == 1, (fmt, result.output)
            # Text keeps the verdict on stdout; a machine-readable format owns
            # stdout, so the verdict goes to stderr there.
            stream = result.stdout if fmt == "text" else result.stderr
            assert "Refusing to rank" in stream, (fmt, result.output)

    def test_cli_compare_of_bundles_without_metrics_reads_as_before(self, tmp_path: Path) -> None:
        """A pre-#5464 pair (no resource metrics) prints no cost block at all.

        "No metrics" is ``None``, not zero. A bundle that reported ``0``
        tokens has reported a metric and gets the block; this case is about a
        bundle where the harness reported nothing at all.
        """
        unreported = {"id": "t1", "tokens": None, "cost_usd": None, "duration_seconds": None}
        b1 = _make_sample_bundle(task_specs=[dict(unreported)])
        b2 = _make_sample_bundle(task_specs=[dict(unreported)])
        p1, p2 = tmp_path / "b1.json", tmp_path / "b2.json"
        b1.save(p1)
        b2.save(p2)
        result = CliRunner().invoke(bench_group, ["compare", str(p1), str(p2)])
        assert result.exit_code == 0, result.output
        assert "Cost     :" not in result.output

    def test_cost_delta_percent_is_not_a_number_when_a_cost_nothing(self) -> None:
        """$0 -> $0.05 is not a 0.0% change."""
        a = _make_sample_bundle(task_specs=[{"id": "t1", "cost_usd": 0.0, "tokens": 0, "duration_seconds": 0.0}])
        b = _make_sample_bundle(task_specs=[{"id": "t1", "cost_usd": 0.05}])
        cmp = compare_bundles(a, b)
        assert cmp.cost_delta_percent is None
        assert cmp.to_dict()["cost_delta_percent"] is None
        assert "$+0.0500 (n/a)" in cmp.to_markdown()

    def test_compare_reports_how_many_tasks_a_budget_refused(self, tmp_path: Path) -> None:
        """A budget-cut run is cheaper than a complete one only because tasks never ran."""
        from bernstein.eval.bench.golden_suite import build_golden_suite_v1

        suite = build_golden_suite_v1()
        complete = BenchRunner(suite=suite, adapter=MockReplayAdapter(), scheduler_config={"scheduler": "s"}).run()
        cut = BenchRunner(
            suite=suite, adapter=MockReplayAdapter(), scheduler_config={"scheduler": "s"}, budget_usd=0.001
        ).run()
        cmp = compare_bundles(complete, cut)
        assert cmp.refused_a == 0
        assert cmp.refused_b == sum(1 for r in cut.task_results if r.harness_output.get("refusal"))
        assert cmp.refused_b > 0
        assert "| **Refused (budget)** | 0 |" in cmp.to_markdown()

        p1, p2 = tmp_path / "complete.json", tmp_path / "cut.json"
        complete.save(p1)
        cut.save(p2)
        result = CliRunner().invoke(bench_group, ["compare", str(p1), str(p2)])
        assert result.exit_code == 0, result.output
        assert f"Refused  : 0 -> {cmp.refused_b} tasks never ran (budget)" in result.output

    def test_compare_is_registered_once(self) -> None:
        """Click keeps the last registration; a second `compare` would silently
        replace the harness-drift refusal with a command that has none."""
        from bernstein.cli.main import cli

        bench = cli.commands["bench"]
        assert isinstance(bench, click.Group)
        assert list(bench.commands).count("compare") == 1
        assert "--allow-harness-drift" in bench.commands["compare"].get_help(click.Context(bench.commands["compare"]))


class TestBudgetGate:
    """Test budget limit enforcement and refusal receipt emission."""

    def test_negative_cost_receipts_are_clamped_to_zero(self) -> None:
        """A hostile/buggy adapter cannot drive the cumulative budget negative."""

        class NegativeCostAdapter(MockReplayAdapter):
            def run_task(self, task: BenchTask, scheduler_config: dict[str, Any]) -> dict[str, Any]:
                res = super().run_task(task, scheduler_config)
                res["cost_usd"] = -5.0
                return res

        runner = BenchRunner(
            suite=BenchSuite("s", [BenchTask(id="t1", description="desc", steps=(), assertions=())]),
            adapter=NegativeCostAdapter(),
            scheduler_config={"scheduler": "s"},
        )
        bundle = runner.run()
        assert bundle.total_cost_usd == 0.0
        assert bundle.task_results[0].cost_usd == 0.0

    def test_runner_budget_gate_stops_early(self) -> None:
        suite = BenchSuite(
            version="test-v1",
            tasks=[
                BenchTask(id="t1", description="Task 1", steps=(), assertions=()),
                BenchTask(id="t2", description="Task 2", steps=(), assertions=()),
                BenchTask(id="t3", description="Task 3", steps=(), assertions=()),
            ],
        )

        class CostingAdapter(MockReplayAdapter):
            def run_task(self, task: BenchTask, scheduler_config: dict[str, Any]) -> dict[str, Any]:
                res = super().run_task(task, scheduler_config)
                res["cost_usd"] = 0.02
                res["tokens"] = 100
                res["duration_seconds"] = 0.5
                return res

        # Budget of $0.03 stops after task 2
        runner = BenchRunner(
            suite=suite,
            adapter=CostingAdapter(),
            scheduler_config={},
            budget_usd=0.03,
        )
        bundle = runner.run()

        assert len(bundle.task_results) == 3
        # Task 1 & 2 ran, Task 3 was refused / stopped due to budget
        assert bundle.task_results[0].passed is True
        assert bundle.task_results[1].passed is True
        assert bundle.task_results[2].passed is False
        assert "budget_exceeded" in bundle.task_results[2].receipt.get("refusal_reason", "")

    def test_cli_run_with_budget_announces_the_refusals_and_exits_non_zero(self, tmp_path: Path) -> None:
        """A run the budget cut short is not a completed run, and the CLI says so
        (#5464 review, F4) -- the bundle alone cannot be told apart from a run
        where one task of five passed."""
        out = tmp_path / "budget_bundle.json"
        runner = CliRunner()
        result = runner.invoke(
            bench_group,
            ["run", "golden-v1", "--out", str(out), "--budget", "0.001", "--stub-signer"],
        )
        assert result.exit_code == 2, result.output
        assert out.exists()
        bundle = SubmissionBundle.load(out)
        refused = [r for r in bundle.task_results if r.harness_output.get("refusal") == "budget_exceeded"]
        assert refused and len(refused) < len(bundle.task_results)
        assert f"{len(refused)}/{len(bundle.task_results)} tasks refused" in result.output
        assert "Budget exceeded: limit $0.0010" in result.output

    def test_cli_run_within_budget_is_a_normal_run(self, tmp_path: Path) -> None:
        out = tmp_path / "bundle.json"
        result = CliRunner().invoke(
            bench_group,
            ["run", "golden-v1", "--out", str(out), "--budget", "100", "--stub-signer"],
        )
        assert result.exit_code == 0, result.output
        assert "Budget exceeded" not in result.output

    def test_budget_with_reliability_is_refused_not_ignored(self, tmp_path: Path) -> None:
        """The reliability runner enforces no budget; accepting the flag and
        running K attempts uncapped would be a spend cap that vanished."""
        result = CliRunner().invoke(
            bench_group,
            ["run", "golden-v1", "--out", str(tmp_path / "r.json"), "--reliability", "2", "--budget", "1"],
        )
        assert result.exit_code != 0
        assert "--budget is not enforced on the --reliability path" in result.output
        assert not (tmp_path / "r.json").exists()

    def test_refused_tasks_verify_as_refused_not_as_fabricated(self, tmp_path: Path) -> None:
        """A refusal receipt is a verifiable receipt: replaying it must agree
        with the stored verdict (passed=False), not report a fabricated score."""
        from bernstein.eval.bench.golden_suite import build_golden_suite_v1
        from bernstein.eval.bench.verifier import BenchVerifier, VerificationStatus

        suite = build_golden_suite_v1()
        bundle = BenchRunner(suite=suite, adapter=MockReplayAdapter(), scheduler_config={}, budget_usd=0.001).run()
        assert any(r.harness_output.get("refusal") == "budget_exceeded" for r in bundle.task_results)
        result = BenchVerifier(suite=suite, adapter=MockReplayAdapter()).verify(bundle)
        assert result.status is VerificationStatus.MATCH, [
            (tr.task_id, tr.status.value, tr.detail)
            for tr in result.task_results
            if tr.status is not VerificationStatus.MATCH
        ]
        refused_ids = {r.task_id for r in bundle.task_results if r.harness_output.get("refusal") == "budget_exceeded"}
        for tr in result.task_results:
            if tr.task_id in refused_ids:
                assert tr.detail.startswith("refused: budget_exceeded"), tr

    def test_a_refusal_receipt_that_claims_a_score_is_fabricated(self) -> None:
        """The one thing a refusal receipt has to prove is that nothing was
        claimed for the task it refused."""
        import dataclasses

        from bernstein.eval.bench.golden_suite import build_golden_suite_v1
        from bernstein.eval.bench.verifier import BenchVerifier, VerificationStatus

        suite = build_golden_suite_v1()
        bundle = BenchRunner(suite=suite, adapter=MockReplayAdapter(), scheduler_config={}, budget_usd=0.001).run()
        forged = [
            dataclasses.replace(r, passed=True, score=1.0) if r.receipt.get("status") == "refused" else r
            for r in bundle.task_results
        ]
        forged_bundle = dataclasses.replace(bundle, task_results=forged)
        result = BenchVerifier(suite=suite, adapter=MockReplayAdapter()).verify(forged_bundle)
        assert result.status is VerificationStatus.DIVERGED
        assert any(
            tr.status is VerificationStatus.FABRICATED_SCORE and "refused task scores nothing" in tr.detail
            for tr in result.task_results
        )


class TestOneCanonicalRefusalMarker:
    """Three readers decide what a refusal is; they have to decide the same way.

    The verifier scores a refusal instead of replaying it, the comparison
    counts refusals so a budget-cut run cannot read as a cheaper complete one,
    and `bench run` says so on stdout and exits non-zero. Two of them read
    ``receipt["status"]`` and the third read ``harness_output["refusal"]``, so
    a refusal receipt carrying only the canonical field verified clean, went
    uncounted, and the run reported success.
    """

    @staticmethod
    def _refusal_result(task_id: str = "t1") -> TaskResult:
        """A refusal receipt with the canonical marker and nothing else.

        Deliberately empty ``harness_output``: that is the shape the readers
        disagreed about, and a reader that still needs the non-canonical
        field fails here.
        """
        return TaskResult(
            task_id=task_id,
            task_hash="0" * 64,
            receipt={
                "journal_head": "",
                "spine_head": "",
                "run_id": f"refusal-{task_id}",
                "refusal_reason": "budget_exceeded: limit $0.0100 exceeded (spent $0.0200)",
                "status": REFUSED_STATUS,
            },
            passed=False,
            score=0.0,
            harness_output={},
        )

    def _bundle(self, results: list[TaskResult]) -> SubmissionBundle:
        return SubmissionBundle(
            suite_hash="a" * 64,
            suite_version="golden-v1",
            task_results=results,
            scheduler_config={"scheduler": "default"},
        )

    def test_the_runner_writes_the_canonical_marker(self) -> None:
        suite = BenchSuite(
            version="golden-v1",
            tasks=[
                BenchTask(id=f"t{i}", description="d", steps=("s",), assertions=({"k": "v"},), category="c")
                for i in range(3)
            ],
        )
        bundle = BenchRunner(
            suite=suite,
            adapter=MockReplayAdapter(),
            scheduler_config={"scheduler": "default"},
            budget_usd=0.0,
        ).run()
        refusals = [r for r in bundle.task_results if r.receipt.get("status") == REFUSED_STATUS]
        assert refusals, "a zero budget should refuse every task"

    def test_the_comparison_counts_it(self) -> None:
        a = self._bundle([self._refusal_result("t1")])
        b = self._bundle([self._refusal_result("t1")])
        report = compare_bundles(a, b)
        assert report.refused_a == 1
        assert report.refused_b == 1

    def test_the_verifier_recognises_it(self) -> None:
        from bernstein.eval.bench.runner import MockReplayAdapter as Adapter
        from bernstein.eval.bench.verifier import BenchVerifier, VerificationStatus

        suite = BenchSuite(
            version="golden-v1",
            tasks=[BenchTask(id="t1", description="d", steps=("s",), assertions=({"k": "v"},), category="c")],
        )
        result = self._refusal_result("t1")
        result.task_hash = suite.tasks[0].content_hash()
        bundle = SubmissionBundle(
            suite_hash=suite.suite_hash,
            suite_version=suite.version,
            task_results=[result],
            scheduler_config={"scheduler": "default"},
        )
        verified = BenchVerifier(suite=suite, adapter=Adapter()).verify(bundle)
        assert verified.status is VerificationStatus.MATCH
        assert "refused" in verified.task_results[0].detail

    def test_the_summary_reader_is_the_same_predicate(self) -> None:
        """`bench run`'s summary was the reader still on the non-canonical field.

        It cannot be fed a hand-built bundle -- it always runs the runner, which
        writes both fields -- so the check is on the predicate it now calls.
        """
        bundle = self._bundle([self._refusal_result("t1"), self._refusal_result("t2")])
        assert [r.task_id for r in bundle.refused_results()] == ["t1", "t2"]
        assert all(not r.harness_output for r in bundle.refused_results())

    def test_the_cli_summary_reports_it(self, tmp_path: Path) -> None:
        """End to end, through the installed command."""
        suite_path = tmp_path / "suite.json"
        BenchSuite(
            version="golden-v1",
            tasks=[
                BenchTask(id=f"t{i}", description="d", steps=("s",), assertions=({"k": "v"},), category="c")
                for i in range(3)
            ],
        ).save(suite_path)
        out = tmp_path / "bundle.json"
        result = CliRunner().invoke(
            bench_group,
            ["run", str(suite_path), "--out", str(out), "--stub-signer", "--budget", "0.0"],
        )
        assert result.exit_code == 2, result.output
        assert "tasks refused and recorded as refusal receipts" in result.output


class TestUnreportedMetricsAreNotFacts:
    """A metric the harness never reported must not be recorded as a value (#5464).

    Two separate properties are at stake. The bundle is evidence, so an
    absent cost must not read as ``$0.00``; and the bundle hash is a
    determinism guarantee, so nothing derived from the wall clock may reach
    it.
    """

    class _SilentAdapter:
        """Reports a verdict and no resource metrics at all."""

        def run_task(self, task: BenchTask, scheduler_config: dict[str, Any]) -> dict[str, Any]:
            task_hash = task.content_hash()
            return {
                "journal_head": hashlib.sha256(f"j:{task_hash}".encode()).hexdigest(),
                "spine_head": hashlib.sha256(f"s:{task_hash}".encode()).hexdigest(),
                "run_id": f"silent-{task_hash[:12]}",
                "events": [],
            }

        def score_task(self, task: BenchTask, receipt: dict[str, Any]) -> tuple[bool, float, dict[str, Any]]:
            return True, 1.0, {}

    @staticmethod
    def _suite() -> BenchSuite:
        return BenchSuite(
            version="golden-v1",
            tasks=[BenchTask(id="t1", description="d", steps=(), assertions=())],
        )

    def test_unreported_metrics_are_absent_not_zero(self) -> None:
        bundle = BenchRunner(
            suite=self._suite(),
            adapter=self._SilentAdapter(),
            scheduler_config={"scheduler": "default"},
        ).run()

        result = bundle.task_results[0]
        assert result.tokens is None
        assert result.cost_usd is None
        assert result.duration_seconds is None
        assert not result.has_resource_metrics()

        # And the serialised form omits them rather than writing nulls or
        # zeros: a consumer that coerces must not be handed a number.
        row = result.to_dict()
        assert "tokens" not in row
        assert "cost_usd" not in row
        assert "duration_seconds" not in row

    def test_a_reported_zero_still_counts_as_reported(self) -> None:
        """0 is a measurement. Only absence means "not measured"."""
        result = TaskResult(
            task_id="t1",
            task_hash="h",
            receipt={"journal_head": "j", "spine_head": "s"},
            passed=True,
            score=1.0,
            tokens=0,
            cost_usd=0.0,
            duration_seconds=0.0,
        )
        assert result.has_resource_metrics()
        row = result.to_dict()
        assert row["tokens"] == 0
        assert row["cost_usd"] == 0.0
        assert row["duration_seconds"] == 0.0

    def test_bundle_hash_determinism_preserved(self) -> None:
        """Two runs of one suite hash identically when no metrics are reported.

        The previous code substituted wall-clock ``t1 - t0`` for an
        unreported duration, so the value differed on every run and two
        replays of the same suite produced different bundle hashes. This is
        the regression guard for that.
        """
        suite = self._suite()
        cfg = {"scheduler": "default"}
        first = BenchRunner(suite=suite, adapter=self._SilentAdapter(), scheduler_config=cfg).run()
        second = BenchRunner(suite=suite, adapter=self._SilentAdapter(), scheduler_config=cfg).run()

        # submitted_at is a wall-clock stamp by design and is not part of the
        # per-task payload under test, so compare the task rows directly.
        assert [r.to_dict() for r in first.task_results] == [r.to_dict() for r in second.task_results]

    def test_totals_sum_what_was_reported(self) -> None:
        """An unreported metric contributes nothing rather than a zero."""
        bundle = _make_sample_bundle(
            task_specs=[
                {"id": "t1", "tokens": 30, "cost_usd": 0.02, "duration_seconds": 1.0},
                {"id": "t2", "tokens": None, "cost_usd": None, "duration_seconds": None},
            ]
        )
        assert bundle.total_tokens == 30
        assert bundle.total_cost_usd == pytest.approx(0.02)
        assert bundle.total_duration_seconds == pytest.approx(1.0)


class TestBundleSchemaVersion:
    """The bundle declares its schema version, and old bundles keep their hash (#5464)."""

    def test_a_new_bundle_declares_the_current_schema_version(self) -> None:
        bundle = _make_sample_bundle(task_specs=[{"id": "t1"}])
        assert bundle.schema_version == BUNDLE_SCHEMA_VERSION
        assert bundle.to_dict()["schema_version"] == BUNDLE_SCHEMA_VERSION

    def test_a_version_1_bundle_keeps_its_hash_and_omits_the_key(self) -> None:
        """A bundle written before the field existed must still verify.

        Its stored hash was computed over a payload with no ``schema_version``
        key, so version 1 must not contribute one — otherwise every
        pre-existing bundle fails the integrity check it exists to pass.
        """
        bundle = _make_sample_bundle(task_specs=[{"id": "t1"}])
        legacy = SubmissionBundle(
            suite_hash=bundle.suite_hash,
            suite_version=bundle.suite_version,
            task_results=bundle.task_results,
            scheduler_config=bundle.scheduler_config,
            submitted_at=bundle.submitted_at,
            schema_version=1,
        )
        assert "schema_version" not in legacy.to_dict()
        # Round-trips back to version 1 rather than being silently upgraded.
        assert SubmissionBundle.from_dict(legacy.to_dict()).schema_version == 1
        assert SubmissionBundle.from_dict(legacy.to_dict()).bundle_hash() == legacy.bundle_hash()

    def test_the_version_changes_the_hash(self) -> None:
        """The declared version is bound into the hash, not merely reported."""
        bundle = _make_sample_bundle(task_specs=[{"id": "t1"}])
        legacy = SubmissionBundle(
            suite_hash=bundle.suite_hash,
            suite_version=bundle.suite_version,
            task_results=bundle.task_results,
            scheduler_config=bundle.scheduler_config,
            submitted_at=bundle.submitted_at,
            schema_version=1,
        )
        assert bundle.bundle_hash() != legacy.bundle_hash()
