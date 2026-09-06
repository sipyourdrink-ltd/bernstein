"""Tests for the tool-surface risk benchmark suite and verifier integration."""

from __future__ import annotations

from bernstein.eval.bench.bundle import SubmissionBundle, TaskResult
from bernstein.eval.bench.runner import BenchRunner
from bernstein.eval.bench.signer import StubSigner
from bernstein.eval.bench.tool_surface_suite import (
    ToolSurfaceReplayAdapter,
    build_tool_surface_suite,
    get_tool_surface_fixtures,
)
from bernstein.eval.bench.verifier import BenchVerifier, VerificationStatus
from bernstein.mcp.tool_surface import (
    RiskClass,
    evaluate_tool_surface_risk,
    is_approval_forced,
)


def test_ten_fixtures_exist_and_load() -> None:
    fixtures = get_tool_surface_fixtures()
    assert len(fixtures) >= 10
    fixture_ids = {f.server_id for f in fixtures}
    expected_ids = {
        "read_only_local",
        "read_only_public",
        "read_egress",
        "risky_triple_basic",
        "risky_triple_oauth",
        "risky_triple_no_auth",
        "wildcard_no_auth",
        "wildcard_oauth",
        "sensitive_read_no_egress",
        "untrusted_input_no_egress",
    }
    assert expected_ids <= fixture_ids


def test_risky_triple_detection_rate_is_100_percent() -> None:
    fixtures = get_tool_surface_fixtures()
    triple_fixtures = [f for f in fixtures if "risky_triple" in f.server_id]
    assert len(triple_fixtures) == 3

    detected_count = 0
    for f in triple_fixtures:
        receipt = evaluate_tool_surface_risk(f)
        if receipt.has_risky_triple and receipt.risk_class == RiskClass.CRITICAL and receipt.forced_approval is True:
            detected_count += 1

    detection_rate = detected_count / len(triple_fixtures)
    assert detection_rate == 1.0


def test_read_only_fixtures_never_force_approval() -> None:
    fixtures = get_tool_surface_fixtures()
    read_only_fixtures = [f for f in fixtures if "read_only" in f.server_id]
    assert len(read_only_fixtures) >= 2

    for f in read_only_fixtures:
        receipt = evaluate_tool_surface_risk(f)
        assert receipt.forced_approval is False
        assert receipt.has_risky_triple is False
        forced, reason = is_approval_forced(receipt, approver_configured=True)
        assert forced is False
        assert "ALLOWED" in reason


def test_forced_approval_fixtures_deny_by_default_when_no_approver() -> None:
    fixtures = get_tool_surface_fixtures()
    forced_fixtures = [f for f in fixtures if "risky_triple" in f.server_id or "wildcard" in f.server_id]
    assert len(forced_fixtures) >= 5

    for f in forced_fixtures:
        receipt = evaluate_tool_surface_risk(f)
        assert receipt.forced_approval is True
        forced, reason = is_approval_forced(receipt, approver_configured=False)
        assert forced is True
        assert "DENIED" in reason


def test_build_tool_surface_suite_structure() -> None:
    suite = build_tool_surface_suite()
    assert suite.version == "tool-surface-v1"
    assert len(suite.tasks) >= 10
    for task in suite.tasks:
        assert task.category == "tool_surface"
        assert len(task.steps) > 0
        assert len(task.assertions) > 0


def test_bench_runner_executes_suite_and_scores_100() -> None:
    suite = build_tool_surface_suite()
    adapter = ToolSurfaceReplayAdapter()
    runner = BenchRunner(suite=suite, adapter=adapter, scheduler_config={"scheduler": "deterministic"})

    bundle = runner.run()
    signed_bundle = StubSigner().sign(bundle)

    assert signed_bundle.pass_rate == 1.0
    assert signed_bundle.overall_score == 1.0
    assert len(signed_bundle.task_results) == len(suite.tasks)


def test_bench_verifier_matches_and_reports_risk_class_and_capability_hash() -> None:
    suite = build_tool_surface_suite()
    adapter = ToolSurfaceReplayAdapter()
    runner = BenchRunner(suite=suite, adapter=adapter, scheduler_config={"scheduler": "deterministic"})
    bundle = StubSigner().sign(runner.run())

    verifier = BenchVerifier(suite=suite, adapter=adapter)
    result = verifier.verify(bundle)

    assert result.passed is True
    assert result.status == VerificationStatus.MATCH
    report_text = result.report()

    assert "MATCH" in report_text
    assert "risk_class" in report_text
    assert "capability_hash" in report_text


def test_bench_verifier_catches_tampered_capability_receipt() -> None:
    suite = build_tool_surface_suite()
    adapter = ToolSurfaceReplayAdapter()
    runner = BenchRunner(suite=suite, adapter=adapter, scheduler_config={"scheduler": "deterministic"})
    bundle = StubSigner().sign(runner.run())

    # Tamper with the first task result's receipt
    first_result = bundle.task_results[0]
    tampered_receipt = dict(first_result.receipt)
    tampered_receipt["risk_class"] = "TAMPERED_CRITICAL"

    tampered_task_result = TaskResult(
        task_id=first_result.task_id,
        task_hash=first_result.task_hash,
        receipt=tampered_receipt,
        passed=first_result.passed,
        score=first_result.score,
        stored_receipt_hash=first_result.stored_receipt_hash,  # old hash doesn't match new bytes
    )
    tampered_bundle = SubmissionBundle(
        suite_hash=bundle.suite_hash,
        suite_version=bundle.suite_version,
        task_results=[tampered_task_result, *bundle.task_results[1:]],
        scheduler_config=bundle.scheduler_config,
    )

    verifier = BenchVerifier(suite=suite, adapter=adapter)
    result = verifier.verify(tampered_bundle)

    assert result.passed is False
    assert result.status == VerificationStatus.DIVERGED
    assert any(tr.status == VerificationStatus.HASH_MISMATCH for tr in result.task_results)
