"""
TDD tests for the pass^k reliability floor (issue #2933).

Acceptance criteria covered:

AC-1  --reliability k runs each task k times with byte-identical
      coordination across attempts (empirical: coordination fields of the
      k receipts are identical; only model-output fields differ).

AC-2  The result reports both pass@1 and pass^k, with pass^k as the
      headline floor.

AC-3  The reliability receipt is signed and embeds all k per-attempt run
      receipts; a verifier replays all k attempts offline and recomputes
      the identical floor.

AC-4  A fabricated floor (claimed pass^k not matching the replayed
      attempts) is rejected; stripping the replay substrate makes the
      floor unverifiable (artefact-as-proof).

AC-5  reliability-check proves one attempt replays byte-identically, so a
      low floor is attributable to model sampling and not hidden
      coordination non-determinism.

AC-6  Docs shipped in the same PR.

All tests use hermetic mock adapters — no network, no real agents.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from bernstein.eval.bench.bundle import TaskResult
from bernstein.eval.bench.reliability import (
    InstallIdentityReliabilitySigner,
    ReliabilityReceipt,
    ReliabilityRunner,
    ReliabilityVerificationStatus,
    ReliabilityVerifier,
    StubReliabilitySigner,
    coordination_hash,
    reliability_check,
)
from bernstein.eval.bench.runner import MockReplayAdapter, StochasticMockReplayAdapter
from bernstein.eval.bench.suite import BenchSuite, BenchTask

# ---------------------------------------------------------------------------
# Fixtures and helpers
# ---------------------------------------------------------------------------


@pytest.fixture()
def simple_suite() -> BenchSuite:
    """A minimal two-task suite for fast tests."""
    return BenchSuite(
        version="test-v1",
        tasks=[
            BenchTask(
                id="task_a",
                description="Task A",
                steps=("step 1", "step 2"),
                assertions=({"kind": "exists"},),
                category="cat1",
            ),
            BenchTask(
                id="task_b",
                description="Task B",
                steps=("step 1",),
                assertions=({"kind": "syntax_valid"},),
                category="cat2",
            ),
        ],
    )


class NonDeterministicCoordinationAdapter:
    """
    Deliberately broken adapter: a *coordination* event drifts per call.

    This is the failure mode the reliability floor must detect — if
    coordination is not held fixed, a low pass^k measures scheduler noise,
    not model sampling.
    """

    def __init__(self) -> None:
        self._inner = MockReplayAdapter()
        self._calls = 0

    def run_task(self, task: BenchTask, scheduler_config: dict[str, Any]) -> dict[str, Any]:
        receipt = self._inner.run_task(task, scheduler_config)
        self._calls += 1
        receipt["events"] = [
            *receipt["events"],
            {"seq": len(receipt["events"]), "kind": "scheduler.retry", "attempt_budget": self._calls},
        ]
        return receipt

    def score_task(self, task: BenchTask, receipt: dict[str, Any]) -> tuple[bool, float, dict[str, Any]]:
        return self._inner.score_task(task, receipt)


def _verdicts(seed: int, task: BenchTask, k: int) -> list[bool]:
    """Predict the stochastic mock's verdicts for (seed, task) without running it."""
    task_hash = task.content_hash()
    return [
        StochasticMockReplayAdapter.sample_passes(StochasticMockReplayAdapter.derive_sample(seed, task_hash, i))
        for i in range(k)
    ]


def _find_seed(task: BenchTask, k: int, predicate: Callable[[list[bool]], bool]) -> int:
    """Find a seed whose predicted verdicts satisfy *predicate* (deterministic)."""
    for seed in range(500):
        if predicate(_verdicts(seed, task, k)):
            return seed
    raise AssertionError("no seed satisfying the predicate in range(500) — widen the search")


def _make_receipt(suite: BenchSuite, adapter: Any, k: int) -> ReliabilityReceipt:
    runner = ReliabilityRunner(suite=suite, adapter=adapter, scheduler_config={"scheduler": "test"}, k=k)
    return runner.run()


def _signed(receipt: ReliabilityReceipt) -> ReliabilityReceipt:
    return StubReliabilitySigner().sign(receipt)


# ===========================================================================
# AC-1 — Fixed-coordination repetition
# ===========================================================================


class TestFixedCoordinationRepetition:
    def test_k_attempts_share_identical_coordination_receipts(self, simple_suite: BenchSuite) -> None:
        """With the deterministic adapter, all k attempts share one coordination hash."""
        receipt = _make_receipt(simple_suite, MockReplayAdapter(), k=3)
        assert receipt.coordination_ok
        for tr in receipt.task_results:
            assert tr.coordination_identical
            hashes = {coordination_hash(attempt.receipt) for attempt in tr.attempts}
            assert len(hashes) == 1
            assert tr.coordination_hash in hashes

    def test_deterministic_adapter_yields_identical_attempt_receipts(self, simple_suite: BenchSuite) -> None:
        """Full byte-identity across attempts, not just coordination identity."""
        receipt = _make_receipt(simple_suite, MockReplayAdapter(), k=3)
        for tr in receipt.task_results:
            first = tr.attempts[0]
            for attempt in tr.attempts[1:]:
                assert attempt.receipt == first.receipt
                assert attempt.stored_receipt_hash == first.stored_receipt_hash

    def test_stochastic_divergence_is_detected_across_attempts(self, simple_suite: BenchSuite) -> None:
        """
        The stochastic mock varies ONLY the model-output payload: attempt
        receipts differ in bytes, coordination stays identical, and the
        mixed verdicts land in the per-task attempt records.
        """
        task = simple_suite.tasks[0]
        seed = _find_seed(task, 3, lambda v: any(v) and not all(v))
        receipt = _make_receipt(simple_suite, StochasticMockReplayAdapter(seed=seed), k=3)
        tr = next(t for t in receipt.task_results if t.task_id == task.id)

        # Full receipt bytes differ across attempts (the samples vary)…
        receipt_hashes = {attempt.stored_receipt_hash for attempt in tr.attempts}
        assert len(receipt_hashes) > 1, "stochastic attempts must not be byte-identical"
        # …but coordination is held fixed.
        assert tr.coordination_identical
        assert receipt.coordination_ok
        # And the verdicts really are mixed, matching the predicted schedule.
        assert [a.passed for a in tr.attempts] == _verdicts(seed, task, 3)

    def test_coordination_nondeterminism_is_flagged_by_runner(self, simple_suite: BenchSuite) -> None:
        receipt = _make_receipt(simple_suite, NonDeterministicCoordinationAdapter(), k=2)
        assert not receipt.coordination_ok
        assert any(not tr.coordination_identical for tr in receipt.task_results)


# ===========================================================================
# AC-2 — pass@1 and pass^k, floor as headline
# ===========================================================================


class TestFloorComputation:
    def test_passk_floor_below_pass_at_1_for_flaky_tasks(self, simple_suite: BenchSuite) -> None:
        """A task that passes some but not all attempts drops the floor, not the ceiling."""
        task_a = simple_suite.tasks[0]
        k = 3
        seed = _find_seed(
            task_a,
            k,
            lambda v: any(v) and not all(v),
        )
        receipt = _make_receipt(simple_suite, StochasticMockReplayAdapter(seed=seed), k=k)

        tr_a = next(t for t in receipt.task_results if t.task_id == task_a.id)
        assert tr_a.passed_any and not tr_a.passed_all
        assert receipt.pass_caret_k < receipt.pass_at_1

    def test_floor_never_exceeds_pass_at_1(self, simple_suite: BenchSuite) -> None:
        """pass^k <= pass@1 must hold for any seed (all-of-k implies any-of-k)."""
        for seed in range(10):
            receipt = _make_receipt(simple_suite, StochasticMockReplayAdapter(seed=seed), k=4)
            assert receipt.pass_caret_k <= receipt.pass_at_1

    def test_k_of_one_degenerates_to_pass_at_1(self, simple_suite: BenchSuite) -> None:
        """With k=1, 'any attempt' and 'all attempts' are the same event."""
        receipt = _make_receipt(simple_suite, StochasticMockReplayAdapter(seed=7), k=1)
        assert receipt.pass_caret_k == receipt.pass_at_1

    def test_all_passing_suite_has_floor_equal_to_ceiling(self, simple_suite: BenchSuite) -> None:
        receipt = _make_receipt(simple_suite, MockReplayAdapter(), k=5)
        assert receipt.pass_at_1 == 1.0
        assert receipt.pass_caret_k == 1.0

    def test_k_below_one_rejected(self, simple_suite: BenchSuite) -> None:
        runner = ReliabilityRunner(suite=simple_suite, adapter=MockReplayAdapter(), scheduler_config={}, k=0)
        with pytest.raises(ValueError, match="k must be >= 1"):
            runner.run()


# ===========================================================================
# AC-3 — Signed receipt, embedded attempt receipts, round-trip
# ===========================================================================


class TestReceiptRoundTripAndSigning:
    def test_receipt_embeds_all_k_attempt_receipts(self, simple_suite: BenchSuite) -> None:
        k = 3
        receipt = _make_receipt(simple_suite, MockReplayAdapter(), k=k)
        assert len(receipt.task_results) == len(simple_suite.tasks)
        for tr in receipt.task_results:
            assert len(tr.attempts) == k
            for attempt in tr.attempts:
                assert attempt.receipt, "every attempt must carry its replayable run receipt"

    def test_receipt_save_load_round_trip_preserves_hash(self, simple_suite: BenchSuite, tmp_path: Path) -> None:
        receipt = _signed(_make_receipt(simple_suite, MockReplayAdapter(), k=2))
        path = tmp_path / "reliability.json"
        receipt.save(path)
        loaded = ReliabilityReceipt.load(path)
        assert loaded.receipt_hash() == receipt.receipt_hash()
        assert loaded.signature == receipt.signature
        for orig, restored in zip(receipt.task_results, loaded.task_results, strict=True):
            for a, b in zip(orig.attempts, restored.attempts, strict=True):
                assert a.stored_receipt_hash == b.stored_receipt_hash

    def test_corrupted_receipt_file_rejected_on_load(self, simple_suite: BenchSuite, tmp_path: Path) -> None:
        receipt = _signed(_make_receipt(simple_suite, MockReplayAdapter(), k=2))
        path = tmp_path / "reliability.json"
        receipt.save(path)
        raw = json.loads(path.read_text())
        raw["receipt_hash"] = "deadbeef" * 8
        path.write_text(json.dumps(raw))
        with pytest.raises(ValueError, match="Reliability receipt hash mismatch"):
            ReliabilityReceipt.load(path)

    def test_stub_signing_is_deterministic(self, simple_suite: BenchSuite) -> None:
        receipt = _make_receipt(simple_suite, MockReplayAdapter(), k=2)
        s1 = _signed(receipt)
        s2 = _signed(receipt)
        assert s1.signature == s2.signature != ""
        assert s1.signer_fingerprint == s2.signer_fingerprint != ""

    def test_unsigned_or_signature_stripped_receipt_rejected(self, simple_suite: BenchSuite) -> None:
        adapter = MockReplayAdapter()
        verifier = ReliabilityVerifier(suite=simple_suite, adapter=adapter)

        unsigned = _make_receipt(simple_suite, adapter, k=2)
        assert verifier.verify(unsigned).status == ReliabilityVerificationStatus.UNSIGNED

        stripped = replace(_signed(unsigned), signature="")
        assert verifier.verify(stripped).status == ReliabilityVerificationStatus.UNSIGNED

        no_fingerprint = replace(_signed(unsigned), signer_fingerprint="")
        assert verifier.verify(no_fingerprint).status == ReliabilityVerificationStatus.UNSIGNED

    def test_invalid_stub_signature_rejected(self, simple_suite: BenchSuite) -> None:
        """A stub-fingerprint receipt whose signature doesn't verify is rejected."""
        adapter = MockReplayAdapter()
        verifier = ReliabilityVerifier(suite=simple_suite, adapter=adapter)
        signed = _signed(_make_receipt(simple_suite, adapter, k=2))
        forged = replace(signed, signature="Zm9yZ2Vk")  # base64("forged")
        assert verifier.verify(forged).status == ReliabilityVerificationStatus.UNSIGNED


# ===========================================================================
# AC-3/AC-4 — Offline verification: honest MATCH, fabricated floor rejected
# ===========================================================================


class TestVerifier:
    def test_verifier_recomputes_identical_floor_offline(self, simple_suite: BenchSuite, tmp_path: Path) -> None:
        """An honest receipt survives save/load and the recomputed floor equals the sealed one."""
        task_a = simple_suite.tasks[0]
        seed = _find_seed(task_a, 3, lambda v: any(v) and not all(v))
        adapter = StochasticMockReplayAdapter(seed=seed)
        receipt = _signed(_make_receipt(simple_suite, adapter, k=3))

        path = tmp_path / "reliability.json"
        receipt.save(path)
        loaded = ReliabilityReceipt.load(path)

        verifier = ReliabilityVerifier(suite=simple_suite, adapter=StochasticMockReplayAdapter(seed=seed))
        result = verifier.verify(loaded)
        assert result.status == ReliabilityVerificationStatus.MATCH
        assert result.passed
        assert result.recomputed_pass_at_1 == receipt.pass_at_1
        assert result.recomputed_pass_caret_k == receipt.pass_caret_k

    def test_fabricated_floor_rejected_by_replay(self, simple_suite: BenchSuite) -> None:
        """Inflating the sealed pass^k without touching the attempts is caught."""
        task_a = simple_suite.tasks[0]
        seed = _find_seed(task_a, 3, lambda v: any(v) and not all(v))
        adapter = StochasticMockReplayAdapter(seed=seed)
        honest = _make_receipt(simple_suite, adapter, k=3)
        assert honest.pass_caret_k < 1.0  # guard: there is something to inflate

        inflated = _signed(replace(honest, pass_caret_k=1.0))
        verifier = ReliabilityVerifier(suite=simple_suite, adapter=adapter)
        result = verifier.verify(inflated)
        assert result.status == ReliabilityVerificationStatus.FABRICATED_FLOOR
        assert not result.passed

    def test_flipped_attempt_verdict_rejected(self, simple_suite: BenchSuite) -> None:
        """Flipping one embedded attempt verdict is caught by replaying that attempt."""
        adapter = MockReplayAdapter()
        honest = _make_receipt(simple_suite, adapter, k=2)
        target = honest.task_results[0]
        flipped_attempt = TaskResult(
            task_id=target.attempts[0].task_id,
            task_hash=target.attempts[0].task_hash,
            receipt=target.attempts[0].receipt,
            passed=not target.attempts[0].passed,  # ← flip
            score=0.0,
        )
        tampered_task = replace(target, attempts=[flipped_attempt, *target.attempts[1:]])
        tampered = _signed(replace(honest, task_results=[tampered_task, *honest.task_results[1:]]))

        verifier = ReliabilityVerifier(suite=simple_suite, adapter=adapter)
        result = verifier.verify(tampered)
        assert not result.passed
        statuses = {tr.task_id: tr.status for tr in result.task_results}
        assert statuses[target.task_id] == ReliabilityVerificationStatus.FABRICATED_SCORE

    def test_reliability_receipt_tamper_fails_verification(self, simple_suite: BenchSuite) -> None:
        """A byte-flip inside one embedded attempt receipt is caught via the emit-time hash."""
        adapter = MockReplayAdapter()
        honest = _make_receipt(simple_suite, adapter, k=2)
        target = honest.task_results[0]

        tampered_run_receipt = copy.deepcopy(target.attempts[0].receipt)
        tampered_run_receipt["journal_head"] = "aaaa" * 16  # ← byte-flip, verdict untouched
        tampered_attempt = TaskResult(
            task_id=target.attempts[0].task_id,
            task_hash=target.attempts[0].task_hash,
            receipt=tampered_run_receipt,
            passed=target.attempts[0].passed,
            score=target.attempts[0].score,
            stored_receipt_hash=target.attempts[0].stored_receipt_hash,  # ← pinned emit-time hash
        )
        tampered_task = replace(target, attempts=[tampered_attempt, *target.attempts[1:]])
        tampered = _signed(replace(honest, task_results=[tampered_task, *honest.task_results[1:]]))

        verifier = ReliabilityVerifier(suite=simple_suite, adapter=adapter)
        result = verifier.verify(tampered)
        assert not result.passed
        statuses = {tr.task_id: tr.status for tr in result.task_results}
        assert statuses[target.task_id] == ReliabilityVerificationStatus.HASH_MISMATCH

    def test_stripping_an_attempt_receipt_makes_floor_unverifiable(self, simple_suite: BenchSuite) -> None:
        """Fewer than k embedded attempts (or an emptied receipt) can never verify."""
        adapter = MockReplayAdapter()
        honest = _make_receipt(simple_suite, adapter, k=3)
        target = honest.task_results[0]
        verifier = ReliabilityVerifier(suite=simple_suite, adapter=adapter)

        # Variant 1: drop one attempt entirely.
        short_task = replace(target, attempts=target.attempts[:-1])
        short = _signed(replace(honest, task_results=[short_task, *honest.task_results[1:]]))
        result = verifier.verify(short)
        assert not result.passed
        statuses = {tr.task_id: tr.status for tr in result.task_results}
        assert statuses[target.task_id] == ReliabilityVerificationStatus.MISSING_RECEIPT

        # Variant 2: keep k attempts but empty one run receipt.
        emptied_attempt = TaskResult(
            task_id=target.attempts[0].task_id,
            task_hash=target.attempts[0].task_hash,
            receipt={},  # ← replay substrate removed
            passed=target.attempts[0].passed,
            score=target.attempts[0].score,
        )
        emptied_task = replace(target, attempts=[emptied_attempt, *target.attempts[1:]])
        emptied = _signed(replace(honest, task_results=[emptied_task, *honest.task_results[1:]]))
        result = verifier.verify(emptied)
        assert not result.passed
        statuses = {tr.task_id: tr.status for tr in result.task_results}
        assert statuses[target.task_id] == ReliabilityVerificationStatus.MISSING_RECEIPT

    def test_stripping_a_failing_task_does_not_inflate_floor(self, simple_suite: BenchSuite) -> None:
        """
        Removing a failing task and recomputing the aggregates must be
        rejected: the floor is computed over the full suite or not at all.
        """
        task_a = simple_suite.tasks[0]
        seed = _find_seed(task_a, 3, lambda v: not all(v))
        adapter = StochasticMockReplayAdapter(seed=seed)
        honest = _make_receipt(simple_suite, adapter, k=3)
        failing = next(tr for tr in honest.task_results if not tr.passed_all)

        remaining = [tr for tr in honest.task_results if tr.task_id != failing.task_id]
        # The attacker recomputes the aggregates over the remaining tasks.
        any_rate = sum(1 for tr in remaining if tr.passed_any) / len(remaining)
        all_rate = sum(1 for tr in remaining if tr.passed_all) / len(remaining)
        stripped = _signed(replace(honest, task_results=remaining, pass_at_1=any_rate, pass_caret_k=all_rate))

        verifier = ReliabilityVerifier(suite=simple_suite, adapter=adapter)
        result = verifier.verify(stripped)
        assert not result.passed
        assert result.status == ReliabilityVerificationStatus.MISSING_RECEIPT
        assert failing.task_id in result.detail

    def test_wrong_suite_hash_rejected(self, simple_suite: BenchSuite) -> None:
        adapter = MockReplayAdapter()
        honest = _make_receipt(simple_suite, adapter, k=2)
        forged = _signed(replace(honest, suite_hash="0000" * 16))
        verifier = ReliabilityVerifier(suite=simple_suite, adapter=adapter)
        assert verifier.verify(forged).status == ReliabilityVerificationStatus.HASH_MISMATCH

    def test_coordination_divergence_rejected_by_verifier(self, simple_suite: BenchSuite) -> None:
        """A receipt whose attempts diverge in a coordination field is inadmissible."""
        receipt = _signed(_make_receipt(simple_suite, NonDeterministicCoordinationAdapter(), k=2))
        verifier = ReliabilityVerifier(suite=simple_suite, adapter=MockReplayAdapter())
        result = verifier.verify(receipt)
        assert not result.passed
        diverged = [
            tr for tr in result.task_results if tr.status == ReliabilityVerificationStatus.COORDINATION_DIVERGED
        ]
        assert diverged, "verifier must flag coordination divergence"
        assert "attempt_budget" in diverged[0].detail


# ===========================================================================
# AC-5 — reliability-check: byte-identical replay of one attempt
# ===========================================================================


class TestReliabilityCheck:
    def test_reliability_check_passes_on_deterministic_adapter(self, simple_suite: BenchSuite) -> None:
        receipt = _make_receipt(simple_suite, MockReplayAdapter(), k=2)
        result = reliability_check(receipt, simple_suite, MockReplayAdapter())
        assert result.passed
        assert result.coordination_identical
        assert result.byte_identical, "the fully deterministic adapter must replay byte-identically"

    def test_reliability_check_passes_under_model_sampling_variance(self, simple_suite: BenchSuite) -> None:
        """
        A fresh run whose model sample differs from the recorded attempt
        still passes: coordination is byte-identical, only the model-output
        payload varies.  A different seed models the model resampling.
        """
        task_a = simple_suite.tasks[0]
        th = task_a.content_hash()
        # Two seeds whose attempt-0 samples differ.
        seed_a = 0
        seed_b = next(
            s
            for s in range(1, 500)
            if StochasticMockReplayAdapter.derive_sample(s, th, 0)
            != StochasticMockReplayAdapter.derive_sample(seed_a, th, 0)
        )
        receipt = _make_receipt(simple_suite, StochasticMockReplayAdapter(seed=seed_a), k=1)
        result = reliability_check(
            receipt,
            simple_suite,
            StochasticMockReplayAdapter(seed=seed_b),
            task_id=task_a.id,
            attempt_index=0,
        )
        assert result.passed
        assert result.coordination_identical
        assert not result.byte_identical, "model samples must differ between the compared runs"

    def test_reliability_check_compares_the_requested_attempt(self, simple_suite: BenchSuite) -> None:
        """
        With a stateful adapter, --attempt N must compare position N against
        position N: the check replays a fresh same-seed adapter to attempt 1,
        so the run compared against recorded attempt 1 is byte-identical —
        which only holds when the positions are aligned.
        """
        task_a = simple_suite.tasks[0]
        th = task_a.content_hash()
        # A seed whose attempt-0 and attempt-1 samples differ, so a position
        # mismatch would be visible as byte divergence.
        seed = next(
            s
            for s in range(500)
            if StochasticMockReplayAdapter.derive_sample(s, th, 0)
            != StochasticMockReplayAdapter.derive_sample(s, th, 1)
        )
        receipt = _make_receipt(simple_suite, StochasticMockReplayAdapter(seed=seed), k=2)
        result = reliability_check(
            receipt,
            simple_suite,
            StochasticMockReplayAdapter(seed=seed),
            task_id=task_a.id,
            attempt_index=1,
        )
        assert result.passed
        assert result.byte_identical, (
            "same-seed replay to position 1 must reproduce recorded attempt 1 exactly; "
            "byte divergence here means the check compared the wrong attempt position"
        )

    def test_reliability_check_detects_coordination_nondeterminism(self, simple_suite: BenchSuite) -> None:
        """A drifting coordination field fails the check and is named."""
        receipt = _make_receipt(simple_suite, MockReplayAdapter(), k=2)
        result = reliability_check(receipt, simple_suite, NonDeterministicCoordinationAdapter())
        assert not result.passed
        assert not result.coordination_identical
        assert result.divergent_field, "the first divergent coordination field must be named"

    def test_reliability_check_unknown_task_fails_cleanly(self, simple_suite: BenchSuite) -> None:
        receipt = _make_receipt(simple_suite, MockReplayAdapter(), k=2)
        result = reliability_check(receipt, simple_suite, MockReplayAdapter(), task_id="no_such_task")
        assert not result.passed
        assert "no_such_task" in result.detail


# ===========================================================================
# CLI: bench run --reliability K / reliability-verify / reliability-check
# ===========================================================================


class TestCLI:
    def test_cmd_run_reliability_writes_signed_receipt(self, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from bernstein.eval.bench.bench_cli import bench_group

        out = tmp_path / "reliability.json"
        runner = CliRunner()
        result = runner.invoke(
            bench_group,
            ["run", "golden-v1", "--reliability", "3", "--out", str(out), "--stub-signer"],
        )
        assert result.exit_code == 0, result.output
        assert out.exists()
        loaded = ReliabilityReceipt.load(out)
        assert loaded.k == 3
        assert loaded.signature != ""
        assert "pass^3" in result.output
        assert "pass@1" in result.output

    def test_cmd_run_reliability_rejects_k_zero(self, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from bernstein.eval.bench.bench_cli import bench_group

        runner = CliRunner()
        result = runner.invoke(
            bench_group,
            ["run", "golden-v1", "--reliability", "0", "--out", str(tmp_path / "r.json")],
        )
        assert result.exit_code != 0

    def test_cmd_reliability_verify_passes_on_honest_receipt(self, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from bernstein.eval.bench.bench_cli import bench_group

        out = tmp_path / "reliability.json"
        runner = CliRunner()
        run_result = runner.invoke(
            bench_group,
            ["run", "golden-v1", "--reliability", "2", "--out", str(out), "--stub-signer"],
        )
        assert run_result.exit_code == 0, run_result.output

        verify_result = runner.invoke(bench_group, ["reliability-verify", str(out)])
        assert verify_result.exit_code == 0, verify_result.output
        assert "MATCH" in verify_result.output

    def test_cmd_reliability_verify_fails_on_fabricated_floor(self, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from bernstein.eval.bench.bench_cli import bench_group

        out = tmp_path / "reliability.json"
        runner = CliRunner()
        run_result = runner.invoke(
            bench_group,
            ["run", "golden-v1", "--reliability", "2", "--out", str(out), "--stub-signer"],
        )
        assert run_result.exit_code == 0, run_result.output

        honest = ReliabilityReceipt.load(out)
        deflated = StubReliabilitySigner().sign(replace(honest, pass_caret_k=0.5))
        tampered_path = tmp_path / "tampered.json"
        deflated.save(tampered_path)

        verify_result = runner.invoke(bench_group, ["reliability-verify", str(tampered_path)])
        assert verify_result.exit_code == 1
        assert "FABRICATED_FLOOR" in verify_result.output

    def test_cmd_reliability_check_passes(self, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from bernstein.eval.bench.bench_cli import bench_group

        out = tmp_path / "reliability.json"
        runner = CliRunner()
        run_result = runner.invoke(
            bench_group,
            ["run", "golden-v1", "--reliability", "2", "--out", str(out), "--stub-signer"],
        )
        assert run_result.exit_code == 0, run_result.output

        check_result = runner.invoke(bench_group, ["reliability-check", str(out)])
        assert check_result.exit_code == 0, check_result.output
        assert "PASS" in check_result.output


# ===========================================================================
# AC-6 — Docs shipped in the same PR
# ===========================================================================


class TestDocs:
    def test_reliability_docs_file_exists(self) -> None:
        repo_root = Path(__file__).parents[4]
        docs_path = repo_root / "docs" / "eval" / "reliability.md"
        assert docs_path.exists(), (
            f"docs/eval/reliability.md not found at {docs_path}. Docs must ship in the same PR (AC-6)."
        )

    def test_docs_cover_commands_and_metrics(self) -> None:
        repo_root = Path(__file__).parents[4]
        docs_path = repo_root / "docs" / "eval" / "reliability.md"
        if not docs_path.exists():
            pytest.skip("docs file missing — caught by test_reliability_docs_file_exists")
        content = docs_path.read_text()
        assert "--reliability" in content
        assert "reliability-verify" in content
        assert "reliability-check" in content
        assert "pass^k" in content
        assert "pass@1" in content

    def test_bench_docs_cross_reference_reliability(self) -> None:
        repo_root = Path(__file__).parents[4]
        bench_docs = repo_root / "docs" / "eval" / "bench.md"
        assert bench_docs.exists()
        assert "reliability" in bench_docs.read_text()


# ===========================================================================
# Review hardening: model-event coordination metadata + receipt schema
# ===========================================================================


class ModelMetadataDivergenceAdapter:
    """
    Adapter whose ``model.output`` events carry coordination metadata (a
    routing field) that drifts per call while the sampled payload stays
    fixed.  The projection must treat the undeclared field as coordination
    (fail-closed), so this divergence must be detected.
    """

    def __init__(self) -> None:
        self._calls = 0

    def run_task(self, task: BenchTask, scheduler_config: dict[str, Any]) -> dict[str, Any]:
        task_hash = task.content_hash()
        self._calls += 1
        return {
            "journal_head": "j" * 64,
            "spine_head": "s" * 64,
            "run_id": f"meta-{self._calls}",
            "events": [
                {"seq": 0, "kind": "task.started", "task_hash": task_hash},
                {"seq": 1, "kind": "model.output", "sample": 42, "route": self._calls},  # ← route drifts
                {"seq": 2, "kind": "task.completed", "task_hash": task_hash},
            ],
        }

    def score_task(self, task: BenchTask, receipt: dict[str, Any]) -> tuple[bool, float, dict[str, Any]]:
        return True, 1.0, {}


class MalformedReceiptAdapter:
    """Adapter emitting an event whose kind is not a string."""

    def run_task(self, task: BenchTask, scheduler_config: dict[str, Any]) -> dict[str, Any]:
        return {
            "journal_head": "j" * 64,
            "spine_head": "s" * 64,
            "run_id": "malformed",
            "events": [{"seq": 0, "kind": 123}],
        }

    def score_task(self, task: BenchTask, receipt: dict[str, Any]) -> tuple[bool, float, dict[str, Any]]:
        return True, 1.0, {}


def _with_first_attempt_receipt(receipt: ReliabilityReceipt, run_receipt: dict[str, Any]) -> ReliabilityReceipt:
    """Rebuild *receipt* with task 0 / attempt 0 swapped for *run_receipt* (hash recomputed)."""
    target = receipt.task_results[0]
    swapped_attempt = TaskResult(
        task_id=target.attempts[0].task_id,
        task_hash=target.attempts[0].task_hash,
        receipt=run_receipt,
        passed=target.attempts[0].passed,
        score=target.attempts[0].score,
    )
    swapped_task = replace(target, attempts=[swapped_attempt, *target.attempts[1:]])
    return replace(receipt, task_results=[swapped_task, *receipt.task_results[1:]])


class TestModelEventCoordinationMetadata:
    def test_model_event_metadata_divergence_is_detected(self, simple_suite: BenchSuite) -> None:
        """
        Coordination-relevant metadata inside a model.* event is NOT erased
        by the projection: divergence there fails coordination identity.
        """
        receipt = _make_receipt(simple_suite, ModelMetadataDivergenceAdapter(), k=2)
        assert not receipt.coordination_ok
        assert all(not tr.coordination_identical for tr in receipt.task_results)

    def test_model_event_metadata_divergence_rejected_by_verifier(self, simple_suite: BenchSuite) -> None:
        receipt = _signed(_make_receipt(simple_suite, ModelMetadataDivergenceAdapter(), k=2))
        verifier = ReliabilityVerifier(suite=simple_suite, adapter=ModelMetadataDivergenceAdapter())
        result = verifier.verify(receipt)
        assert not result.passed
        diverged = [
            tr for tr in result.task_results if tr.status == ReliabilityVerificationStatus.COORDINATION_DIVERGED
        ]
        assert diverged
        assert "route" in diverged[0].detail

    def test_declared_sample_field_still_allowed_to_vary(self, simple_suite: BenchSuite) -> None:
        """The declared stochastic payload (sample) still varies freely (regression guard)."""
        task = simple_suite.tasks[0]
        seed = _find_seed(task, 3, lambda v: any(v) and not all(v))
        receipt = _make_receipt(simple_suite, StochasticMockReplayAdapter(seed=seed), k=3)
        assert receipt.coordination_ok


class TestReceiptSchemaValidation:
    def test_malformed_event_kind_rejected_by_verifier(self, simple_suite: BenchSuite) -> None:
        """A non-string event kind is surfaced as MALFORMED_RECEIPT, never MATCH."""
        honest = _make_receipt(simple_suite, MockReplayAdapter(), k=2)
        malformed = _signed(
            _with_first_attempt_receipt(
                honest,
                {"journal_head": "j" * 64, "spine_head": "s" * 64, "run_id": "x", "events": [{"seq": 0, "kind": 123}]},
            )
        )
        verifier = ReliabilityVerifier(suite=simple_suite, adapter=MockReplayAdapter())
        result = verifier.verify(malformed)
        assert not result.passed
        statuses = {tr.task_id: tr.status for tr in result.task_results}
        assert statuses[honest.task_results[0].task_id] == ReliabilityVerificationStatus.MALFORMED_RECEIPT

    def test_malformed_event_seq_rejected_by_verifier(self, simple_suite: BenchSuite) -> None:
        """A missing / non-integer seq is surfaced as MALFORMED_RECEIPT."""
        honest = _make_receipt(simple_suite, MockReplayAdapter(), k=2)
        malformed = _signed(
            _with_first_attempt_receipt(
                honest,
                {
                    "journal_head": "j" * 64,
                    "spine_head": "s" * 64,
                    "run_id": "x",
                    "events": [{"kind": "task.started"}],  # ← seq missing
                },
            )
        )
        verifier = ReliabilityVerifier(suite=simple_suite, adapter=MockReplayAdapter())
        result = verifier.verify(malformed)
        assert not result.passed
        statuses = {tr.task_id: tr.status for tr in result.task_results}
        assert statuses[honest.task_results[0].task_id] == ReliabilityVerificationStatus.MALFORMED_RECEIPT

    def test_runner_rejects_malformed_adapter_receipt(self, simple_suite: BenchSuite) -> None:
        runner = ReliabilityRunner(suite=simple_suite, adapter=MalformedReceiptAdapter(), scheduler_config={}, k=1)
        with pytest.raises(ValueError, match="malformed run receipt"):
            runner.run()

    def test_reliability_check_rejects_malformed_recorded_receipt(self, simple_suite: BenchSuite) -> None:
        honest = _make_receipt(simple_suite, MockReplayAdapter(), k=2)
        malformed = _with_first_attempt_receipt(
            honest,
            {"journal_head": "j" * 64, "spine_head": "s" * 64, "run_id": "x", "events": [{"seq": 0, "kind": 123}]},
        )
        result = reliability_check(malformed, simple_suite, MockReplayAdapter())
        assert not result.passed
        assert "malformed" in result.detail


# ===========================================================================
# Review hardening: install-identity signature verification
# ===========================================================================


class TestInstallIdentitySignature:
    @staticmethod
    def _keypair() -> tuple[bytes, bytes]:
        from bernstein.core.security.agent_card_signer import generate_ed25519_keypair

        return generate_ed25519_keypair()

    def test_install_identity_signature_verifies_offline(self, simple_suite: BenchSuite, tmp_path: Path) -> None:
        """An Ed25519-signed receipt round-trips and verifies against the trusted key."""
        private_pem, public_pem = self._keypair()
        signer = InstallIdentityReliabilitySigner(private_key_pem=private_pem, public_key_pem=public_pem)
        receipt = signer.sign(_make_receipt(simple_suite, MockReplayAdapter(), k=2))

        path = tmp_path / "reliability.json"
        receipt.save(path)
        loaded = ReliabilityReceipt.load(path)

        verifier = ReliabilityVerifier(
            suite=simple_suite,
            adapter=MockReplayAdapter(),
            trusted_keys={signer.fingerprint(): public_pem},
        )
        assert verifier.verify(loaded).passed

    def test_forged_production_signature_never_reaches_match(self, simple_suite: BenchSuite) -> None:
        """
        Swapped-in fingerprint/signature values must never verify: garbage
        signatures, signatures minted by a different key, and unknown
        fingerprints all end UNSIGNED.
        """
        private_pem, public_pem = self._keypair()
        signer = InstallIdentityReliabilitySigner(private_key_pem=private_pem, public_key_pem=public_pem)
        base = _make_receipt(simple_suite, MockReplayAdapter(), k=2)
        honest = signer.sign(base)
        verifier = ReliabilityVerifier(
            suite=simple_suite,
            adapter=MockReplayAdapter(),
            trusted_keys={signer.fingerprint(): public_pem},
        )
        assert verifier.verify(honest).passed  # guard

        # (a) Garbage signature under the trusted fingerprint.
        garbage = replace(honest, signature="AAAA..BBBB")
        assert verifier.verify(garbage).status == ReliabilityVerificationStatus.UNSIGNED

        # (b) Structurally valid signature minted by a DIFFERENT key,
        #     presented under the trusted fingerprint.
        other_private, other_public = self._keypair()
        other_signer = InstallIdentityReliabilitySigner(private_key_pem=other_private, public_key_pem=other_public)
        cross = replace(honest, signature=other_signer.sign(base).signature)
        assert verifier.verify(cross).status == ReliabilityVerificationStatus.UNSIGNED

        # (c) Unknown fingerprint (not in the trusted key map).
        unknown = replace(honest, signer_fingerprint="not-a-known-keyid")
        assert verifier.verify(unknown).status == ReliabilityVerificationStatus.UNSIGNED

    def test_tampered_content_fails_signature_verification(self, simple_suite: BenchSuite) -> None:
        """Content edited after signing no longer verifies (hash moved under the signature)."""
        private_pem, public_pem = self._keypair()
        signer = InstallIdentityReliabilitySigner(private_key_pem=private_pem, public_key_pem=public_pem)
        honest = signer.sign(_make_receipt(simple_suite, MockReplayAdapter(), k=2))
        tampered = replace(honest, pass_caret_k=0.0)  # keeps the old signature
        verifier = ReliabilityVerifier(
            suite=simple_suite,
            adapter=MockReplayAdapter(),
            trusted_keys={signer.fingerprint(): public_pem},
        )
        assert verifier.verify(tampered).status == ReliabilityVerificationStatus.UNSIGNED

    def test_signer_requires_both_or_neither_key(self) -> None:
        with pytest.raises(ValueError, match="both"):
            InstallIdentityReliabilitySigner(private_key_pem=b"x")
