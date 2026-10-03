"""Retry patching reads the escalation ladder (issue #4855 slice 2)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from bernstein.core.models import Complexity, Scope, Task, TaskStatus, TaskType

from bernstein.core.routing.escalation_ladder import (
    EVIDENCE_VERIFICATION_FAILURE,
    FAILURE_EVIDENCE_CLASS_METADATA_KEY,
    FAILURE_EVIDENCE_DIGEST_METADATA_KEY,
    LADDER_STEP_METADATA_KEY,
    REASON_ADVANCED,
    REASON_BUDGET_STOP,
    REASON_MISSING_EVIDENCE,
    REASON_UNKNOWN_EVIDENCE_CLASS,
    FailureEvidence,
    apply_retry_ladder_to_patch,
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
)

_DIGEST = "c" * 64
_EVIDENCE = FailureEvidence(evidence_class=EVIDENCE_VERIFICATION_FAILURE, digest=_DIGEST)
_QWEN_LADDER = [
    {"model": "qwen2.5-coder-7b", "adapter": "qwen", "max_attempts": 1},
    {"model": "qwen2.5-coder-32b", "adapter": "qwen", "max_attempts": 1},
    {"model": "qwen2.5-coder-72b", "adapter": "qwen", "max_attempts": 1},
]
_POLICY = {"backend": {"model": "qwen2.5-coder-7b", "ladder": _QWEN_LADDER}}


def _plan(**overrides: Any) -> Any:
    kwargs: dict[str, Any] = {
        "role": "backend",
        "role_model_policy": _POLICY,
        "task_model": "qwen2.5-coder-7b",
        "task_metadata": {},
        "legacy_fallback_model": "opus",
        "evidence": _EVIDENCE,
    }
    kwargs.update(overrides)
    return plan_retry_model_patch(**kwargs)


def _canonical(body: dict[str, Any]) -> str:
    return json.dumps(body, sort_keys=True, separators=(",", ":"))


def test_unset_ladder_keeps_cascade_fallback_byte_identical() -> None:
    """No ladder: the compaction patch still carries the cascade model and nothing else."""
    plan = plan_retry_model_patch(
        role="backend",
        role_model_policy=None,
        task_model="sonnet",
        task_metadata={},
        legacy_fallback_model="gpt-5.4-mini",
        evidence=_EVIDENCE,
    )
    base = {"description": "compacted", "meta_messages": ["CONTEXT COMPACTION: focus"]}
    body = apply_retry_ladder_to_patch(patch_body=base, plan=plan, base_metadata={})
    assert body == {
        "description": "compacted",
        "meta_messages": ["CONTEXT COMPACTION: focus"],
        "model": "gpt-5.4-mini",
    }
    assert plan.decision is None


def test_ladder_without_evidence_refuses_and_does_not_stamp_fallback(tmp_path: Path) -> None:
    plan = _plan(evidence=None, task_metadata={}, legacy_fallback_model="gpt-5.4-mini")
    assert plan.model is None
    assert plan.decision is not None
    assert plan.decision.kind == "refuse"
    assert plan.decision.reason == REASON_MISSING_EVIDENCE

    chain = AuditChainStore(tmp_path / "audit", key=b"0" * 32)
    record_ladder_decision(chain=chain, run_id="run-1", task_id="T-1", decision=plan.decision)
    rows = chain.query(event_type=EVENT_ESCALATION_LADDER_REFUSAL)
    assert len(rows) == 1
    assert rows[0].details["reason"] == REASON_MISSING_EVIDENCE


def test_unknown_evidence_class_refused_like_missing() -> None:
    plan = _plan(evidence=FailureEvidence(evidence_class="vibes", digest=_DIGEST))
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
    first = apply_retry_ladder_to_patch(patch_body=dict(base), plan=plan, base_metadata={})
    second = apply_retry_ladder_to_patch(patch_body=dict(base), plan=plan, base_metadata={})
    assert _canonical(first) == _canonical(second)
    assert plan.escalation_context in first["meta_messages"]
    assert first["model"] == "qwen2.5-coder-32b"
    assert first["cli"] == "qwen"

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
        base_metadata={"original_task_id": "T-1"},
    )
    # Compaction discarded the escalation line and the task model looks like step 0 again.
    second = _plan(
        task_model="qwen2.5-coder-7b",
        task_metadata=body["metadata"],
        legacy_fallback_model="haiku",
    )
    assert second.decision is not None
    assert second.decision.from_step == 1
    assert second.decision.to_step == 2
    assert second.decision.to_step > second.decision.from_step
    assert second.model == "qwen2.5-coder-72b"
    assert second.metadata_updates[LADDER_STEP_METADATA_KEY] == 2


def test_budget_guard_stops_the_retry_patch(tmp_path: Path) -> None:
    policy = {
        "backend": {
            "model": "qwen2.5-coder-7b",
            "ladder": _QWEN_LADDER,
            "escalation_budget_usd": 1.0,
        }
    }
    plan = _plan(
        role_model_policy=policy,
        spend_usd=0.8,
        estimated_next_step_usd=0.5,
        legacy_fallback_model="opus",
    )
    assert plan.model is None
    assert plan.decision is not None
    assert plan.decision.kind == "budget_stop"
    assert plan.decision.reason == REASON_BUDGET_STOP

    chain = AuditChainStore(tmp_path / "audit", key=b"0" * 32)
    record_ladder_decision(chain=chain, run_id="run-1", task_id="T-b", decision=plan.decision)
    assert chain.query(event_type=EVENT_ESCALATION_LADDER_BUDGET_STOP)[0].details["reason"] == REASON_BUDGET_STOP


def test_evidence_on_task_metadata_causes_the_hop() -> None:
    plan = _plan(
        evidence=None,
        task_metadata={
            FAILURE_EVIDENCE_CLASS_METADATA_KEY: EVIDENCE_VERIFICATION_FAILURE,
            FAILURE_EVIDENCE_DIGEST_METADATA_KEY: _DIGEST,
        },
    )
    assert plan.model == "qwen2.5-coder-32b"
    assert plan.decision is not None
    assert plan.decision.kind == "advance"


def test_patch_retry_records_hop_and_keeps_vendor_model(tmp_path: Path) -> None:
    from bernstein.core.agents.agent_lifecycle import _patch_retry_with_compaction

    task = Task(
        id="T-1",
        title="Implement feature",
        description="Write the code",
        role="backend",
        status=TaskStatus.OPEN,
        scope=Scope.MEDIUM,
        complexity=Complexity.MEDIUM,
        task_type=TaskType.STANDARD,
        model="qwen2.5-coder-7b",
        metadata={
            FAILURE_EVIDENCE_CLASS_METADATA_KEY: EVIDENCE_VERIFICATION_FAILURE,
            FAILURE_EVIDENCE_DIGEST_METADATA_KEY: _DIGEST,
        },
    )
    client = MagicMock()
    listed = MagicMock()
    listed.raise_for_status.return_value = None
    listed.json.return_value = [{"id": "T-retry-1", "title": "[RETRY 1] Implement feature", "status": "open"}]
    client.get.return_value = listed
    patched = MagicMock()
    patched.raise_for_status.return_value = None
    client.patch.return_value = patched

    _patch_retry_with_compaction(
        client=client,
        server_url="http://server",
        original_task=task,
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
