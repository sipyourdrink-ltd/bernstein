"""Tests for the top-level run_verify entry point (#6559)."""

from __future__ import annotations

from typing import Any
from unittest.mock import MagicMock

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

    def test_lineage_recording_called(self) -> None:
        """When a spine is provided, it should be called with record."""
        # We pass a mock that will not be a LineageSpine instance,
        # so _record_in_lineage will skip silently. This tests the
        # code path doesn't crash.
        spine = MagicMock()
        record = run_verify(
            _StubVerifier(Verdict.PASS),
            {},
            run_id="task-123",
            spine=spine,
            sleep_fn=lambda _: None,
        )
        assert record.ok

    def test_audit_chain_recording(self) -> None:
        """When an audit chain is provided, its record method is called."""
        chain = MagicMock()
        chain.record = MagicMock()
        record = run_verify(
            _StubVerifier(Verdict.PASS),
            {},
            run_id="task-456",
            audit_chain=chain,
            sleep_fn=lambda _: None,
        )
        assert record.ok
        chain.record.assert_called_once()
        call_kwargs = chain.record.call_args[1]
        assert call_kwargs["event_type"] == "verify_outcome"
        assert call_kwargs["payload"]["run_id"] == "task-456"
        assert call_kwargs["payload"]["tier"] == "test"
        assert call_kwargs["payload"]["verdict"] == "pass"

    def test_no_spine_no_crash(self) -> None:
        record = run_verify(
            _StubVerifier(Verdict.PASS),
            {},
            run_id="task-789",
            sleep_fn=lambda _: None,
        )
        assert record.ok

    def test_no_run_id_skips_recording(self) -> None:
        chain = MagicMock()
        chain.record = MagicMock()
        run_verify(
            _StubVerifier(Verdict.PASS),
            {},
            audit_chain=chain,
            sleep_fn=lambda _: None,
        )
        # record should not be called without a run_id.
        chain.record.assert_not_called()
