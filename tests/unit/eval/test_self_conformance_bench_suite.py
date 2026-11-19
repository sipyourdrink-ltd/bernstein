"""Measured benchmark-envelope conformance through actual offline verification."""

from __future__ import annotations

import pytest

from bernstein.eval.bench.bench_cli import _get_suite, _resolve_adapter

CASES = ("receipt-byte-flip", "signed-result-reorder", "result-deletion", "old-context-receipt", "signer-trust-change")


def test_builtin_suite_covers_five_explicit_bench_envelope_probes():
    suite = _get_suite("self-conformance-bench-v1")
    assert suite.version == "self-conformance-bench-v1"
    assert tuple(task.id for task in suite.tasks) == CASES
    assert suite.measurement_control_ids == ("CTL-ROB-01",)
    assert not getattr(_resolve_adapter(suite), "synthetic", False)


@pytest.mark.parametrize("case", CASES)
def test_actual_verifier_accepts_control_and_rejects_mutation(case):
    suite = _get_suite("self-conformance-bench-v1")
    adapter = _resolve_adapter(suite)
    task = next(task for task in suite.tasks if task.id == case)
    receipt = adapter.run_task(task, {})
    passed, score, evidence = adapter.score_task(task, receipt)
    assert passed and score == 1.0
    assert evidence["control_status"] == "MATCH"
    assert evidence["probe_status"] != "MATCH"


@pytest.mark.parametrize("case", CASES)
def test_always_accepting_verifier_never_earns_conformance(case, monkeypatch):
    from types import SimpleNamespace

    from bernstein.eval.bench import self_conformance_suite as module

    suite = _get_suite("self-conformance-bench-v1")
    adapter = _resolve_adapter(suite)
    task = next(item for item in suite.tasks if item.id == case)
    receipt = adapter.run_task(task, {})
    monkeypatch.setattr(
        module.BenchVerifier,
        "verify",
        lambda *a: SimpleNamespace(status=module.VerificationStatus.MATCH, task_results=[]),
    )
    passed, score, _ = adapter.score_task(task, receipt)
    assert not passed and score == 0.0


@pytest.mark.parametrize("case", CASES)
def test_broken_positive_control_never_earns_conformance(case, monkeypatch):
    from bernstein.eval.bench import self_conformance_suite as module

    suite = _get_suite("self-conformance-bench-v1")
    adapter = _resolve_adapter(suite)
    task = next(item for item in suite.tasks if item.id == case)
    receipt = adapter.run_task(task, {})
    monkeypatch.setattr(module._ContextAdapter, "score_task", lambda *a: (False, 0.0, {}))
    passed, score, evidence = adapter.score_task(task, receipt)
    assert not passed and score == 0.0
    assert evidence["control_status"] != "MATCH"


@pytest.mark.parametrize("case", CASES)
def test_arbitrary_rejection_payload_cannot_replace_named_probe(case):
    suite = _get_suite("self-conformance-bench-v1")
    adapter = _resolve_adapter(suite)
    task = next(item for item in suite.tasks if item.id == case)
    receipt = adapter.run_task(task, {})
    receipt["probe"]["signature"] = "arbitrary invalid signature"
    passed, score, _ = adapter.score_task(task, receipt)
    assert not passed and score == 0.0


def test_receipts_reproduce_without_timestamp_normalization():
    suite = _get_suite("self-conformance-bench-v1")
    adapter = _resolve_adapter(suite)
    for task in suite.tasks:
        assert adapter.run_task(task, {}) == adapter.run_task(task, {})


def test_unknown_or_modified_task_is_rejected():
    from dataclasses import replace

    suite = _get_suite("self-conformance-bench-v1")
    adapter = _resolve_adapter(suite)
    with pytest.raises(ValueError, match="canonical"):
        adapter.run_task(replace(suite.tasks[0], description="different semantics"), {})


def test_boolean_schema_cannot_alias_integer_schema():
    suite = _get_suite("self-conformance-bench-v1")
    adapter = _resolve_adapter(suite)
    receipt = adapter.run_task(suite.tasks[0], {})
    receipt["schema"] = True
    assert adapter.score_task(suite.tasks[0], receipt)[:2] == (False, 0.0)


def test_real_cli_signed_bundle_replays_offline_without_running_tasks(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from bernstein.eval.bench import self_conformance_suite as module
    from bernstein.eval.bench import signer as signer_module
    from bernstein.eval.bench.bench_cli import bench_group

    # Explicit PUBLIC fixture key in this test, never the owner's install key.
    signer = module._fixture_signer()
    monkeypatch.setattr(signer_module, "AgentCardSigner", lambda: signer)
    output = tmp_path / "bundle.json"
    public_key = tmp_path / "fixture.pub"
    public_key.write_bytes(signer.public_key_pem())
    runner = CliRunner()
    run = runner.invoke(bench_group, ["run", "self-conformance-bench-v1", "--out", str(output)])
    assert run.exit_code == 0, run.output
    assert output.is_file()
    from bernstein.eval.bench.bundle import SubmissionBundle

    measured = SubmissionBundle.load(output)
    assert measured.pass_rate == measured.overall_score == 1.0
    assert len(measured.task_results) == 5

    def refuse_execution(*args, **kwargs):
        raise AssertionError("offline verifier must score stored receipts only")

    monkeypatch.setattr(module.SelfConformanceBenchReplayAdapter, "run_task", refuse_execution)
    verified = runner.invoke(
        bench_group,
        [
            "verify",
            str(output),
            "--suite",
            "self-conformance-bench-v1",
            "--trusted-key",
            f"{signer.fingerprint()}={public_key}",
        ],
    )
    assert verified.exit_code == 0, verified.output
    assert "MATCH" in verified.output
    untrusted = runner.invoke(bench_group, ["verify", str(output), "--suite", "self-conformance-bench-v1"])
    assert untrusted.exit_code != 0
