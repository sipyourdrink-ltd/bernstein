"""
TDD tests for bernstein-bench.

Acceptance criteria covered (verbatim from issue #2932):

AC-1  bernstein bench run produces a signed submission bundle; two runs of the
      same suite on the same inputs produce byte-identical per-task receipts
      (empirical determinism).

AC-2  bernstein bench verify recomputes every task's score by replaying the
      embedded receipts offline, with no access to the submitter's machine,
      and reports MATCH or the exact task whose replay diverged.

AC-3  A bundle with a fabricated score (verdict flipped without a matching
      replayable run) is rejected at the diverging task; removing or
      corrupting a task's receipt makes the whole bundle fail verification —
      the score has no meaning without the replay substrate.

AC-4  The suite is content-addressed: two runners on the same suite hash
      provably ran the same task set; a changed task changes the suite hash.

AC-5  The leaderboard projection lists only bench verify-passing bundles,
      each row linking its bundle hash.

AC-6  Docs shipped in the same PR.  (Verified by test_docs_file_exists.)

All tests use the hermetic MockReplayAdapter — no network, no real adapters.
"""

from __future__ import annotations

import copy
import json
import tempfile
from dataclasses import replace
from pathlib import Path

import pytest

from bernstein.eval.bench.bundle import SubmissionBundle, TaskResult
from bernstein.eval.bench.golden_suite import build_golden_suite_v1
from bernstein.eval.bench.leaderboard import Leaderboard, LeaderboardEntry
from bernstein.eval.bench.runner import BenchRunner, MockReplayAdapter
from bernstein.eval.bench.signer import AgentCardSigner, StubSigner
from bernstein.eval.bench.suite import BenchSuite, BenchTask
from bernstein.eval.bench.verifier import BenchVerifier, VerificationStatus

# ---------------------------------------------------------------------------
# Fixtures
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


@pytest.fixture()
def adapter() -> MockReplayAdapter:
    return MockReplayAdapter()


@pytest.fixture()
def golden_suite() -> BenchSuite:
    return build_golden_suite_v1()


def _make_bundle(suite: BenchSuite, adapter: MockReplayAdapter, cfg: dict | None = None) -> SubmissionBundle:
    runner = BenchRunner(suite=suite, adapter=adapter, scheduler_config=cfg or {})
    return runner.run()


# ===========================================================================
# AC-4 — Suite content-addressing
# ===========================================================================


class TestSuiteContentAddressing:
    """AC-4: suite is content-addressed; a changed task changes the suite hash."""

    def test_same_suite_same_hash(self, simple_suite: BenchSuite) -> None:
        suite2 = BenchSuite(version="test-v1", tasks=list(simple_suite.tasks))
        assert simple_suite.suite_hash == suite2.suite_hash

    def test_changed_task_changes_hash(self, simple_suite: BenchSuite) -> None:
        original_hash = simple_suite.suite_hash
        mutated_task = BenchTask(
            id="task_a",
            description="Task A CHANGED",
            steps=("step 1", "step 2"),
            assertions=({"kind": "exists"},),
            category="cat1",
        )
        mutated = BenchSuite(version="test-v1", tasks=[mutated_task, simple_suite.tasks[1]])
        assert mutated.suite_hash != original_hash

    def test_reordering_tasks_changes_hash(self, simple_suite: BenchSuite) -> None:
        reordered = BenchSuite(version="test-v1", tasks=list(reversed(simple_suite.tasks)))
        assert reordered.suite_hash != simple_suite.suite_hash

    def test_adding_task_changes_hash(self, simple_suite: BenchSuite) -> None:
        extended = BenchSuite(
            version="test-v1",
            tasks=[*list(simple_suite.tasks), BenchTask(id="task_c", description="C", steps=("s",), assertions=())],
        )
        assert extended.suite_hash != simple_suite.suite_hash

    def test_task_hash_stable(self, simple_suite: BenchSuite) -> None:
        task = simple_suite.tasks[0]
        assert task.content_hash() == task.content_hash()

    def test_suite_round_trip(self, simple_suite: BenchSuite) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "suite.json"
            simple_suite.save(path)
            loaded = BenchSuite.load(path)
        assert loaded.suite_hash == simple_suite.suite_hash
        assert len(loaded.tasks) == len(simple_suite.tasks)

    def test_tampered_suite_file_rejected(self, simple_suite: BenchSuite) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "suite.json"
            simple_suite.save(path)
            raw = json.loads(path.read_text())
            raw["suite_hash"] = "deadbeef" * 8
            path.write_text(json.dumps(raw))
            with pytest.raises(ValueError, match="Suite hash mismatch"):
                BenchSuite.load(path)


# ===========================================================================
# AC-1 — Runner: empirical determinism + signed bundle
# ===========================================================================


class TestRunnerDeterminism:
    """AC-1: two runs produce byte-identical receipts; bundle is signed."""

    def test_two_runs_produce_identical_receipts(self, simple_suite: BenchSuite, adapter: MockReplayAdapter) -> None:
        cfg = {"scheduler": "test", "workers": 1}
        runner = BenchRunner(suite=simple_suite, adapter=adapter, scheduler_config=cfg)
        b1 = runner.run()
        b2 = runner.run()
        for r1, r2 in zip(b1.task_results, b2.task_results, strict=True):
            assert r1.receipt == r2.receipt, f"Task {r1.task_id}: receipts diverged between runs"

    def test_receipt_hashes_stable_across_runs(self, simple_suite: BenchSuite, adapter: MockReplayAdapter) -> None:
        runner = BenchRunner(suite=simple_suite, adapter=adapter, scheduler_config={})
        b1, b2 = runner.run(), runner.run()
        for r1, r2 in zip(b1.task_results, b2.task_results, strict=True):
            assert r1.stored_receipt_hash == r2.stored_receipt_hash

    def test_bundle_covers_all_tasks(self, simple_suite: BenchSuite, adapter: MockReplayAdapter) -> None:
        bundle = _make_bundle(simple_suite, adapter)
        assert len(bundle.task_results) == len(simple_suite.tasks)

    def test_bundle_is_signed_by_stub_signer(self, simple_suite: BenchSuite, adapter: MockReplayAdapter) -> None:
        """AC-1: the emitted bundle carries a non-empty signature."""
        bundle = _make_bundle(simple_suite, adapter)
        signed = StubSigner().sign(bundle)
        assert signed.signature != "", "signature must be non-empty after signing"
        assert signed.signer_fingerprint != "", "signer_fingerprint must be set"

    def test_signing_is_deterministic(self, simple_suite: BenchSuite, adapter: MockReplayAdapter) -> None:
        bundle = _make_bundle(simple_suite, adapter)
        s1 = StubSigner().sign(bundle)
        s2 = StubSigner().sign(bundle)
        assert s1.signature == s2.signature
        assert s1.signer_fingerprint == s2.signer_fingerprint

    def test_golden_suite_run(self, golden_suite: BenchSuite, adapter: MockReplayAdapter) -> None:
        bundle = _make_bundle(golden_suite, adapter)
        assert len(bundle.task_results) == len(golden_suite.tasks)
        assert bundle.overall_score == 1.0


# ===========================================================================
# Bundle round-trip and integrity
# ===========================================================================


class TestBundleRoundTrip:
    def test_save_load_preserves_bundle_hash(self, simple_suite: BenchSuite, adapter: MockReplayAdapter) -> None:
        bundle = _make_bundle(simple_suite, adapter)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bundle.json"
            bundle.save(path)
            loaded = SubmissionBundle.load(path)
        assert loaded.bundle_hash() == bundle.bundle_hash()

    def test_save_load_preserves_stored_receipt_hashes(
        self, simple_suite: BenchSuite, adapter: MockReplayAdapter
    ) -> None:
        """stored_receipt_hash must survive a save/load round-trip."""
        bundle = _make_bundle(simple_suite, adapter)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bundle.json"
            bundle.save(path)
            loaded = SubmissionBundle.load(path)
        for orig, restored in zip(bundle.task_results, loaded.task_results, strict=True):
            assert orig.stored_receipt_hash == restored.stored_receipt_hash

    def test_tampered_bundle_hash_rejected_on_load(self, simple_suite: BenchSuite, adapter: MockReplayAdapter) -> None:
        bundle = _make_bundle(simple_suite, adapter)
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bundle.json"
            bundle.save(path)
            raw = json.loads(path.read_text())
            raw["bundle_hash"] = "badbad" * 10
            path.write_text(json.dumps(raw))
            with pytest.raises(ValueError, match="Bundle hash mismatch"):
                SubmissionBundle.load(path)


# ===========================================================================
# AC-2 — Verifier: MATCH path
# ===========================================================================


class TestVerifierMatch:
    """AC-2: bench verify recomputes scores offline and reports MATCH."""

    def test_honest_bundle_passes_verification(self, simple_suite: BenchSuite, adapter: MockReplayAdapter) -> None:
        bundle = StubSigner().sign(_make_bundle(simple_suite, adapter))
        verifier = BenchVerifier(suite=simple_suite, adapter=adapter)
        result = verifier.verify(bundle)
        assert result.status == VerificationStatus.MATCH
        assert result.passed
        for tr in result.task_results:
            assert tr.status == VerificationStatus.MATCH

    def test_honest_bundle_after_save_load(self, simple_suite: BenchSuite, adapter: MockReplayAdapter) -> None:
        """verify must pass on a bundle loaded from disk (round-trip)."""
        bundle = StubSigner().sign(_make_bundle(simple_suite, adapter))
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bundle.json"
            bundle.save(path)
            loaded = SubmissionBundle.load(path)
        verifier = BenchVerifier(suite=simple_suite, adapter=adapter)
        result = verifier.verify(loaded)
        assert result.passed

    def test_verify_names_each_task(self, simple_suite: BenchSuite, adapter: MockReplayAdapter) -> None:
        """Report must include a per-task result for every task."""
        bundle = StubSigner().sign(_make_bundle(simple_suite, adapter))
        verifier = BenchVerifier(suite=simple_suite, adapter=adapter)
        result = verifier.verify(bundle)
        ids = {tr.task_id for tr in result.task_results}
        assert ids == {t.id for t in simple_suite.tasks}

    def test_golden_suite_verifies(self, golden_suite: BenchSuite, adapter: MockReplayAdapter) -> None:
        bundle = StubSigner().sign(_make_bundle(golden_suite, adapter))
        verifier = BenchVerifier(suite=golden_suite, adapter=adapter)
        assert verifier.verify(bundle).passed

    def test_report_string_contains_match(self, simple_suite: BenchSuite, adapter: MockReplayAdapter) -> None:
        bundle = StubSigner().sign(_make_bundle(simple_suite, adapter))
        verifier = BenchVerifier(suite=simple_suite, adapter=adapter)
        report = verifier.verify(bundle).report()
        assert "MATCH" in report


# ===========================================================================
# AC-3 — Fabricated score rejected
# ===========================================================================


class TestVerifierFabricatedScore:
    """AC-3a: a flipped verdict is caught at the exact diverging task."""

    def test_flipped_verdict_rejected(self, simple_suite: BenchSuite, adapter: MockReplayAdapter) -> None:
        bundle = _make_bundle(simple_suite, adapter)
        tampered = TaskResult(
            task_id=bundle.task_results[0].task_id,
            task_hash=bundle.task_results[0].task_hash,
            receipt=bundle.task_results[0].receipt,
            passed=not bundle.task_results[0].passed,  # ← flip
            score=0.0,
        )
        bad_bundle = StubSigner().sign(
            SubmissionBundle(
                suite_hash=bundle.suite_hash,
                suite_version=bundle.suite_version,
                task_results=[tampered, *bundle.task_results[1:]],
                scheduler_config=bundle.scheduler_config,
                submitted_at=bundle.submitted_at,
            )
        )
        verifier = BenchVerifier(suite=simple_suite, adapter=adapter)
        result = verifier.verify(bad_bundle)
        assert not result.passed
        fabricated = [tr for tr in result.task_results if tr.status == VerificationStatus.FABRICATED_SCORE]
        assert len(fabricated) == 1
        assert fabricated[0].task_id == bundle.task_results[0].task_id

    def test_wrong_suite_hash_rejected(self, simple_suite: BenchSuite, adapter: MockReplayAdapter) -> None:
        bundle = _make_bundle(simple_suite, adapter)
        forged = SubmissionBundle(
            suite_hash="0000" * 16,
            suite_version=bundle.suite_version,
            task_results=bundle.task_results,
            scheduler_config=bundle.scheduler_config,
            submitted_at=bundle.submitted_at,
        )
        verifier = BenchVerifier(suite=simple_suite, adapter=adapter)
        result = verifier.verify(forged)
        assert result.status == VerificationStatus.HASH_MISMATCH


# ===========================================================================
# AC-3 — Receipt integrity: missing / corrupted receipt
# ===========================================================================


class TestVerifierReceiptIntegrity:
    """
    AC-3b: removing or corrupting a task's receipt makes the whole bundle
    fail verification — the score has no meaning without the replay substrate.
    """

    def test_empty_receipt_fails(self, simple_suite: BenchSuite, adapter: MockReplayAdapter) -> None:
        bundle = _make_bundle(simple_suite, adapter)
        stripped = TaskResult(
            task_id=bundle.task_results[0].task_id,
            task_hash=bundle.task_results[0].task_hash,
            receipt={},  # ← removed
            passed=bundle.task_results[0].passed,
            score=bundle.task_results[0].score,
        )
        bad = StubSigner().sign(
            SubmissionBundle(
                suite_hash=bundle.suite_hash,
                suite_version=bundle.suite_version,
                task_results=[stripped, *bundle.task_results[1:]],
                scheduler_config=bundle.scheduler_config,
                submitted_at=bundle.submitted_at,
            )
        )
        verifier = BenchVerifier(suite=simple_suite, adapter=adapter)
        result = verifier.verify(bad)
        assert not result.passed
        missing = [tr for tr in result.task_results if tr.status == VerificationStatus.MISSING_RECEIPT]
        assert len(missing) == 1

    def test_corrupted_receipt_bytes_caught(self, simple_suite: BenchSuite, adapter: MockReplayAdapter) -> None:
        """
        A receipt whose bytes were changed AFTER the bundle was emitted is
        caught even when the verdict is left unchanged.

        stored_receipt_hash was committed at emit time; any byte-flip
        diverges from the stored hash.
        """
        bundle = _make_bundle(simple_suite, adapter)

        # Tamper the receipt bytes without touching the verdict.
        tampered_receipt = copy.deepcopy(bundle.task_results[0].receipt)
        tampered_receipt["journal_head"] = "aaaa" * 16  # flip one field

        # Build a TaskResult that has the ORIGINAL stored_receipt_hash
        # (from emit time) but a DIFFERENT live receipt — simulating a
        # post-emit byte-flip while the stored hash stays pinned.
        original_stored_hash = bundle.task_results[0].stored_receipt_hash
        corrupted = TaskResult.__new__(TaskResult)
        corrupted.task_id = bundle.task_results[0].task_id
        corrupted.task_hash = bundle.task_results[0].task_hash
        corrupted.receipt = tampered_receipt  # ← changed bytes
        corrupted.passed = bundle.task_results[0].passed
        corrupted.score = bundle.task_results[0].score
        corrupted.harness_output = {}
        corrupted.stored_receipt_hash = original_stored_hash  # ← original hash

        bad = StubSigner().sign(
            SubmissionBundle(
                suite_hash=bundle.suite_hash,
                suite_version=bundle.suite_version,
                task_results=[corrupted, *bundle.task_results[1:]],
                scheduler_config=bundle.scheduler_config,
                submitted_at=bundle.submitted_at,
            )
        )

        verifier = BenchVerifier(suite=simple_suite, adapter=adapter)
        result = verifier.verify(bad)

        assert not result.passed, "Corrupted receipt must not pass verification"
        mismatch = [tr for tr in result.task_results if tr.status == VerificationStatus.HASH_MISMATCH]
        assert len(mismatch) == 1, (
            f"Expected exactly one HASH_MISMATCH task, got: {[tr.status for tr in result.task_results]}"
        )

    def test_one_bad_receipt_fails_whole_bundle(self, simple_suite: BenchSuite, adapter: MockReplayAdapter) -> None:
        """Whole-bundle status must not be MATCH if any task fails."""
        bundle = _make_bundle(simple_suite, adapter)
        stripped = TaskResult(
            task_id=bundle.task_results[0].task_id,
            task_hash=bundle.task_results[0].task_hash,
            receipt={},
            passed=bundle.task_results[0].passed,
            score=bundle.task_results[0].score,
        )
        bad = StubSigner().sign(
            SubmissionBundle(
                suite_hash=bundle.suite_hash,
                suite_version=bundle.suite_version,
                task_results=[stripped, *bundle.task_results[1:]],
                scheduler_config=bundle.scheduler_config,
                submitted_at=bundle.submitted_at,
            )
        )
        verifier = BenchVerifier(suite=simple_suite, adapter=adapter)
        result = verifier.verify(bad)
        assert result.status != VerificationStatus.MATCH


# ===========================================================================
# Bundle signature verification (issue #5856)
# ===========================================================================


class TestBundleSignatureVerification:
    """
    #5856: AgentCardSigner.sign imported a module that does not exist, so
    every production run silently fell back to StubSigner's public test
    key, and BenchVerifier.verify never checked signature/signer_fingerprint
    at all. Both are fixed together: an honest but unsigned bundle must
    never verify as MATCH, and AgentCardSigner must produce a real,
    independently-verifiable install-identity signature rather than a
    disguised stub one.
    """

    @staticmethod
    def _keypair() -> tuple[bytes, bytes]:
        from bernstein.core.security.agent_card_signer import generate_ed25519_keypair

        return generate_ed25519_keypair()

    def test_unsigned_bundle_is_unsigned_not_match(self, simple_suite: BenchSuite, adapter: MockReplayAdapter) -> None:
        """An otherwise-honest bundle with no signature must not read as MATCH."""
        bundle = _make_bundle(simple_suite, adapter)
        assert bundle.signature == "" and bundle.signer_fingerprint == ""
        verifier = BenchVerifier(suite=simple_suite, adapter=adapter)
        result = verifier.verify(bundle)
        assert result.status == VerificationStatus.UNSIGNED

    def test_forged_stub_signature_never_reaches_match(
        self, simple_suite: BenchSuite, adapter: MockReplayAdapter
    ) -> None:
        """A bundle carrying the stub's own fingerprint but a garbage signature must fail."""
        bundle = replace(
            StubSigner().sign(_make_bundle(simple_suite, adapter)),
            signature="not-the-real-stub-signature",
        )
        verifier = BenchVerifier(suite=simple_suite, adapter=adapter)
        result = verifier.verify(bundle)
        assert result.status == VerificationStatus.UNSIGNED
        assert "Stub signature" in result.detail
        assert not result.passed

    def test_agent_card_signer_raises_without_install_identity(
        self, simple_suite: BenchSuite, adapter: MockReplayAdapter, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Signing fails loudly when no key material is available, never silently minted."""
        from bernstein.core.security.agent_card_keystore import AgentCardKeystore

        empty_keystore = AgentCardKeystore(tmp_path / "keys")
        assert not empty_keystore.has_keypair()
        monkeypatch.setattr("bernstein.core.identity.http_signing.default_keystore", lambda: empty_keystore)
        with pytest.raises(RuntimeError, match="bernstein init"):
            AgentCardSigner().sign(_make_bundle(simple_suite, adapter))
        assert not empty_keystore.has_keypair()

    def test_agent_card_signer_fingerprint_differs_from_stub(
        self, simple_suite: BenchSuite, adapter: MockReplayAdapter
    ) -> None:
        private_pem, public_pem = self._keypair()
        signer = AgentCardSigner(private_key_pem=private_pem, public_key_pem=public_pem)
        signed = signer.sign(_make_bundle(simple_suite, adapter))
        assert signed.signer_fingerprint != StubSigner.fingerprint()
        assert signed.signature != StubSigner.expected_signature(signed)

    def test_install_identity_signature_verifies_offline(
        self, simple_suite: BenchSuite, adapter: MockReplayAdapter
    ) -> None:
        """An Ed25519-signed bundle round-trips and verifies against the trusted key."""
        private_pem, public_pem = self._keypair()
        signer = AgentCardSigner(private_key_pem=private_pem, public_key_pem=public_pem)
        signed = signer.sign(_make_bundle(simple_suite, adapter))

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bundle.json"
            signed.save(path)
            loaded = SubmissionBundle.load(path)

        verifier = BenchVerifier(
            suite=simple_suite,
            adapter=adapter,
            trusted_keys={signer.fingerprint(): public_pem},
        )
        assert verifier.verify(loaded).passed

    def test_forged_bundle_signature_never_reaches_match(
        self, simple_suite: BenchSuite, adapter: MockReplayAdapter
    ) -> None:
        """
        Swapped-in fingerprint/signature values must never verify: garbage
        signatures, signatures minted by a different key, and unknown
        fingerprints all end UNSIGNED.
        """
        private_pem, public_pem = self._keypair()
        signer = AgentCardSigner(private_key_pem=private_pem, public_key_pem=public_pem)
        base = _make_bundle(simple_suite, adapter)
        honest = signer.sign(base)
        verifier = BenchVerifier(
            suite=simple_suite,
            adapter=adapter,
            trusted_keys={signer.fingerprint(): public_pem},
        )
        assert verifier.verify(honest).passed  # guard

        # (a) Garbage signature under the trusted fingerprint.
        garbage = replace(honest, signature="AAAA..BBBB")
        assert verifier.verify(garbage).status == VerificationStatus.UNSIGNED

        # (b) Structurally valid signature minted by a DIFFERENT key,
        #     presented under the trusted fingerprint.
        other_private, other_public = self._keypair()
        other_signer = AgentCardSigner(private_key_pem=other_private, public_key_pem=other_public)
        cross = replace(honest, signature=other_signer.sign(base).signature)
        assert verifier.verify(cross).status == VerificationStatus.UNSIGNED

        # (c) Unknown fingerprint (not in the trusted key map).
        unknown = replace(honest, signer_fingerprint="not-a-known-keyid")
        assert verifier.verify(unknown).status == VerificationStatus.UNSIGNED

    def test_tampered_content_fails_signature_verification(
        self, simple_suite: BenchSuite, adapter: MockReplayAdapter
    ) -> None:
        """Content edited after signing no longer verifies (hash moved under the signature)."""
        private_pem, public_pem = self._keypair()
        signer = AgentCardSigner(private_key_pem=private_pem, public_key_pem=public_pem)
        honest = signer.sign(_make_bundle(simple_suite, adapter))
        tampered = replace(honest, holdout_hash="deadbeef" * 8)  # keeps the old signature
        verifier = BenchVerifier(
            suite=simple_suite,
            adapter=adapter,
            trusted_keys={signer.fingerprint(): public_pem},
        )
        assert verifier.verify(tampered).status == VerificationStatus.UNSIGNED

    def test_agent_card_signer_requires_both_or_neither_key(self) -> None:
        with pytest.raises(ValueError, match="both"):
            AgentCardSigner(private_key_pem=b"x")

    def test_require_install_identity_refuses_valid_stub_signature(
        self, simple_suite: BenchSuite, adapter: MockReplayAdapter
    ) -> None:
        """A stub signature that verifies fine by default is refused under the strict flag."""
        bundle = StubSigner().sign(_make_bundle(simple_suite, adapter))
        lenient = BenchVerifier(suite=simple_suite, adapter=adapter)
        assert lenient.verify(bundle).passed  # guard: still MATCH by default

        strict = BenchVerifier(suite=simple_suite, adapter=adapter, require_install_identity=True)
        result = strict.verify(bundle)
        assert result.status == VerificationStatus.UNSIGNED
        assert "stub" in result.detail.lower()

    def test_report_names_the_signer(self, simple_suite: BenchSuite, adapter: MockReplayAdapter) -> None:
        """The verify report says whether a MATCH came from a stub or an install identity."""
        stub_bundle = StubSigner().sign(_make_bundle(simple_suite, adapter))
        verifier = BenchVerifier(suite=simple_suite, adapter=adapter)
        stub_report = verifier.verify(stub_bundle).report()
        assert f"signer      : {StubSigner.fingerprint()} (stub, test-grade)" in stub_report

        private_pem, public_pem = self._keypair()
        signer = AgentCardSigner(private_key_pem=private_pem, public_key_pem=public_pem)
        install_bundle = signer.sign(_make_bundle(simple_suite, adapter))
        install_verifier = BenchVerifier(
            suite=simple_suite, adapter=adapter, trusted_keys={signer.fingerprint(): public_pem}
        )
        install_report = install_verifier.verify(install_bundle).report()
        assert f"signer      : {signer.fingerprint()} (install identity)" in install_report


# ===========================================================================
# AC-5 — Leaderboard: only verified bundles appear
# ===========================================================================


class TestLeaderboard:
    """AC-5: leaderboard lists only bench-verify-passing bundles."""

    def test_only_verified_bundles_appear(self, simple_suite: BenchSuite, adapter: MockReplayAdapter) -> None:
        bundle = StubSigner().sign(_make_bundle(simple_suite, adapter))
        verifier = BenchVerifier(suite=simple_suite, adapter=adapter)
        assert verifier.verify(bundle).passed  # guard

        lb = Leaderboard(suite_hash=simple_suite.suite_hash, suite_version="test-v1")
        lb.add_entry(
            LeaderboardEntry(
                bundle_hash=bundle.bundle_hash(),
                suite_hash=bundle.suite_hash,
                suite_version=bundle.suite_version,
                overall_score=bundle.overall_score,
                pass_rate=bundle.pass_rate,
                num_tasks=len(bundle.task_results),
                submitted_at=bundle.submitted_at,
                bundle_path="bundles/test.json",
            )
        )
        md = lb.to_markdown()
        assert bundle.bundle_hash()[:16] in md
        assert "bernstein bench verify" in md

    def test_leaderboard_sorted_by_score_desc(self, simple_suite: BenchSuite, adapter: MockReplayAdapter) -> None:
        lb = Leaderboard(suite_hash="x", suite_version="v1")
        for score in [0.7, 1.0, 0.5]:
            lb.add_entry(
                LeaderboardEntry(
                    bundle_hash=f"hash-{score}",
                    suite_hash="x",
                    suite_version="v1",
                    overall_score=score,
                    pass_rate=score,
                    num_tasks=2,
                    submitted_at=1.0,
                )
            )
        scores = [e.overall_score for e in lb.entries]
        assert scores == sorted(scores, reverse=True)

    def test_leaderboard_round_trip(self, simple_suite: BenchSuite, adapter: MockReplayAdapter) -> None:
        bundle = _make_bundle(simple_suite, adapter)
        lb = Leaderboard(suite_hash=simple_suite.suite_hash, suite_version="test-v1")
        lb.add_entry(
            LeaderboardEntry(
                bundle_hash=bundle.bundle_hash(),
                suite_hash=bundle.suite_hash,
                suite_version=bundle.suite_version,
                overall_score=bundle.overall_score,
                pass_rate=bundle.pass_rate,
                num_tasks=len(bundle.task_results),
                submitted_at=bundle.submitted_at,
            )
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "lb.json"
            lb.save(path)
            loaded = Leaderboard.load(path)
        assert len(loaded.entries) == 1
        assert loaded.entries[0].bundle_hash == bundle.bundle_hash()


# ===========================================================================
# AC-6 — Docs file exists
# ===========================================================================


class TestDocs:
    """AC-6: docs shipped in the same PR."""

    def test_docs_file_exists(self) -> None:
        # Resolve relative to this test file's location in the repo.
        repo_root = Path(__file__).parents[4]  # tests/unit/eval/bench/test_bench.py -> repo root
        docs_path = repo_root / "docs" / "eval" / "bench.md"
        assert docs_path.exists(), f"docs/eval/bench.md not found at {docs_path}. Docs must ship in the same PR (AC-6)."

    def test_docs_covers_run_and_verify(self) -> None:
        repo_root = Path(__file__).parents[4]
        docs_path = repo_root / "docs" / "eval" / "bench.md"
        if not docs_path.exists():
            pytest.skip("docs file missing — caught by test_docs_file_exists")
        content = docs_path.read_text(encoding="utf-8")
        assert "bernstein bench run" in content
        assert "bernstein bench verify" in content


# ===========================================================================
# CLI smoke tests
# ===========================================================================


class TestCLI:
    def test_cmd_run_golden_suite(self, tmp_path: Path) -> None:
        from click.testing import CliRunner

        from bernstein.eval.bench.bench_cli import bench_group

        out = tmp_path / "bundle.json"
        runner = CliRunner()
        result = runner.invoke(bench_group, ["run", "golden-v1", "--out", str(out), "--stub-signer"])
        assert result.exit_code == 0, result.output
        assert out.exists()
        loaded = SubmissionBundle.load(out)
        assert loaded.suite_version == "golden-v1"
        assert loaded.signature != ""

    def test_cmd_verify_passes_on_honest_bundle(
        self, tmp_path: Path, simple_suite: BenchSuite, adapter: MockReplayAdapter
    ) -> None:
        from click.testing import CliRunner

        from bernstein.eval.bench.bench_cli import bench_group

        bundle = _make_bundle(simple_suite, adapter)
        signed = StubSigner().sign(bundle)
        suite_path = tmp_path / "suite.json"
        simple_suite.save(suite_path)
        bundle_path = tmp_path / "bundle.json"
        signed.save(bundle_path)
        runner = CliRunner()
        result = runner.invoke(bench_group, ["verify", str(bundle_path), "--suite", str(suite_path)])
        assert result.exit_code == 0, result.output

    def test_cmd_verify_checks_install_identity_signature_via_signer_key(
        self, tmp_path: Path, simple_suite: BenchSuite, adapter: MockReplayAdapter
    ) -> None:
        """
        End-to-end through the CLI entry point: --signer-key loads the
        trusted public key file that _reliability_trusted_keys resolves
        into BenchVerifier(trusted_keys=...), not just the class directly.
        """
        from click.testing import CliRunner

        from bernstein.core.security.agent_card_signer import generate_ed25519_keypair
        from bernstein.eval.bench.bench_cli import bench_group

        private_pem, public_pem = generate_ed25519_keypair()
        signed = AgentCardSigner(private_key_pem=private_pem, public_key_pem=public_pem).sign(
            _make_bundle(simple_suite, adapter)
        )
        suite_path = tmp_path / "suite.json"
        simple_suite.save(suite_path)
        bundle_path = tmp_path / "bundle.json"
        signed.save(bundle_path)
        key_path = tmp_path / "signer.pub.pem"
        key_path.write_bytes(public_pem)

        runner = CliRunner()
        no_key = runner.invoke(bench_group, ["verify", str(bundle_path), "--suite", str(suite_path)])
        assert no_key.exit_code == 1, no_key.output

        with_key = runner.invoke(
            bench_group,
            ["verify", str(bundle_path), "--suite", str(suite_path), "--signer-key", str(key_path)],
        )
        assert with_key.exit_code == 0, with_key.output

    def test_cmd_verify_fails_on_fabricated_bundle(
        self, tmp_path: Path, simple_suite: BenchSuite, adapter: MockReplayAdapter
    ) -> None:
        from click.testing import CliRunner

        from bernstein.eval.bench.bench_cli import bench_group

        bundle = _make_bundle(simple_suite, adapter)
        tampered = TaskResult(
            task_id=bundle.task_results[0].task_id,
            task_hash=bundle.task_results[0].task_hash,
            receipt=bundle.task_results[0].receipt,
            passed=not bundle.task_results[0].passed,
            score=0.0,
        )
        bad = StubSigner().sign(
            SubmissionBundle(
                suite_hash=bundle.suite_hash,
                suite_version=bundle.suite_version,
                task_results=[tampered, *bundle.task_results[1:]],
                scheduler_config=bundle.scheduler_config,
                submitted_at=bundle.submitted_at,
            )
        )
        suite_path = tmp_path / "suite.json"
        simple_suite.save(suite_path)
        bundle_path = tmp_path / "bundle.json"
        bad.save(bundle_path)
        runner = CliRunner()
        result = runner.invoke(bench_group, ["verify", str(bundle_path), "--suite", str(suite_path)])
        assert result.exit_code == 1
