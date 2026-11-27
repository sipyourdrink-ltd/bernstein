"""A scorecard compares only authenticated, replay-verified baselines (#5902)."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from click.testing import CliRunner
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519

from bernstein.eval.bench.bundle import SubmissionBundle
from bernstein.eval.bench.ci import evaluate_ci_scorecard
from bernstein.eval.bench.runner import BenchRunner, MockReplayAdapter
from bernstein.eval.bench.signer import AgentCardSigner
from bernstein.eval.bench.suite import BenchSuite, BenchTask
from bernstein.eval.bench.verifier import BenchVerifier, verify_signature


def _keypair() -> tuple[bytes, bytes]:
    private = ed25519.Ed25519PrivateKey.generate()
    return (
        private.private_bytes(
            serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
        ),
        private.public_key().public_bytes(serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo),
    )


@dataclass
class SignedBaseline:
    suite: BenchSuite
    bundle: SubmissionBundle
    signer: AgentCardSigner
    trusted_keys: dict[str, bytes]
    verifier: BenchVerifier


@pytest.fixture
def baseline() -> SignedBaseline:
    suite = BenchSuite(
        version="scorecard-signature-v1",
        tasks=[BenchTask(id=task_id, description=task_id, steps=(), assertions=()) for task_id in ("one", "two")],
    )
    private, public = _keypair()
    signer = AgentCardSigner(private, public)
    bundle = signer.sign(BenchRunner(suite=suite, adapter=MockReplayAdapter(), scheduler_config={}).run())
    trusted = {bundle.signer_fingerprint: public}
    return SignedBaseline(suite, bundle, signer, trusted, BenchVerifier(suite, MockReplayAdapter(), trusted))


def test_trusted_install_baseline_produces_verified_scorecard(baseline: SignedBaseline) -> None:
    assert verify_signature(baseline.bundle, baseline.trusted_keys) == ""
    assert baseline.verifier.verify(baseline.bundle).passed

    scorecard = evaluate_ci_scorecard(
        baseline.bundle, baseline.suite, baseline.bundle, baseline.verifier, trusted_keys=baseline.trusted_keys
    )

    assert scorecard.conclusion == "success"
    assert scorecard.pass_rate_delta == 0.0
    assert scorecard.baseline_bundle_hash == baseline.bundle.bundle_hash()


@pytest.mark.parametrize("trust", ["matching", "missing", "wrong"])
def test_run_cli_compares_install_baseline_only_with_matching_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, baseline: SignedBaseline, trust: str
) -> None:
    from bernstein.eval.bench import bench_cli

    monkeypatch.setattr(bench_cli, "_get_suite", lambda _name: baseline.suite)
    monkeypatch.setattr(bench_cli, "_resolve_adapter", lambda _suite: MockReplayAdapter())
    stored = tmp_path / "baseline.json"
    baseline.bundle.save(stored)
    args = ["run", "fixture", "--stub-signer", "--out", str(tmp_path / "current.json"), "--baseline", str(stored)]
    if trust != "missing":
        public = next(iter(baseline.trusted_keys.values())) if trust == "matching" else _keypair()[1]
        key_path = tmp_path / "trusted.pem"
        key_path.write_bytes(public)
        args += ["--trusted-key", f"{baseline.bundle.signer_fingerprint}={key_path}"]

    result = CliRunner().invoke(bench_cli.bench_group, args)
    assert result.exit_code == 0, result.output
    if trust == "matching":
        assert "Benchmark passed successfully" in result.output
    else:
        assert "NEUTRAL" in result.output
        assert "trusted public key" in result.output
        assert "Benchmark passed successfully" not in result.output


def test_run_cli_refuses_a_trusted_key_without_a_baseline(tmp_path: Path) -> None:
    from bernstein.eval.bench.bench_cli import bench_group

    target = tmp_path / "no-run.json"
    result = CliRunner().invoke(
        bench_group, ["run", "unused", "--trusted-key", "fingerprint=unused.pem", "--out", str(target)]
    )
    assert result.exit_code == 1
    assert "--trusted-key requires --baseline" in result.output
    assert not target.exists()


def test_trusted_install_baseline_detects_score_regression(baseline: SignedBaseline) -> None:
    first, second = baseline.bundle.task_results
    current = replace(baseline.bundle, task_results=[first, replace(second, passed=False, score=0.0)])

    scorecard = evaluate_ci_scorecard(
        current, baseline.suite, baseline.bundle, baseline.verifier, trusted_keys=baseline.trusted_keys
    )

    assert scorecard.conclusion == "failure"
    assert scorecard.pass_rate_delta == -0.5
    assert scorecard.score_delta is not None and scorecard.score_delta < 0


@pytest.mark.parametrize("trust", ["missing", "empty", "unknown"])
def test_install_baseline_without_trusted_key_stays_neutral(baseline: SignedBaseline, trust: str) -> None:
    keys = (
        None
        if trust == "missing"
        else {}
        if trust == "empty"
        else {"someone-else": next(iter(baseline.trusted_keys.values()))}
    )
    # A caller's replay-only verifier must not disable the scorecard's authentication.
    replay_only = BenchVerifier(baseline.suite, MockReplayAdapter(), require_signature=False)
    assert replay_only.verify(baseline.bundle).passed

    scorecard = evaluate_ci_scorecard(baseline.bundle, baseline.suite, baseline.bundle, replay_only, trusted_keys=keys)

    assert scorecard.conclusion == "neutral"
    assert scorecard.pass_rate_delta is None
    assert "trusted public key" in scorecard.summary


def test_install_baseline_with_wrong_trusted_key_stays_neutral(baseline: SignedBaseline) -> None:
    _, wrong_public = _keypair()
    scorecard = evaluate_ci_scorecard(
        baseline.bundle,
        baseline.suite,
        baseline.bundle,
        baseline.verifier,
        trusted_keys={baseline.bundle.signer_fingerprint: wrong_public},
    )

    assert scorecard.conclusion == "neutral"
    assert "does not verify" in scorecard.summary


def test_tampered_install_baseline_stays_neutral_after_hash_recomputation(baseline: SignedBaseline) -> None:
    altered = replace(baseline.bundle, submitted_at=baseline.bundle.submitted_at + 1)
    rebuilt = SubmissionBundle.from_dict({**altered.to_dict(), "bundle_hash": altered.bundle_hash()})
    assert rebuilt.signature == baseline.bundle.signature
    assert verify_signature(rebuilt, baseline.trusted_keys) != ""
    replay_only = BenchVerifier(baseline.suite, MockReplayAdapter(), require_signature=False)
    assert replay_only.verify(rebuilt).passed

    scorecard = evaluate_ci_scorecard(
        baseline.bundle, baseline.suite, rebuilt, replay_only, trusted_keys=baseline.trusted_keys
    )

    assert scorecard.conclusion == "neutral"
    assert "does not verify" in scorecard.summary


def test_malformed_serialized_signer_fingerprint_stays_neutral(baseline: SignedBaseline) -> None:
    raw = {**baseline.bundle.to_dict(), "signer_fingerprint": ["malformed"]}
    malformed = SubmissionBundle.from_dict(raw)

    scorecard = evaluate_ci_scorecard(baseline.bundle, baseline.suite, malformed, baseline.verifier)

    assert scorecard.conclusion == "neutral"
    assert scorecard.pass_rate_delta is None
    assert "signature verification raised TypeError" in scorecard.summary


def test_malformed_install_signature_stays_neutral(baseline: SignedBaseline) -> None:
    malformed = replace(baseline.bundle, signature="not-a-detached-jws")
    scorecard = evaluate_ci_scorecard(
        baseline.bundle, baseline.suite, malformed, baseline.verifier, trusted_keys=baseline.trusted_keys
    )

    assert scorecard.conclusion == "neutral"
    assert "does not verify" in scorecard.summary


@pytest.mark.parametrize("missing_verifier", [False, True])
def test_authenticated_baseline_still_requires_receipt_verification(
    baseline: SignedBaseline, missing_verifier: bool
) -> None:
    first, second = baseline.bundle.task_results
    damaged = baseline.signer.sign(
        replace(baseline.bundle, task_results=[replace(first, stored_receipt_hash="not-the-receipt-hash"), second])
    )
    assert verify_signature(damaged, baseline.trusted_keys) == ""
    assert not baseline.verifier.verify(damaged).passed

    scorecard = evaluate_ci_scorecard(
        baseline.bundle,
        baseline.suite,
        damaged,
        None if missing_verifier else baseline.verifier,
        trusted_keys=baseline.trusted_keys,
    )

    assert scorecard.conclusion == "neutral"
    assert "No verifier" in scorecard.summary if missing_verifier else "unverifiable" in scorecard.summary
