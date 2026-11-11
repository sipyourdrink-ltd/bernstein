"""Tests for the top-level run_verify entry point (#6559)."""

from __future__ import annotations

from typing import Any

import pytest

from bernstein.core.lineage.spine import LineageSpine
from bernstein.core.security.audit_chain import AuditChainStore
from bernstein.core.verification import (
    Verdict,
    VerifyContract,
    VerifyVerdict,
    run_verify,
    verify_evidence_hash,
)


class _StubVerifier(VerifyContract):
    def __init__(self, verdict: Verdict = Verdict.PASS) -> None:
        self._verdict = verdict

    @property
    def tier(self) -> str:
        return "test"

    def verify(self, task_result: dict[str, Any], *, attempt: int = 1) -> VerifyVerdict:
        evidence = {"stub": True}
        return VerifyVerdict(
            verdict=self._verdict,
            tier=self.tier,
            backend="stub",
            evidence=evidence,
            evidence_hash=verify_evidence_hash(evidence),
            attempt=attempt,
        )

    def max_retries(self) -> int:
        return 0


class TestRunVerify:
    def test_pass_returns_record(self) -> None:
        record = run_verify(
            _StubVerifier(Verdict.PASS),
            {},
            sleep_fn=lambda _: None,
        )
        assert record.ok
        assert record.final_verdict.tier == "test"

    def test_fail_returns_record(self) -> None:
        record = run_verify(
            _StubVerifier(Verdict.FAIL),
            {},
            sleep_fn=lambda _: None,
        )
        assert not record.ok

    def test_lineage_recording_called(self, tmp_path) -> None:
        spine = LineageSpine(tmp_path / "lineage", run_id="task-123", hmac_key=b"key")
        chain = AuditChainStore(tmp_path / "audit", key=b"key")
        record = run_verify(_StubVerifier(), {}, run_id="task-123", spine=spine, audit_chain=chain)
        assert record.recorded
        assert len(list(spine.iter_entries())) == 1
        assert spine.verify().ok

    def test_audit_chain_recording(self, tmp_path) -> None:
        spine = LineageSpine(tmp_path / "lineage", run_id="task-456", hmac_key=b"key")
        chain = AuditChainStore(tmp_path / "audit", key=b"key")
        record = run_verify(_StubVerifier(), {}, run_id="task-456", spine=spine, audit_chain=chain)
        assert record.recorded
        events = chain.query(event_type="verify_outcome", resource_id="task-456")
        assert len(events) == 1
        assert events[0].details["tier"] == "test"
        assert events[0].details["verdict"] == "pass"
        assert chain.verify() == (True, [])

    def test_no_spine_no_crash(self) -> None:
        record = run_verify(
            _StubVerifier(Verdict.PASS),
            {},
            run_id="task-789",
            sleep_fn=lambda _: None,
        )
        assert record.ok

    def test_no_run_id_rejects_recording(self, tmp_path) -> None:
        spine = LineageSpine(tmp_path / "lineage", run_id="task-123", hmac_key=b"key")
        chain = AuditChainStore(tmp_path / "audit", key=b"key")
        with pytest.raises(ValueError, match="run_id"):
            run_verify(_StubVerifier(), {}, spine=spine, audit_chain=chain)
        assert not chain.query()
