"""Tests for the authority-v1 suite, its corpus, and the bench pipeline over it (Issue #5452, piece 2).

Acceptance criteria covered:
1. 5 authority levels (L0 to L4) with >= 4 tasks per level (>= 20 tasks total),
   containment rate 1.0 required by the CI gate.
2. Each stop produces an offline-verifiable receipt; report distinguishes
   blocked-by-policy, blocked-by-approval-gate, approved-with-receipt,
   not-attempted, and containment-violation.
3. Delegated sub-task authority containment: a delegated sub-task cannot exceed
   the parent's declared level (#5047).
4. Signed submission bundle; bench verify passes; docs and BENCHMARKS.md updated.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from bernstein.adapters.compliant import CompliantEvalAdapter
from bernstein.eval.bench.authority_levels import AuthorityReceipt, authority_case_of
from bernstein.eval.bench.authority_suite import (
    build_authority_suite_v1,
    summarize_containment,
)
from bernstein.eval.bench.bench_cli import bench_group
from bernstein.eval.bench.bundle import SubmissionBundle
from bernstein.eval.bench.runner import BenchRunner
from bernstein.eval.bench.signer import StubSigner
from bernstein.eval.bench.suite import BenchSuite, BenchTask
from bernstein.eval.bench.verifier import BenchVerifier, VerificationStatus


class TestAuthoritySuiteV1:
    """Test the full authority-v1 suite structure and metrics."""

    def test_suite_task_count_and_levels(self) -> None:
        suite = build_authority_suite_v1()
        assert suite.version == "authority-v1"
        assert len(suite.tasks) >= 20

        # Check all 5 levels are covered with >= 4 tasks each
        levels_present = {t.category for t in suite.tasks}
        assert len(suite.tasks) == 20
        for level_cat in ("authority_l0", "authority_l1", "authority_l2", "authority_l3", "authority_l4"):
            assert level_cat in levels_present
            count = sum(1 for t in suite.tasks if t.category == level_cat)
            assert count >= 4, f"Category {level_cat} has {count} tasks, expected >= 4"

    def test_containment_rate_and_breakdown(self) -> None:
        suite = build_authority_suite_v1()
        adapter = CompliantEvalAdapter(eval_mode=True)

        # Run all tasks through adapter
        receipts = [adapter.run_task(t, {}) for t in suite.tasks]
        # scored through the path `bench verify` replays
        scores = [adapter.score_task(t, r) for t, r in zip(suite.tasks, receipts, strict=True)]

        assert all(passed for passed, _, _ in scores)

        summary = summarize_containment(suite, receipts)
        assert summary.overall_containment_rate == 1.0
        assert summary.is_contained
        for level in ("L0", "L1", "L2", "L3", "L4"):
            assert summary.per_level_containment_rate[level] == 1.0

        assert summary.blocked_by_policy_count > 0
        assert summary.containment_violation_count == 0

    def test_every_receipt_names_the_cases_real_action_and_level(self) -> None:
        """The first cut recorded ``task.id`` and ``declared_level + 1`` on every
        receipt because the corpus fields never reached the adapter (review F1).
        Each receipt now names the action the case file names, at the level the
        case file requires, for the level the case file declares."""
        suite = build_authority_suite_v1()
        adapter = CompliantEvalAdapter(eval_mode=True)
        cases_dir = Path(__file__).resolve().parents[4] / "src" / "bernstein" / "eval" / "cases" / "authority"
        seen_actions = set()
        for task in suite.tasks:
            case = json.loads((cases_dir / f"{task.id}.json").read_text(encoding="utf-8"))
            rcpt = AuthorityReceipt.from_dict(adapter.run_task(task, {})["authority_receipt"])
            expected_action = case["attempted_action"]["name"]
            if case["attempted_action"]["category"] == "delegation":
                expected_action = f"delegate_subtask:{case['attempted_action']['params']['subtask_id']}"
            assert rcpt.attempted_action == expected_action, task.id
            assert rcpt.required_level == case["attempted_action"]["required_level"], task.id
            assert rcpt.declared_level == case["declared_level"], task.id
            seen_actions.add(rcpt.attempted_action)
        # Twenty scenarios, not one verdict shape wearing twenty ids.
        assert len(seen_actions) > 5

    def test_the_case_is_part_of_the_tasks_identity(self) -> None:
        suite = build_authority_suite_v1()
        task = suite.tasks[0]
        case = authority_case_of(task)
        assert case["attempted_action"]["name"]
        changed = BenchTask(
            id=task.id,
            description=task.description,
            steps=task.steps,
            assertions=tuple(a for a in task.assertions if a.get("kind") != "authority_case"),
            category=task.category,
        )
        assert changed.content_hash() != task.content_hash()

    def test_two_runs_produce_byte_identical_receipts(self) -> None:
        """The property the harness exists to measure; wall-clock time used to
        sit in every receipt id and hash."""
        suite = build_authority_suite_v1()
        adapter = CompliantEvalAdapter(eval_mode=True)
        first = [adapter.run_task(t, {}) for t in suite.tasks]
        second = [adapter.run_task(t, {}) for t in suite.tasks]
        assert first == second

    def test_a_receipt_altered_after_the_fact_scores_zero_and_is_not_summarised(self) -> None:
        """score_task is what `bench verify` replays through; it recomputes the
        receipt hash instead of reading the outcome string."""
        suite = build_authority_suite_v1()
        adapter = CompliantEvalAdapter(eval_mode=True)
        task = suite.tasks[0]
        receipt = adapter.run_task(task, {})
        forged = json.loads(json.dumps(receipt))
        forged["authority_receipt"]["outcome"] = "permitted_in_level"  # same hash, different story
        passed, score, output = adapter.score_task(task, forged)
        assert (passed, score) == (False, 0.0)
        assert "hash does not match" in output["error"]
        with pytest.raises(ValueError, match="does not verify"):
            summarize_containment(BenchSuite(version="x", tasks=[task]), [forged])

    def test_the_suite_declares_registered_controls(self) -> None:
        from bernstein.compliance.controls import get_default_registry

        suite = build_authority_suite_v1()
        assert suite.controls
        assert get_default_registry().validate_control_ids(suite.controls) == []
        suite.validate_controls()

    def test_a_case_missing_its_action_is_refused_at_load(self, tmp_path: Path) -> None:
        (tmp_path / "bad.json").write_text(
            json.dumps({"id": "bad", "description": "d", "steps": [], "declared_level": "L0"}), encoding="utf-8"
        )
        with pytest.raises(ValueError, match=r"bad\.json.* is malformed.*attempted_action"):
            build_authority_suite_v1(cases_dir=tmp_path)


# ===========================================================================
# AC-6: End-to-End Runner, Signing, and Offline Verifier
# ===========================================================================


class TestAuthoritySuiteEndToEnd:
    """Test bench run -> bundle -> sign -> verify pipeline on authority-v1."""

    def test_end_to_end_verification(self, tmp_path: Path) -> None:
        suite = build_authority_suite_v1()
        adapter = CompliantEvalAdapter(eval_mode=True)
        runner = BenchRunner(suite=suite, adapter=adapter, scheduler_config={"mode": "eval_governed"})
        bundle = runner.run()

        signer = StubSigner()
        signed_bundle = signer.sign(bundle)

        bundle_file = tmp_path / "authority_bundle.json"
        signed_bundle.save(bundle_file)

        loaded_bundle = SubmissionBundle.load(bundle_file)
        verifier = BenchVerifier(suite=suite, adapter=adapter)
        result = verifier.verify(loaded_bundle)

        assert result.status == VerificationStatus.MATCH
        assert result.passed is True


# ===========================================================================
# AC-7: CLI Command Integration
# ===========================================================================


class TestAuthorityCLI:
    """Test running authority-v1 via Click bench_cli."""

    def test_cli_run_authority_v1(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("BERNSTEIN_EVAL_UNCONSTRAINED", "1")
        runner = CliRunner()
        out_path = tmp_path / "authority_out.json"

        result = runner.invoke(
            bench_group,
            ["run", "authority-v1", "--out", str(out_path), "--stub-signer"],
        )
        assert result.exit_code == 0, result.output
        assert "Suite       : authority-v1" in result.output
        assert "Pass rate   : 100.0%" in result.output
        assert out_path.exists()

    def test_cli_verify_replays_an_authority_bundle(self, tmp_path: Path) -> None:
        """Through the installed entry point, run then verify, the way a user does."""
        from bernstein.cli.main import cli

        runner = CliRunner()
        out_path = tmp_path / "authority_out.json"
        assert (
            runner.invoke(cli, ["bench", "run", "authority-v1", "--out", str(out_path), "--stub-signer"]).exit_code == 0
        )
        result = runner.invoke(cli, ["bench", "verify", str(out_path), "--suite", "authority-v1"])
        assert result.exit_code == 0, result.output
        assert "MATCH" in result.output


def test_a_declared_level_override_groups_every_receipt_under_that_level() -> None:
    """Under a scheduler ``declared_level`` override, per-level rates report the
    run as declared: every receipt lands in that one level's bucket and the
    others read the empty-bucket default. The overall rate is unaffected. This
    is the documented reading, pinned so the override's effect on the report is
    not a surprise."""
    suite = build_authority_suite_v1()
    adapter = CompliantEvalAdapter(eval_mode=True)
    receipts = [adapter.run_task(task, {"declared_level": "L2"}) for task in suite.tasks]
    assert all(AuthorityReceipt.from_dict(r["authority_receipt"]).declared_level == "L2" for r in receipts)

    summary = summarize_containment(suite, receipts)
    rates = summary.per_level_containment_rate
    assert rates["L2"] == summary.overall_containment_rate
    assert rates["L0"] == rates["L1"] == rates["L3"] == rates["L4"] == 1.0


def test_a_malformed_case_file_is_refused_with_its_name(tmp_path: Path) -> None:
    """The 'refused at load' promise names the offending file, not a raw traceback."""
    from bernstein.eval.bench.authority_suite import load_authority_cases

    (tmp_path / "good.json").write_text(
        json.dumps(
            {
                "id": "l0_ok",
                "description": "ok",
                "steps": ["read"],
                "declared_level": "L0",
                "attempted_action": {"name": "write_file", "category": "file_write", "required_level": "L1"},
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "bad.json").write_text('{"id": "x", "description": "no action"}', encoding="utf-8")
    with pytest.raises(ValueError, match=r"bad\.json.* is malformed"):
        load_authority_cases(tmp_path)
