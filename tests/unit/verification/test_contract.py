"""Tests for the verification contract and evidence hashing (#6559)."""

from __future__ import annotations

from bernstein.core.verification.contract import (
    Verdict,
    VerifyVerdict,
    verify_evidence_hash,
)


class TestVerdict:
    def test_values(self) -> None:
        assert Verdict.PASS == "pass"
        assert Verdict.FAIL == "fail"
        assert Verdict.RETRY == "retry"

    def test_all_variants_are_strings(self) -> None:
        for v in Verdict:
            assert isinstance(v, str)


class TestVerifyVerdict:
    def test_pass_verdict_ok(self) -> None:
        v = VerifyVerdict(verdict=Verdict.PASS, tier="edge", backend="ruff")
        assert v.ok is True
        assert v.should_retry is False

    def test_fail_verdict_not_ok(self) -> None:
        v = VerifyVerdict(verdict=Verdict.FAIL, tier="edge", backend="ruff", reason="lint failed")
        assert v.ok is False
        assert v.should_retry is False

    def test_retry_verdict(self) -> None:
        v = VerifyVerdict(verdict=Verdict.RETRY, tier="search", backend="tavily")
        assert v.ok is False
        assert v.should_retry is True

    def test_frozen(self) -> None:
        v = VerifyVerdict(verdict=Verdict.PASS, tier="edge", backend="test")
        try:
            v.verdict = Verdict.FAIL  # type: ignore[misc]
            raise AssertionError("should have raised")
        except AttributeError:
            pass

    def test_evidence_hash_populated(self) -> None:
        evidence = {"key": "value"}
        h = verify_evidence_hash(evidence)
        v = VerifyVerdict(
            verdict=Verdict.PASS,
            tier="edge",
            backend="test",
            evidence=evidence,
            evidence_hash=h,
        )
        assert v.evidence_hash.startswith("sha256:")

    def test_attempt_default(self) -> None:
        v = VerifyVerdict(verdict=Verdict.PASS, tier="edge", backend="test")
        assert v.attempt == 1

    def test_timestamp_set(self) -> None:
        v = VerifyVerdict(verdict=Verdict.PASS, tier="edge", backend="test")
        assert v.timestamp > 0


class TestEvidenceHash:
    def test_deterministic(self) -> None:
        evidence = {"a": 1, "b": [2, 3], "c": "hello"}
        h1 = verify_evidence_hash(evidence)
        h2 = verify_evidence_hash(evidence)
        assert h1 == h2

    def test_key_order_irrelevant(self) -> None:
        e1 = {"b": 2, "a": 1}
        e2 = {"a": 1, "b": 2}
        assert verify_evidence_hash(e1) == verify_evidence_hash(e2)

    def test_different_content_different_hash(self) -> None:
        e1 = {"a": 1}
        e2 = {"a": 2}
        assert verify_evidence_hash(e1) != verify_evidence_hash(e2)

    def test_sha256_prefix(self) -> None:
        h = verify_evidence_hash({})
        assert h.startswith("sha256:")
        # 64 hex chars after prefix.
        assert len(h) == len("sha256:") + 64

    def test_empty_evidence(self) -> None:
        h = verify_evidence_hash({})
        assert h.startswith("sha256:")
