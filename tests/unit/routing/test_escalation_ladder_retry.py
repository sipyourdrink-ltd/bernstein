"""Retry patching reads the escalation ladder (issue #4855 slice 2)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

from bernstein.core.models import Complexity, Scope, Task, TaskStatus, TaskType

from bernstein.core.cost.cost_tracker import CostTracker
from bernstein.core.routing.escalation_ladder import (
    EVIDENCE_VERIFICATION_FAILURE,
    REASON_ADVANCED,
    REASON_BUDGET_STOP,
    REASON_BUDGET_UNPRICED,
    REASON_MISSING_EVIDENCE,
    REASON_UNKNOWN_EVIDENCE_CLASS,
    FailureEvidence,
    apply_retry_ladder_to_patch,
    evidence_from_chain,
    format_escalation_context,
    hop_record_digest,
    plan_retry_model_patch,
    record_ladder_decision,
    verify_hop_record_digest,
)
from bernstein.core.security.audit_chain import (
    EVENT_ESCALATION_LADDER_BUDGET_STOP,
    EVENT_ESCALATION_LADDER_HOP,
    EVENT_ESCALATION_LADDER_REFUSAL,
    AuditChainStore,
    record_escalation_ladder_failure_evidence,
)

_DIGEST = "c" * 64
_EVIDENCE = FailureEvidence(evidence_class=EVIDENCE_VERIFICATION_FAILURE, digest=_DIGEST)
_QWEN_LADDER = [
    {"model": "qwen2.5-coder-7b", "adapter": "qwen", "max_attempts": 1},
    {"model": "qwen2.5-coder-32b", "adapter": "qwen", "max_attempts": 1},
    {"model": "qwen2.5-coder-72b", "adapter": "qwen", "max_attempts": 1},
]
_POLICY = {"backend": {"model": "qwen2.5-coder-7b", "ladder": _QWEN_LADDER}}
_BUDGET_POLICY = {
    "backend": {
        "model": "qwen2.5-coder-7b",
        "ladder": _QWEN_LADDER,
        "escalation_budget_usd": 1.0,
    }
}


def _plan(**overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "role": "backend",
        "role_model_policy": _POLICY,
        "task_model": "qwen2.5-coder-7b",
        "task_metadata": {},
        "legacy_fallback_model": "opus",
        "resolve_evidence": lambda: _EVIDENCE,
    }
    kwargs.update(overrides)
    return plan_retry_model_patch(**kwargs)


def _canonical(body: dict[str, Any]) -> str:
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def _task(**overrides: Any) -> Task:
    fields: dict[str, Any] = {
        "id": "T-1",
        "title": "Implement feature",
        "description": "Write the code",
        "role": "backend",
        "status": TaskStatus.OPEN,
        "scope": Scope.MEDIUM,
        "complexity": Complexity.MEDIUM,
        "task_type": TaskType.STANDARD,
        "model": "qwen2.5-coder-7b",
    }
    fields.update(overrides)
    return Task(**fields)


def _patch_client() -> MagicMock:
    client = MagicMock()
    listed = MagicMock()
    listed.raise_for_status.return_value = None
    listed.json.return_value = [{"id": "T-retry-1", "title": "[RETRY 1] Implement feature", "status": "open"}]
    client.get.return_value = listed
    patched = MagicMock()
    patched.raise_for_status.return_value = None
    client.patch.return_value = patched
    return client


def test_unset_ladder_keeps_cascade_fallback_byte_identical() -> None:
    """No ladder: the compaction patch still carries the cascade model and nothing else."""
    plan = plan_retry_model_patch(
        role="backend",
        role_model_policy=None,
        task_model="sonnet",
        task_metadata={},
        legacy_fallback_model="gpt-5.4-mini",
        resolve_evidence=lambda: _EVIDENCE,
    )
    base = {"description": "compacted", "meta_messages": ["CONTEXT COMPACTION: focus"]}
    body = apply_retry_ladder_to_patch(patch_body=base, plan=plan)
    assert body == {
        "description": "compacted",
        "meta_messages": ["CONTEXT COMPACTION: focus"],
        "model": "gpt-5.4-mini",
    }
    assert plan.decision is None


def test_unset_ladder_never_resolves_evidence() -> None:
    """The chain lookup only runs when a ladder is configured."""
    resolver = MagicMock(return_value=_EVIDENCE)
    plan_retry_model_patch(
        role="backend",
        role_model_policy=None,
        task_model="sonnet",
        task_metadata={},
        legacy_fallback_model=None,
        resolve_evidence=resolver,
    )
    resolver.assert_not_called()


def test_ladder_without_evidence_refuses_and_does_not_stamp_fallback(tmp_path: Path) -> None:
    plan = _plan(resolve_evidence=lambda: None, legacy_fallback_model="gpt-5.4-mini")
    assert plan.model is None
    assert plan.decision is not None
    assert plan.decision.kind == "refuse"
    assert plan.decision.reason == REASON_MISSING_EVIDENCE

    chain = AuditChainStore(tmp_path / "audit", key=b"0" * 32)
    record_ladder_decision(chain=chain, run_id="run-1", task_id="T-1", decision=plan.decision)
    rows = chain.query(event_type=EVENT_ESCALATION_LADDER_REFUSAL)
    assert len(rows) == 1
    assert rows[0].details["reason"] == REASON_MISSING_EVIDENCE


def test_evidence_written_to_task_metadata_does_not_cause_a_hop() -> None:
    """Metadata is client-writable, so evidence keys there are ignored."""
    plan = _plan(
        resolve_evidence=lambda: None,
        task_metadata={
            "failure_evidence_class": EVIDENCE_VERIFICATION_FAILURE,
            "failure_evidence_digest": _DIGEST,
            "spend_usd": 0.0,
        },
    )
    assert plan.model is None
    assert plan.decision is not None
    assert plan.decision.reason == REASON_MISSING_EVIDENCE


def test_unknown_evidence_class_refused_like_missing() -> None:
    plan = _plan(resolve_evidence=lambda: FailureEvidence(evidence_class="vibes", digest=_DIGEST))
    assert plan.model is None
    assert plan.decision is not None
    assert plan.decision.reason == REASON_UNKNOWN_EVIDENCE_CLASS


def test_non_claude_model_names_pass_through_on_the_retry_patch() -> None:
    """The hop hands the next adapter its own model id, not a Claude tier name."""
    plan = _plan()
    assert plan.model == "qwen2.5-coder-32b"
    assert plan.cli == "qwen"
    assert plan.escalation_context == format_escalation_context(
        from_step=0,
        to_step=1,
        attempts=1,
        evidence_class=EVIDENCE_VERIFICATION_FAILURE,
    )
    for banned in ("opus", "sonnet", "haiku"):
        assert banned not in (plan.model or "")

    base = {"description": "compacted", "meta_messages": ["CONTEXT COMPACTION: focus"]}
    first = apply_retry_ladder_to_patch(patch_body=dict(base), plan=plan)
    second = apply_retry_ladder_to_patch(patch_body=dict(base), plan=plan)
    assert _canonical(first) == _canonical(second)
    assert plan.escalation_context in first["meta_messages"]
    assert first["model"] == "qwen2.5-coder-32b"
    assert first["cli"] == "qwen"
    assert first["escalation_ladder_step"] == 1
    assert first["escalation_ladder_attempts"] == 1
    assert "metadata" not in first

    record = plan.decision.to_record_dict(task_id="T-1")
    digest = hop_record_digest(record)
    assert verify_hop_record_digest(record, digest)
    assert record["escalation_context"] == plan.escalation_context


def test_compaction_cannot_step_the_ladder_down() -> None:
    """A compacted retry that still names the first model stays on the current rung."""
    first = _plan()
    assert first.decision is not None
    assert first.decision.to_step == 1
    body = apply_retry_ladder_to_patch(
        patch_body={"description": "compacted", "meta_messages": ["CONTEXT COMPACTION: focus"]},
        plan=first,
    )
    # What the task store persists from that PATCH body.
    persisted = {
        "original_task_id": "T-1",
        "escalation_ladder_step": body["escalation_ladder_step"],
        "escalation_ladder_attempts": body["escalation_ladder_attempts"],
    }
    # Compaction discarded the escalation line and the task model looks like step 0 again.
    second = _plan(
        task_model="qwen2.5-coder-7b",
        task_metadata=persisted,
        legacy_fallback_model="haiku",
    )
    assert second.decision is not None
    assert second.decision.from_step == 1
    assert second.decision.to_step == 2
    assert second.model == "qwen2.5-coder-72b"
    assert second.ladder_step == 2


def test_budget_guard_stops_the_retry_patch(tmp_path: Path) -> None:
    plan = _plan(
        role_model_policy=_BUDGET_POLICY,
        spend_usd=0.8,
        estimated_next_step_usd=0.5,
    )
    assert plan.model is None
    assert plan.decision is not None
    assert plan.decision.kind == "budget_stop"
    assert plan.decision.reason == REASON_BUDGET_STOP

    chain = AuditChainStore(tmp_path / "audit", key=b"0" * 32)
    record_ladder_decision(chain=chain, run_id="run-1", task_id="T-b", decision=plan.decision)
    assert chain.query(event_type=EVENT_ESCALATION_LADDER_BUDGET_STOP)[0].details["reason"] == REASON_BUDGET_STOP


def test_failed_estimate_does_not_advance_under_a_budget() -> None:
    """An estimator that cannot quote must not price the next rung at zero."""
    returns_none = _plan(role_model_policy=_BUDGET_POLICY, spend_usd=0.0, estimate_model_usd=lambda _m: None)

    def _boom(_model: str) -> float:
        raise RuntimeError("no pricing")

    raises = _plan(role_model_policy=_BUDGET_POLICY, spend_usd=0.0, estimate_model_usd=_boom)
    for plan in (returns_none, raises):
        assert plan.model is None
        assert plan.decision is not None
        assert plan.decision.kind == "budget_stop"
        assert plan.decision.reason == REASON_BUDGET_UNPRICED


def test_unknown_spend_does_not_advance_under_a_budget() -> None:
    plan = _plan(role_model_policy=_BUDGET_POLICY, spend_usd=None, estimated_next_step_usd=0.1)
    assert plan.model is None
    assert plan.decision is not None
    assert plan.decision.reason == REASON_BUDGET_UNPRICED


def test_unknown_cost_is_irrelevant_without_a_budget() -> None:
    plan = _plan(spend_usd=None, estimate_model_usd=lambda _m: None)
    assert plan.model == "qwen2.5-coder-32b"


def test_evidence_resolves_from_a_verified_chain_record(tmp_path: Path) -> None:
    chain = AuditChainStore(tmp_path / "audit", key=b"0" * 32)
    assert evidence_from_chain(chain, "T-1") is None

    record_escalation_ladder_failure_evidence(
        chain=chain,
        run_id="run-1",
        task_id="T-1",
        evidence_class=EVIDENCE_VERIFICATION_FAILURE,
        evidence_digest=f"sha256:{_DIGEST}",
    )
    resolved = evidence_from_chain(chain, "T-1")
    assert resolved is not None
    assert resolved.evidence_class == EVIDENCE_VERIFICATION_FAILURE
    assert resolved.normalized_digest() == _DIGEST
    assert evidence_from_chain(chain, "T-other") is None


def test_tampered_chain_evidence_does_not_resolve(tmp_path: Path) -> None:
    audit_dir = tmp_path / "audit"
    chain = AuditChainStore(audit_dir, key=b"0" * 32)
    record_escalation_ladder_failure_evidence(
        chain=chain,
        run_id="run-1",
        task_id="T-1",
        evidence_class=EVIDENCE_VERIFICATION_FAILURE,
        evidence_digest=f"sha256:{_DIGEST}",
    )
    logs = [p for p in audit_dir.rglob("*.jsonl") if _DIGEST in p.read_text(encoding="utf-8")]
    assert logs
    for log in logs:
        log.write_text(log.read_text(encoding="utf-8").replace(_DIGEST, "d" * 64), encoding="utf-8")

    reopened = AuditChainStore(audit_dir, key=b"0" * 32)
    assert evidence_from_chain(reopened, "T-1") is None


def test_lineage_spend_reads_the_cost_ledger() -> None:
    from bernstein.core.agents.agent_lifecycle import _lineage_spend_usd

    tracker = CostTracker(run_id="r-1")
    tracker.record("a-1", "T-1", "sonnet", 0, 0, cost_usd=0.25)
    tracker.record("a-2", "T-1-r1", "sonnet", 0, 0, cost_usd=0.5)
    tracker.record("a-3", "T-unrelated", "sonnet", 0, 0, cost_usd=9.0)
    failed_retry = _task(id="T-1-r1", metadata={"original_task_id": "T-1", "spend_usd": 0.0})
    snapshot = {"failed": [_task(), failed_retry], "open": [_task(id="T-unrelated")]}

    spent = _lineage_spend_usd(SimpleNamespace(_cost_tracker=tracker), failed_retry, snapshot)
    assert spent == 0.75
    assert _lineage_spend_usd(SimpleNamespace(), failed_retry, snapshot) is None


def test_patch_retry_records_hop_and_keeps_vendor_model(tmp_path: Path) -> None:
    from bernstein.core.agents.agent_lifecycle import _patch_retry_with_compaction

    record_escalation_ladder_failure_evidence(
        chain=AuditChainStore(tmp_path / ".sdd" / "audit"),
        run_id="run-9",
        task_id="T-1",
        evidence_class=EVIDENCE_VERIFICATION_FAILURE,
        evidence_digest=f"sha256:{_DIGEST}",
    )
    client = _patch_client()

    _patch_retry_with_compaction(
        client=client,
        server_url="http://server",
        original_task=_task(),
        compacted_description="shorter",
        fallback_model="opus",
        role_model_policy=_POLICY,
        workdir=tmp_path,
        run_id="run-9",
    )

    body = client.patch.call_args.kwargs["json"]
    assert body["model"] == "qwen2.5-coder-32b"
    assert body["cli"] == "qwen"
    assert "opus" not in body["model"]
    assert any(line.startswith("ESCALATION:") for line in body["meta_messages"])
    assert body["escalation_ladder_step"] == 1
    assert "metadata" not in body

    chain = AuditChainStore(tmp_path / ".sdd" / "audit")
    hops = chain.query(event_type=EVENT_ESCALATION_LADDER_HOP)
    assert len(hops) == 1
    assert hops[0].details["escalation_context"] in body["meta_messages"]
    assert verify_hop_record_digest(
        {
            "task_id": "T-1",
            "kind": "advance",
            "from_step": 0,
            "to_step": 1,
            "reason": REASON_ADVANCED,
            "evidence_class": EVIDENCE_VERIFICATION_FAILURE,
            "evidence_digest": f"sha256:{_DIGEST}",
            "ladder_policy_version": 1,
            "escalation_context": hops[0].details["escalation_context"],
        },
        hops[0].details["hop_digest"],
    )


def test_patch_retry_ignores_metadata_evidence_without_a_chain_record(tmp_path: Path) -> None:
    """A task carrying fabricated evidence keys gets a recorded refusal, not a hop."""
    from bernstein.core.agents.agent_lifecycle import _patch_retry_with_compaction

    client = _patch_client()
    _patch_retry_with_compaction(
        client=client,
        server_url="http://server",
        original_task=_task(
            metadata={
                "failure_evidence_class": EVIDENCE_VERIFICATION_FAILURE,
                "failure_evidence_digest": _DIGEST,
            }
        ),
        compacted_description="shorter",
        fallback_model="opus",
        role_model_policy=_POLICY,
        workdir=tmp_path,
        run_id="run-9",
    )

    body = client.patch.call_args.kwargs["json"]
    assert "model" not in body
    assert "cli" not in body
    chain = AuditChainStore(tmp_path / ".sdd" / "audit")
    assert chain.query(event_type=EVENT_ESCALATION_LADDER_HOP) == []
    refusals = chain.query(event_type=EVENT_ESCALATION_LADDER_REFUSAL)
    assert [r.details["reason"] for r in refusals] == [REASON_MISSING_EVIDENCE]
