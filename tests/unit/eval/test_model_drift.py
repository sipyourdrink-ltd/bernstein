"""Model drift probe: signed, chain-anchored observations (issue #5041)."""

from __future__ import annotations

import json
from dataclasses import replace
from typing import TYPE_CHECKING, Any

import pytest

from bernstein.core.lineage import ModelRef
from bernstein.core.security.audit_chain import (
    EVENT_MODEL_DRIFT_OBSERVATION,
    AuditChainStore,
)
from bernstein.eval.baseline import EvalBaseline
from bernstein.eval.bench.runner import MockReplayAdapter
from bernstein.eval.bench.suite import BenchSuite, BenchTask
from bernstein.eval.model_drift import (
    COVERAGE_FULL,
    COVERAGE_PARTIAL,
    CaseDelta,
    ComparisonStatus,
    DriftObservation,
    DriftObservationVerifier,
    DriftProbe,
    DriftVerificationStatus,
    StubDriftSigner,
    baseline_fingerprint,
    record_observation,
)

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.eval.bench.suite import BenchTask as _BenchTask


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _task(task_id: str) -> BenchTask:
    return BenchTask(
        id=task_id,
        description=f"probe case {task_id}",
        steps=(f"do {task_id}",),
        assertions=({"kind": "noop", "case": task_id},),
        category="drift",
    )


@pytest.fixture
def suite() -> BenchSuite:
    return BenchSuite(version="drift-v1", tasks=[_task("alpha"), _task("beta"), _task("gamma")])


@pytest.fixture
def baseline() -> EvalBaseline:
    return EvalBaseline(
        score=1.0,
        components={"alpha": 1.0, "beta": 1.0, "gamma": 1.0},
        timestamp="2026-01-01T00:00:00+00:00",
        config_hash="cfg-fixed",
    )


@pytest.fixture
def model_ref() -> ModelRef:
    return ModelRef(
        provider="anthropic",
        model_requested="stable-alias",
        model_reported="stable-alias-20260101",
        version="20260101",
    )


class FailingAdapter(MockReplayAdapter):
    """Mock adapter whose named tasks fail, so a delta is non-zero."""

    def __init__(self, failing: frozenset[str]) -> None:
        self._failing = failing

    def score_task(self, task: _BenchTask, receipt: dict[str, Any]) -> tuple[bool, float, dict[str, Any]]:
        if task.id in self._failing:
            return False, 0.0, {"note": "mock: assertion unmet"}
        return True, 1.0, {"note": "mock: all assertions satisfied"}


def _probe(suite: BenchSuite, model_ref: ModelRef, adapter: MockReplayAdapter | None = None) -> DriftProbe:
    return DriftProbe(
        suite=suite,
        adapter=adapter or MockReplayAdapter(),
        model_ref=model_ref,
        scheduler_config={"parallelism": 1},
        observed_at=1_700_000_000.0,
    )


# ---------------------------------------------------------------------------
# 1. The probe result is a signed, chain-anchored observation
# ---------------------------------------------------------------------------


def test_probe_result_is_a_signed_chain_anchored_observation(
    suite: BenchSuite,
    baseline: EvalBaseline,
    model_ref: ModelRef,
    tmp_path: Path,
) -> None:
    chain = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
    observation = _probe(suite, model_ref).run(baseline=baseline)

    signed, event = record_observation(chain, observation, StubDriftSigner())

    # Signed: the signature covers the observation hash, which commits to the
    # model ref, the suite, the baseline and every per-case outcome.
    assert signed.signature
    assert signed.signer_fingerprint == StubDriftSigner.fingerprint()
    assert signed.signature == StubDriftSigner.expected_signature(signed)

    # Chain-anchored: the observation names the chain head it landed on, and
    # the chain event names the observation.
    assert event.event_type == EVENT_MODEL_DRIFT_OBSERVATION
    assert event.resource_id == signed.observation_hash()
    assert signed.prev_chain_digest == event.details["prev_chain_digest"]
    assert event.details["model_requested"] == "stable-alias"
    assert event.details["model_reported"] == "stable-alias-20260101"
    assert event.details["suite_hash"] == suite.suite_hash
    assert event.details["baseline_hash"] == baseline_fingerprint(baseline)
    assert event.details["coverage"] == COVERAGE_FULL

    ok, errors = chain.verify()
    assert ok, errors


# ---------------------------------------------------------------------------
# 2. The comparison is deterministic
# ---------------------------------------------------------------------------


def test_comparison_is_deterministic_given_the_same_suite_baseline_and_outputs(
    suite: BenchSuite,
    baseline: EvalBaseline,
    model_ref: ModelRef,
) -> None:
    adapter = FailingAdapter(frozenset({"beta"}))
    first = _probe(suite, model_ref, adapter=FailingAdapter(frozenset({"beta"}))).run(baseline=baseline)
    second = _probe(suite, model_ref, adapter=adapter).run(baseline=baseline)

    assert first.comparison.to_dict() == second.comparison.to_dict()
    assert first.observation_hash() == second.observation_hash()
    assert first.comparison.status is ComparisonStatus.COMPARABLE
    assert first.comparison.aggregate_delta == pytest.approx(-1 / 3)
    assert [cd.case_id for cd in first.comparison.case_deltas] == ["alpha", "beta", "gamma"]
    assert first.comparison.case_deltas[1].delta == pytest.approx(-1.0)


def test_baseline_missing_a_case_that_ran_is_incomparable_rather_than_a_delta(
    suite: BenchSuite,
    model_ref: ModelRef,
) -> None:
    partial_baseline = EvalBaseline(
        score=1.0,
        components={"alpha": 1.0, "beta": 1.0},
        timestamp="2026-01-01T00:00:00+00:00",
        config_hash="cfg-fixed",
    )
    observation = _probe(suite, model_ref).run(baseline=partial_baseline)

    assert observation.comparison.status is ComparisonStatus.INCOMPARABLE
    assert observation.comparison.case_deltas == ()
    assert observation.comparison.aggregate_delta is None
    assert "gamma" in observation.comparison.detail


# ---------------------------------------------------------------------------
# 3. The observation recomputes from the bundle alone
# ---------------------------------------------------------------------------


def test_observation_can_be_recomputed_from_the_bundle_alone(
    suite: BenchSuite,
    baseline: EvalBaseline,
    model_ref: ModelRef,
    tmp_path: Path,
) -> None:
    observation = StubDriftSigner().sign(
        _probe(suite, model_ref, adapter=FailingAdapter(frozenset({"gamma"}))).run(baseline=baseline)
    )
    path = tmp_path / "observation.json"
    observation.save(path)

    # Reload from the file alone: no suite object, no baseline file, no
    # database, no adapter, no network.
    reloaded = DriftObservation.load(path)
    result = DriftObservationVerifier().verify(reloaded)

    assert result.status is DriftVerificationStatus.MATCH, result.detail
    assert result.recomputed_comparison is not None
    assert result.recomputed_comparison.to_dict() == observation.comparison.to_dict()


def test_a_fabricated_delta_is_caught_by_offline_recompute(
    suite: BenchSuite,
    baseline: EvalBaseline,
    model_ref: ModelRef,
) -> None:
    """The sealed delta is a claim; the verifier re-derives it from the bundle."""
    honest = _probe(suite, model_ref, adapter=FailingAdapter(frozenset({"gamma"}))).run(baseline=baseline)
    assert honest.comparison.aggregate_delta is not None
    assert honest.comparison.aggregate_delta < 0

    clean_deltas = tuple(
        CaseDelta(case_id=cd.case_id, baseline_score=cd.baseline_score, observed_score=1.0, delta=0.0)
        for cd in honest.comparison.case_deltas
    )
    fabricated = StubDriftSigner().sign(
        replace(
            honest,
            comparison=replace(honest.comparison, case_deltas=clean_deltas, aggregate_delta=0.0),
        )
    )

    verdict = DriftObservationVerifier().verify(fabricated)
    assert verdict.status is DriftVerificationStatus.DIVERGED
    assert "gamma" in verdict.detail


def test_an_edited_observation_file_is_refused_at_load(
    suite: BenchSuite,
    baseline: EvalBaseline,
    model_ref: ModelRef,
    tmp_path: Path,
) -> None:
    observation = StubDriftSigner().sign(
        _probe(suite, model_ref, adapter=FailingAdapter(frozenset({"gamma"}))).run(baseline=baseline)
    )
    path = tmp_path / "observation.json"
    observation.save(path)

    raw = json.loads(path.read_text(encoding="utf-8"))
    for result in raw["bundle"]["task_results"]:
        if result["task_id"] == "gamma":
            result["passed"] = True
            result["score"] = 1.0
    path.write_text(json.dumps(raw, indent=2, sort_keys=True), encoding="utf-8")

    with pytest.raises(ValueError, match="hash mismatch"):
        DriftObservation.load(path)


def test_an_unsigned_observation_never_verifies(
    suite: BenchSuite,
    baseline: EvalBaseline,
    model_ref: ModelRef,
) -> None:
    observation = _probe(suite, model_ref).run(baseline=baseline)
    verdict = DriftObservationVerifier().verify(observation)
    assert verdict.status is DriftVerificationStatus.UNSIGNED


# ---------------------------------------------------------------------------
# 4. A sample never reads as full coverage
# ---------------------------------------------------------------------------


def test_partial_suite_run_records_which_cases_ran(
    suite: BenchSuite,
    baseline: EvalBaseline,
    model_ref: ModelRef,
    tmp_path: Path,
) -> None:
    chain = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
    observation = _probe(suite, model_ref).run(
        baseline=baseline,
        case_ids=["alpha", "gamma"],
        sampling_reason="beta needs a network fixture unavailable on the probe host",
    )

    assert observation.coverage == COVERAGE_PARTIAL
    assert observation.cases_declared == ("alpha", "beta", "gamma")
    assert observation.cases_ran == ("alpha", "gamma")
    assert observation.sampling_reason
    # The comparison is scoped to what actually ran, not to the whole suite.
    assert [cd.case_id for cd in observation.comparison.case_deltas] == ["alpha", "gamma"]
    assert [tr.task_id for tr in observation.bundle.task_results] == ["alpha", "gamma"]
    # The bundle still names the declared suite, so the subset is legible as a
    # subset of a known suite rather than as a suite of its own.
    assert observation.bundle.suite_hash == suite.suite_hash

    signed, event = record_observation(chain, observation, StubDriftSigner())
    assert event.details["coverage"] == COVERAGE_PARTIAL
    assert event.details["cases_ran_count"] == 2
    assert event.details["cases_declared_count"] == 3
    assert event.details["sampling_reason"] == observation.sampling_reason
    assert DriftObservationVerifier().verify(signed).status is DriftVerificationStatus.MATCH


def test_partial_run_without_a_stated_reason_is_refused(
    suite: BenchSuite,
    baseline: EvalBaseline,
    model_ref: ModelRef,
) -> None:
    with pytest.raises(ValueError, match="sampling_reason"):
        _probe(suite, model_ref).run(baseline=baseline, case_ids=["alpha"])


def test_coverage_label_must_agree_with_the_cases_that_ran(
    suite: BenchSuite,
    baseline: EvalBaseline,
    model_ref: ModelRef,
) -> None:
    observation = _probe(suite, model_ref).run(
        baseline=baseline,
        case_ids=["alpha", "gamma"],
        sampling_reason="beta needs a network fixture unavailable on the probe host",
    )
    mislabelled = StubDriftSigner().sign(
        DriftObservation(
            model_ref=observation.model_ref,
            suite_hash=observation.suite_hash,
            suite_version=observation.suite_version,
            cases_declared=observation.cases_declared,
            cases_ran=observation.cases_ran,
            sampling_reason="",
            coverage=COVERAGE_FULL,
            baseline=observation.baseline,
            bundle=observation.bundle,
            comparison=observation.comparison,
            observed_at=observation.observed_at,
        )
    )

    verdict = DriftObservationVerifier().verify(mislabelled)
    assert verdict.status is DriftVerificationStatus.COVERAGE_UNDECLARED
    assert "partial" in verdict.detail


def test_an_unknown_case_id_is_refused(
    suite: BenchSuite,
    baseline: EvalBaseline,
    model_ref: ModelRef,
) -> None:
    with pytest.raises(ValueError, match="not in suite"):
        _probe(suite, model_ref).run(
            baseline=baseline,
            case_ids=["alpha", "delta"],
            sampling_reason="host constraint",
        )


def test_an_install_identity_signature_verifies_against_the_trusted_key(
    suite: BenchSuite,
    baseline: EvalBaseline,
    model_ref: ModelRef,
) -> None:
    from bernstein.core.security.agent_card_signer import generate_ed25519_keypair
    from bernstein.eval.model_drift import InstallIdentityDriftSigner

    private_pem, public_pem = generate_ed25519_keypair()
    signer = InstallIdentityDriftSigner(private_key_pem=private_pem, public_key_pem=public_pem)
    observation = signer.sign(_probe(suite, model_ref).run(baseline=baseline))

    trusted = DriftObservationVerifier(trusted_keys={signer.fingerprint(): public_pem})
    assert trusted.verify(observation).status is DriftVerificationStatus.MATCH
    # An unresolvable fingerprint fails closed rather than passing unverified.
    assert DriftObservationVerifier().verify(observation).status is DriftVerificationStatus.UNSIGNED
