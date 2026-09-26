"""Unit tests for benchmark CI surface, SARIF output, and scorecard deltas (#5458).

Acceptance Criteria:
1. SARIF 2.1.0 document shape, one result per failed case. (Structural checks;
   the SARIF JSON Schema is not vendored, so this is not schema validation.)
2. Check run renders the table; failure on regression; neutral on a missing,
   unsigned, wrong-suite or unverifiable baseline -- never a green.
3. Docs page updated.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from click.testing import CliRunner

from bernstein.eval.bench.bench_cli import bench_group
from bernstein.eval.bench.bundle import SubmissionBundle, TaskResult
from bernstein.eval.bench.ci import evaluate_ci_scorecard, post_bench_check_run
from bernstein.eval.bench.golden_suite import build_golden_suite_v1
from bernstein.eval.bench.runner import BenchRunner, MockReplayAdapter
from bernstein.eval.bench.sarif import bundle_to_sarif
from bernstein.eval.bench.signer import StubSigner
from bernstein.eval.bench.suite import BenchSuite, BenchTask
from bernstein.eval.bench.verifier import BenchVerifier
from bernstein.github_app.check_runs import CheckRunClient


def _make_test_bundle(*, suite_hash: str = "suite123", tasks: list[dict]) -> SubmissionBundle:
    results = [
        TaskResult(
            task_id=t["id"],
            task_hash=f"hash_{t['id']}",
            receipt={"journal_head": "jh", "spine_head": "sh", "run_id": f"r_{t['id']}"},
            passed=t.get("passed", True),
            score=t.get("score", 1.0),
            harness_output=t.get("harness_output", {}),
        )
        for t in tasks
    ]
    return SubmissionBundle(
        suite_hash=suite_hash,
        suite_version="golden-v1",
        task_results=results,
        scheduler_config={"scheduler": "default"},
    )


class TestSarifGeneration:
    """Test generating SARIF 2.1.0 reports for benchmark runs."""

    def test_sarif_structure_on_passing_bundle(self) -> None:
        bundle = _make_test_bundle(tasks=[{"id": "t1", "passed": True, "score": 1.0}])
        sarif = bundle_to_sarif(bundle)

        assert sarif["version"] == "2.1.0"
        assert "$schema" in sarif
        assert len(sarif["runs"]) == 1
        run = sarif["runs"][0]
        assert run["tool"]["driver"]["name"] == "bernstein-bench"
        assert len(run["results"]) == 0

    def test_sarif_contains_failed_task_details(self) -> None:
        bundle = _make_test_bundle(
            tasks=[
                {"id": "task_ok", "passed": True, "score": 1.0},
                {
                    "id": "task_bad",
                    "passed": False,
                    "score": 0.0,
                    "harness_output": {"error": "assertion_failed: file missing"},
                },
            ]
        )
        sarif = bundle_to_sarif(bundle)
        run = sarif["runs"][0]
        assert len(run["results"]) == 1
        res = run["results"][0]
        assert res["ruleId"] == "task_bad"
        assert res["level"] == "error"
        assert "assertion_failed" in res["message"]["text"] or "task_bad" in res["message"]["text"]
        # No suite uri was given, so the result claims no location rather
        # than a path that does not exist.
        assert "locations" not in res
        assert res["partialFingerprints"]["bernstein/taskHash"] == "hash_task_bad"

    def test_sarif_location_is_the_suite_source_when_known(self) -> None:
        bundle = _make_test_bundle(tasks=[{"id": "task_bad", "passed": False, "score": 0.0}])
        sarif = bundle_to_sarif(bundle, suite_uri="src/bernstein/eval/bench/golden_suite.py")
        (res,) = sarif["runs"][0]["results"]
        assert res["locations"][0]["physicalLocation"]["artifactLocation"]["uri"] == (
            "src/bernstein/eval/bench/golden_suite.py"
        )

    def test_sarif_semantic_version_is_the_tool_version_not_the_suite(self) -> None:
        from importlib.metadata import version

        bundle = _make_test_bundle(tasks=[{"id": "t", "passed": True}])
        driver = bundle_to_sarif(bundle)["runs"][0]["tool"]["driver"]
        assert driver["semanticVersion"] == version("bernstein")
        assert driver["properties"]["suiteVersion"] == "golden-v1"

    def test_sarif_does_not_default_semantic_version_to_zero(self) -> None:
        """An uninstalled package omits the field rather than claiming 0.0.0.

        SARIF makes ``semanticVersion`` optional, and the report is an
        operator-facing artefact: "unknown" has to be spelled as absence, not
        as the factual claim that the tool is at version 0.0.0. A consumer
        comparing versions would otherwise read a real, and wrong, answer.
        """
        from importlib.metadata import PackageNotFoundError

        bundle = _make_test_bundle(tasks=[{"id": "t", "passed": True}])
        with patch(
            "importlib.metadata.version",
            side_effect=PackageNotFoundError("bernstein"),
        ):
            driver = bundle_to_sarif(bundle)["runs"][0]["tool"]["driver"]

        assert "semanticVersion" not in driver, (
            f"semanticVersion must be omitted when the package is not installed, got {driver.get('semanticVersion')!r}"
        )
        # The rest of the driver is unaffected: absence of a version is not a
        # reason to lose the identity of the tool or the suite it ran.
        assert driver["name"] == "bernstein-bench"
        assert driver["properties"]["suiteVersion"] == "golden-v1"


class TestScorecardEvaluation:
    """Test scorecard calculation, baseline comparison, and conclusions."""

    def test_scorecard_with_no_baseline_is_neutral(self) -> None:
        suite = build_golden_suite_v1()
        bundle = _make_test_bundle(suite_hash=suite.suite_hash, tasks=[{"id": suite.tasks[0].id, "passed": True}])
        scorecard = evaluate_ci_scorecard(bundle=bundle, suite=suite, baseline_bundle=None)

        assert scorecard.conclusion == "neutral"
        assert scorecard.baseline_pass_rate is None
        assert "baseline" in scorecard.summary.lower()
        md = scorecard.to_markdown()
        assert bundle.suite_version in md
        assert bundle.bundle_hash()[:12] in md

    def test_scorecard_with_unverifiable_baseline_is_neutral(self) -> None:
        suite = build_golden_suite_v1()
        adapter = MockReplayAdapter()
        verifier = BenchVerifier(suite=suite, adapter=adapter)

        curr_bundle = BenchRunner(suite=suite, adapter=adapter, scheduler_config={}).run()

        # Create tampered baseline bundle
        bad_task = TaskResult(
            task_id=suite.tasks[0].id,
            task_hash=suite.tasks[0].content_hash(),
            receipt={"journal_head": "tampered", "spine_head": "bad"},
            passed=True,
            score=1.0,
            stored_receipt_hash="fake_hash",
        )
        bad_baseline = StubSigner().sign(
            SubmissionBundle(
                suite_hash=suite.suite_hash,
                suite_version=suite.version,
                task_results=[bad_task],
                scheduler_config={},
            )
        )

        scorecard = evaluate_ci_scorecard(
            bundle=curr_bundle,
            suite=suite,
            baseline_bundle=bad_baseline,
            verifier=verifier,
        )

        assert scorecard.conclusion == "neutral"
        assert "unverifiable" in scorecard.summary.lower()

    def test_scorecard_with_verified_baseline_success(self) -> None:
        suite = build_golden_suite_v1()
        adapter = MockReplayAdapter()
        verifier = BenchVerifier(suite=suite, adapter=adapter)

        baseline_bundle = StubSigner().sign(BenchRunner(suite=suite, adapter=adapter, scheduler_config={}).run())
        curr_bundle = BenchRunner(suite=suite, adapter=adapter, scheduler_config={}).run()

        scorecard = evaluate_ci_scorecard(
            bundle=curr_bundle,
            suite=suite,
            baseline_bundle=baseline_bundle,
            verifier=verifier,
            regression_threshold=0.0,
        )

        assert scorecard.conclusion == "success"
        assert scorecard.pass_rate_delta == pytest.approx(0.0)
        assert scorecard.baseline_pass_rate == pytest.approx(1.0)
        md = scorecard.to_markdown()
        assert "PASS" in md or "✓" in md

    def test_scorecard_fails_on_regression(self) -> None:
        suite = BenchSuite(
            version="test-v1",
            tasks=[
                BenchTask(id="t1", description="T1", steps=(), assertions=()),
                BenchTask(id="t2", description="T2", steps=(), assertions=()),
            ],
        )

        # The baseline has to survive the verifier's replay, so it is a real run.
        baseline = BenchRunner(suite=suite, adapter=MockReplayAdapter(), scheduler_config={}).run()
        assert baseline.pass_rate == 1.0
        # Regression: t2 fails (50% pass rate vs 100% baseline)
        curr = _make_test_bundle(
            suite_hash=suite.suite_hash,
            tasks=[{"id": "t1", "passed": True, "score": 1.0}, {"id": "t2", "passed": False, "score": 0.0}],
        )

        scorecard = evaluate_ci_scorecard(
            bundle=curr,
            suite=suite,
            baseline_bundle=StubSigner().sign(baseline),
            verifier=BenchVerifier(suite=suite, adapter=MockReplayAdapter()),
            regression_threshold=0.05,
        )

        assert scorecard.conclusion == "failure"
        assert scorecard.pass_rate_delta == pytest.approx(-0.5)
        assert "regression" in scorecard.summary.lower()

    # -- never a green: every way a baseline falls short is neutral -------------

    def test_an_unsigned_baseline_is_neutral_even_when_its_receipts_verify(self) -> None:
        """The first review's F1: byte-correct receipts and an empty signature
        used to come out green."""
        suite = build_golden_suite_v1()
        adapter = MockReplayAdapter()
        unsigned = BenchRunner(suite=suite, adapter=adapter, scheduler_config={}).run()
        assert unsigned.signature == ""
        scorecard = evaluate_ci_scorecard(
            bundle=unsigned,
            suite=suite,
            baseline_bundle=unsigned,
            verifier=BenchVerifier(suite=suite, adapter=adapter),
        )
        assert scorecard.conclusion == "neutral"
        assert "unsigned" in scorecard.summary

    def test_a_stub_signed_baseline_altered_after_signing_is_neutral(self) -> None:
        import dataclasses

        suite = build_golden_suite_v1()
        adapter = MockReplayAdapter()
        signed = StubSigner().sign(BenchRunner(suite=suite, adapter=adapter, scheduler_config={}).run())
        # Same signature, different content: the hash the stub signed is gone.
        altered = dataclasses.replace(signed, submitted_at=signed.submitted_at + 1)
        assert altered.signature == signed.signature and altered.bundle_hash() != signed.bundle_hash()
        scorecard = evaluate_ci_scorecard(
            bundle=signed, suite=suite, baseline_bundle=altered, verifier=BenchVerifier(suite=suite, adapter=adapter)
        )
        assert scorecard.conclusion == "neutral"
        assert "altered after signing" in scorecard.summary

    def test_a_baseline_from_another_suite_is_neutral(self) -> None:
        suite = build_golden_suite_v1()
        adapter = MockReplayAdapter()
        current = BenchRunner(suite=suite, adapter=adapter, scheduler_config={}).run()
        other = BenchSuite(version="other-v1", tasks=[BenchTask(id="x", description="x", steps=(), assertions=())])
        foreign = StubSigner().sign(BenchRunner(suite=other, adapter=adapter, scheduler_config={}).run())
        scorecard = evaluate_ci_scorecard(
            bundle=current, suite=suite, baseline_bundle=foreign, verifier=BenchVerifier(suite=suite, adapter=adapter)
        )
        assert scorecard.conclusion == "neutral"
        assert "different suite" in scorecard.summary

    def test_no_verifier_is_neutral_not_a_way_to_skip_the_check(self) -> None:
        suite = build_golden_suite_v1()
        adapter = MockReplayAdapter()
        signed = StubSigner().sign(BenchRunner(suite=suite, adapter=adapter, scheduler_config={}).run())
        scorecard = evaluate_ci_scorecard(bundle=signed, suite=suite, baseline_bundle=signed, verifier=None)
        assert scorecard.conclusion == "neutral"
        assert "No verifier" in scorecard.summary

    def test_an_install_identity_signature_is_neutral_not_verified(self) -> None:
        """Nothing in the bench layer can verify one yet (#5856); a delta against
        an unverifiable baseline must not read as success."""
        import dataclasses

        suite = build_golden_suite_v1()
        adapter = MockReplayAdapter()
        base = BenchRunner(suite=suite, adapter=adapter, scheduler_config={}).run()
        signed = dataclasses.replace(base, signature="eyJ...detached", signer_fingerprint="ab12cd34ef56")
        scorecard = evaluate_ci_scorecard(
            bundle=base, suite=suite, baseline_bundle=signed, verifier=BenchVerifier(suite=suite, adapter=adapter)
        )
        assert scorecard.conclusion == "neutral"
        assert "cannot be verified" in scorecard.summary
        assert "#5856" in scorecard.summary


class TestCheckRunPosting:
    """Test creating check runs with scorecard summaries."""

    def test_post_bench_check_run(self) -> None:
        suite = build_golden_suite_v1()
        bundle = _make_test_bundle(suite_hash=suite.suite_hash, tasks=[{"id": suite.tasks[0].id, "passed": True}])
        scorecard = evaluate_ci_scorecard(bundle=bundle, suite=suite, baseline_bundle=None)

        client = CheckRunClient(repo="owner/repo")
        with patch.object(client, "create_bench_check_run") as mock_create:
            mock_create.return_value = MagicMock(check_run_id=123)
            res = post_bench_check_run(scorecard=scorecard, client=client, head_sha="abc1234")

            assert res is not None
            mock_create.assert_called_once()
            args, kwargs = mock_create.call_args
            assert kwargs.get("conclusion") == "neutral" or (len(args) > 2 and args[2] == "neutral")


class TestCLI_CI_Integration:
    """Test CLI --ci and --sarif-out options."""

    def test_cli_run_ci_mode(self, tmp_path: Path) -> None:
        out_bundle = tmp_path / "bundle.json"
        sarif_out = tmp_path / "report.sarif"

        runner = CliRunner()
        result = runner.invoke(
            bench_group,
            [
                "run",
                "golden-v1",
                "--out",
                str(out_bundle),
                "--stub-signer",
                "--ci",
                "--sarif-out",
                str(sarif_out),
            ],
        )

        assert result.exit_code == 0, result.output
        assert out_bundle.exists()
        assert sarif_out.exists()
        sarif_data = json.loads(sarif_out.read_text(encoding="utf-8"))
        assert sarif_data["version"] == "2.1.0"
        assert "NEUTRAL" in result.output  # no baseline given

    def test_cli_missing_baseline_path_is_an_error_not_neutral(self, tmp_path: Path) -> None:
        out_bundle = tmp_path / "b.json"
        result = CliRunner().invoke(
            bench_group,
            [
                "run",
                "golden-v1",
                "--out",
                str(out_bundle),
                "--stub-signer",
                "--ci",
                "--baseline",
                str(tmp_path / "absent.json"),
            ],
        )
        assert result.exit_code != 0
        assert "Baseline bundle not found" in result.output
        assert not out_bundle.exists()
        assert not out_bundle.with_suffix(".sarif").exists()

    def test_cli_unloadable_baseline_is_neutral_with_the_reason(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.json"
        bad.write_text("{not json", encoding="utf-8")
        result = CliRunner().invoke(
            bench_group,
            ["run", "golden-v1", "--out", str(tmp_path / "b.json"), "--stub-signer", "--ci", "--baseline", str(bad)],
        )
        assert result.exit_code == 0, result.output
        assert "NEUTRAL" in result.output
        assert "could not be loaded" in result.output

    def test_cli_ci_with_a_signed_baseline_of_the_same_run_is_green(self, tmp_path: Path) -> None:
        runner = CliRunner()
        first = runner.invoke(bench_group, ["run", "golden-v1", "--out", str(tmp_path / "base.json"), "--stub-signer"])
        assert first.exit_code == 0, first.output
        second = runner.invoke(
            bench_group,
            [
                "run",
                "golden-v1",
                "--out",
                str(tmp_path / "b.json"),
                "--stub-signer",
                "--ci",
                "--baseline",
                str(tmp_path / "base.json"),
            ],
        )
        assert second.exit_code == 0, second.output
        assert "PASS" in second.output

    def test_the_sarif_location_is_derived_from_the_suite_name(self, tmp_path: Path) -> None:
        """Direct, because golden-v1 under the mock adapter fails no task and a
        loop over zero results would assert nothing."""
        from bernstein.eval.bench.bench_cli import _suite_source_uri

        assert _suite_source_uri("golden-v1") == "src/bernstein/eval/bench/golden_suite.py"
        assert _suite_source_uri("tool-surface-v1") == "src/bernstein/eval/bench/tool_surface_suite.py"
        assert _suite_source_uri("no-such-suite") is None

    def test_a_custom_suite_inside_the_checkout_is_named_relative_to_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from bernstein.eval.bench import bench_cli

        monkeypatch.setattr(bench_cli, "_REPO_ROOT", tmp_path.resolve())
        suite_path = tmp_path / "bench" / "custom.json"
        suite_path.parent.mkdir()
        build_golden_suite_v1().save(suite_path)
        assert bench_cli._suite_source_uri(str(suite_path)) == "bench/custom.json"

    def test_a_custom_suite_outside_the_checkout_carries_no_location(self, tmp_path: Path) -> None:
        """A runner-absolute path anchors nothing in the repository the report is read against.

        SARIF locations are resolved against the repository a code-scanning
        upload is attached to, so an absolute path from the build machine
        points at nothing there -- and publishes that machine's layout into
        the report. No location is the honest answer.
        """
        from bernstein.eval.bench.bench_cli import _suite_source_uri

        suite_path = tmp_path / "custom.json"
        build_golden_suite_v1().save(suite_path)
        assert _suite_source_uri(str(suite_path)) is None

    def test_a_negative_regression_threshold_is_refused(self, tmp_path: Path) -> None:
        """A negative tolerance would make a perfect run conclude failure."""
        result = CliRunner().invoke(
            bench_group,
            [
                "run",
                "golden-v1",
                "--out",
                str(tmp_path / "b.json"),
                "--stub-signer",
                "--ci",
                "--regression-threshold",
                "-0.05",
            ],
        )
        assert result.exit_code == 2, result.output
        assert "--regression-threshold" in result.output and "not in the range" in result.output
        assert not (tmp_path / "b.json").exists()

    def test_a_check_run_that_was_not_posted_is_announced(self, tmp_path: Path) -> None:
        """--repo and --head-sha asked for a check run; a None from the client is not silence."""
        with patch.object(CheckRunClient, "create_bench_check_run", return_value=None) as create:
            result = CliRunner().invoke(
                bench_group,
                [
                    "run",
                    "golden-v1",
                    "--out",
                    str(tmp_path / "b.json"),
                    "--stub-signer",
                    "--ci",
                    "--repo",
                    "owner/repo",
                    "--head-sha",
                    "abc123",
                ],
            )
        assert result.exit_code == 0, result.output
        assert create.called
        assert "Warning: the bench scorecard check run was not posted" in result.stderr

    def test_half_configured_check_run_flags_are_announced(self, tmp_path: Path) -> None:
        result = CliRunner().invoke(
            bench_group,
            ["run", "golden-v1", "--out", str(tmp_path / "b.json"), "--stub-signer", "--ci", "--repo", "owner/repo"],
        )
        assert result.exit_code == 0, result.output
        assert "--repo and --head-sha are both needed" in result.stderr


class TestTheOptionCombinationIsValidatedBeforeAnythingRuns:
    """An option that is accepted and then does nothing is the failure the budget gate was told not to have."""

    def test_reliability_refuses_the_ci_only_options(self, tmp_path: Path) -> None:
        """`--reliability` returns before the CI block, so those flags were silently dropped.

        The run exited 0 having written only the reliability receipt: no
        SARIF, no scorecard, no check run, no CI conclusion.
        """
        for flag, value in (
            ("--ci", None),
            ("--sarif-out", str(tmp_path / "r.sarif")),
            ("--baseline", str(tmp_path / "base.json")),
            ("--repo", "owner/repo"),
            ("--head-sha", "a" * 40),
        ):
            args = ["run", "golden-v1", "--out", str(tmp_path / "b.json"), "--stub-signer", "--reliability", "2", flag]
            if value is not None:
                args.append(value)
            result = CliRunner().invoke(bench_group, args)
            assert result.exit_code != 0, f"{flag}: {result.output}"
            assert "cannot be combined with --reliability" in result.output, flag

    def test_reliability_without_the_ci_options_still_runs(self, tmp_path: Path) -> None:
        out = tmp_path / "receipt.json"
        result = CliRunner().invoke(
            bench_group, ["run", "golden-v1", "--out", str(out), "--stub-signer", "--reliability", "2"]
        )
        assert result.exit_code == 0, result.output
        assert out.is_file()

    def test_a_baseline_alone_is_evaluated_not_ignored(self, tmp_path: Path) -> None:
        """`--baseline` without `--ci`/`--sarif-out` exited 0 having compared nothing.

        A zero exit then reads as "no regression" when no regression check
        ran at all.
        """
        base = tmp_path / "base.json"
        first = CliRunner().invoke(bench_group, ["run", "golden-v1", "--out", str(base), "--stub-signer"])
        assert first.exit_code == 0, first.output

        result = CliRunner().invoke(
            bench_group,
            ["run", "golden-v1", "--out", str(tmp_path / "b.json"), "--stub-signer", "--baseline", str(base)],
        )
        assert result.exit_code == 0, result.output
        assert "Bench Scorecard" in result.output or "Pass rate" in result.output
        assert "Baseline" in result.output
        # No SARIF was asked for, so none is written.
        assert not (tmp_path / "b.sarif").exists()

    def test_a_missing_baseline_is_a_configuration_error_even_without_ci(self, tmp_path: Path) -> None:
        result = CliRunner().invoke(
            bench_group,
            [
                "run",
                "golden-v1",
                "--out",
                str(tmp_path / "b.json"),
                "--stub-signer",
                "--baseline",
                str(tmp_path / "absent.json"),
            ],
        )
        assert result.exit_code != 0
        assert "Baseline bundle not found" in result.output

    def test_half_a_check_run_target_is_announced(self, tmp_path: Path) -> None:
        """`--repo` without `--head-sha` used to skip silently."""
        result = CliRunner().invoke(
            bench_group,
            ["run", "golden-v1", "--out", str(tmp_path / "b.json"), "--stub-signer", "--repo", "owner/repo"],
        )
        assert result.exit_code == 0, result.output
        assert "--repo and --head-sha are both needed" in result.output
