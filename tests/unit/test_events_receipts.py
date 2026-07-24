"""Receipt-backed automation: receipt before effect, negative proofs (#2548).

Covers acceptance criteria:
  * Receipt before effect - every rule fire writes a fire receipt (rule hash,
    matched event hashes, rendered action) to the chain before the action runs;
    the same rule against the same event bytes renders a byte-identical action;
    an executed action without a matching receipt is rejected.
  * Absence with negative proof - an expired expectation emits a chain receipt
    asserting no matching event exists between two named chain positions; a
    verifier confirms it against the slice; injecting a matching event fails it.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path

import pytest

from bernstein.core.events.actions import ActionSpec, render_action, rule_hash
from bernstein.core.events.feed import project_window
from bernstein.core.events.grammar import CanonicalEvent
from bernstein.core.events.receipts import (
    ActionRejected,
    FireReceipt,
    build_absence_receipt,
    build_fire_receipt,
    dispatch_action,
    verify_absence_receipt,
    verify_fire_receipt,
)
from bernstein.core.events.triggers import AbsenceExpectation, evaluate_absence
from bernstein.core.security.audit_chain import (
    EVENT_AUTOMATION_ACTION,
    EVENT_RULE_FIRE_RECEIPT,
    AuditChainStore,
)
from bernstein.core.security.audit_slice import slice_audit_log


def _event(position: int, label: str, resource_id: str) -> CanonicalEvent:
    return CanonicalEvent(
        position=position,
        hmac=f"hmac{position:04d}",
        prev_hmac=f"hmac{position - 1:04d}" if position else "0" * 64,
        resource_id=resource_id,
        label=label,
        related_resource_ids=(),
        payload_digest="sha256:" + "0" * 64,
        timestamp=f"2026-07-17T00:00:{position:02d}.000000Z",
        actor="worker",
    )


_RULE_SPEC = {
    "id": "pause_on_gate_storm",
    "when": {"threshold": {"label": "gate.result", "count": 3, "window": 10}},
    "action": {
        "kind": "schedule.pause",
        "params": {"schedule_id": "nightly", "reason": "gate storm on {event.resource_id}"},
    },
}


def test_render_action_is_byte_identical() -> None:
    trigger = _event(2, "gate.result", "adapter_v3")
    spec = ActionSpec(kind="schedule.pause", params={"schedule_id": "nightly", "reason": "storm {event.resource_id}"})
    a = render_action(spec, trigger)
    b = render_action(spec, trigger)
    assert a.digest == b.digest
    assert a.params["reason"] == "storm adapter_v3"


def test_dispatch_writes_receipt_before_effect(tmp_path: Path) -> None:
    chain = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
    matched = [_event(0, "gate.result", "g"), _event(1, "gate.result", "g"), _event(2, "gate.result", "g")]
    trigger = matched[-1]
    action = ActionSpec(kind="schedule.pause", params={"schedule_id": "nightly", "reason": "storm {event.resource_id}"})
    receipt = build_fire_receipt(rule_spec=_RULE_SPEC, matched_events=matched, action=action, triggering_event=trigger)
    rendered = render_action(action, trigger)

    observed: list[str] = []

    def effect(rendered_action: object) -> str:
        # At effect time the fire receipt must already be on the chain.
        receipts_now = chain.query(event_type=EVENT_RULE_FIRE_RECEIPT)
        observed.append("receipt_present" if receipts_now else "receipt_missing")
        return "paused"

    result = dispatch_action(chain=chain, receipt=receipt, rendered_action=rendered, effect=effect)

    assert observed == ["receipt_present"]
    assert result.effect_result == "paused"
    assert result.fire_receipt_event.event_type == EVENT_RULE_FIRE_RECEIPT
    assert result.action_event.event_type == EVENT_AUTOMATION_ACTION
    # The executed action references its fire receipt and the triggering event.
    assert result.action_event.details["fire_receipt_hmac"] == result.fire_receipt_event.hmac
    assert result.action_event.details["triggering_event_hmac"] == trigger.hmac
    ok, errors = chain.verify()
    assert ok, errors


def test_action_without_matching_receipt_is_rejected(tmp_path: Path) -> None:
    chain = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
    trigger = _event(0, "gate.result", "g")
    authorised = ActionSpec(kind="schedule.pause", params={"schedule_id": "nightly"})
    receipt = build_fire_receipt(
        rule_spec=_RULE_SPEC, matched_events=[trigger], action=authorised, triggering_event=trigger
    )
    # A different action than the receipt commits to.
    tampered = render_action(ActionSpec(kind="schedule.resume", params={"schedule_id": "nightly"}), trigger)

    called = False

    def effect(_rendered: object) -> None:
        nonlocal called
        called = True

    try:
        dispatch_action(chain=chain, receipt=receipt, rendered_action=tampered, effect=effect)
    except ActionRejected:
        pass
    else:  # pragma: no cover - the dispatch must reject
        raise AssertionError("tampered action was not rejected")

    assert called is False


def test_verify_fire_receipt_detects_divergence() -> None:
    trigger = _event(0, "gate.result", "g")
    action = ActionSpec(kind="schedule.pause", params={"schedule_id": "nightly", "reason": "storm {event.resource_id}"})
    receipt = build_fire_receipt(
        rule_spec=_RULE_SPEC, matched_events=[trigger], action=action, triggering_event=trigger
    )
    events_by_hmac = {trigger.hmac: trigger}

    ok, errors = verify_fire_receipt(receipt, rule_spec=_RULE_SPEC, action=action, events_by_hmac=events_by_hmac)
    assert ok, errors

    # A different rule spec breaks the rule-hash commitment.
    bad_ok, bad_errors = verify_fire_receipt(
        receipt, rule_spec={"id": "other"}, action=action, events_by_hmac=events_by_hmac
    )
    assert not bad_ok
    assert any("rule hash" in e for e in bad_errors)


def test_absence_receipt_verifies_and_fails_on_injection(tmp_path: Path) -> None:
    chain = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
    chain.log_with_prev_digest(
        event_type="run.started", actor="orchestrator", resource_type="run", resource_id="run_1", details={}
    )
    chain.log_with_prev_digest(
        event_type="cost.update", actor="orchestrator", resource_type="run", resource_id="run_1", details={}
    )
    chain.log_with_prev_digest(
        event_type="cost.update", actor="orchestrator", resource_type="run", resource_id="run_1", details={}
    )
    window = project_window(slice_audit_log(tmp_path / "audit"))

    # within=2 so the deadline (chain position 2) is observed by the slice.
    violations = evaluate_absence(
        AbsenceExpectation(after="run.started", expect="run.completed", within=2), window.events
    )
    assert len(violations) == 1
    receipt = build_absence_receipt(violations[0])

    ok, errors = verify_absence_receipt(receipt, window.events)
    assert ok, errors

    # Inject a matching event into the asserted span: the negative proof fails.
    injected = CanonicalEvent(
        position=1,
        hmac="injected",
        prev_hmac=window.events[0].hmac,
        resource_id="run_1",
        label="run.completed",
        related_resource_ids=(),
        payload_digest="sha256:" + "0" * 64,
        timestamp="2026-07-17T00:00:01.500000Z",
        actor="orchestrator",
    )
    tampered_window = [window.events[0], injected, *window.events[1:]]
    bad_ok, bad_errors = verify_absence_receipt(receipt, tampered_window)
    assert not bad_ok
    assert any("matching event present" in e for e in bad_errors)


# ---------------------------------------------------------------------------
# Fire-receipt binding checks (#2653, item 9)
# ---------------------------------------------------------------------------


def test_build_fire_receipt_requires_triggering_in_matched() -> None:
    trigger = _event(0, "gate.result", "g")
    other = _event(1, "gate.result", "h")
    action = ActionSpec(kind="schedule.pause", params={"schedule_id": "nightly"})
    # The action is rendered against ``trigger`` but ``trigger`` is not among the
    # matched events -> the receipt would authorise an unmatched triggering event.
    with pytest.raises(ValueError):
        build_fire_receipt(rule_spec=_RULE_SPEC, matched_events=[other], action=action, triggering_event=trigger)


def test_verify_fire_receipt_rejects_triggering_not_in_matched() -> None:
    trigger = _event(0, "gate.result", "g")
    other = _event(1, "gate.result", "h")
    action = ActionSpec(kind="schedule.pause", params={"schedule_id": "nightly"})
    rendered = render_action(action, trigger)
    # Hand-craft a receipt whose matched set excludes the triggering event.
    receipt = FireReceipt(
        rule_hash=rule_hash(_RULE_SPEC),
        matched_event_hmacs=(other.hmac,),
        action_kind=rendered.kind,
        action_digest=rendered.digest,
        rendered_action=rendered.to_dict(),
    )
    events_by_hmac = {trigger.hmac: trigger, other.hmac: other}

    ok, errors = verify_fire_receipt(receipt, rule_spec=_RULE_SPEC, action=action, events_by_hmac=events_by_hmac)
    assert not ok
    assert any("triggering event not among matched" in e for e in errors)


def test_verify_fire_receipt_rejects_action_kind_mismatch() -> None:
    trigger = _event(0, "gate.result", "g")
    action = ActionSpec(kind="schedule.pause", params={"schedule_id": "nightly"})
    receipt = build_fire_receipt(
        rule_spec=_RULE_SPEC, matched_events=[trigger], action=action, triggering_event=trigger
    )
    # A receipt whose action_kind no longer matches what the action re-renders to.
    tampered = dataclasses.replace(receipt, action_kind="notify")
    events_by_hmac = {trigger.hmac: trigger}

    ok, errors = verify_fire_receipt(tampered, rule_spec=_RULE_SPEC, action=action, events_by_hmac=events_by_hmac)
    assert not ok
    assert any("action kind diverges" in e for e in errors)


# ---------------------------------------------------------------------------
# Dispatch audit continuity (#2653, item 10)
# ---------------------------------------------------------------------------


def _dispatch_fixture() -> tuple[ActionSpec, CanonicalEvent, FireReceipt, object]:
    trigger = _event(0, "gate.result", "g")
    action = ActionSpec(kind="schedule.pause", params={"schedule_id": "nightly"})
    receipt = build_fire_receipt(
        rule_spec=_RULE_SPEC, matched_events=[trigger], action=action, triggering_event=trigger
    )
    rendered = render_action(action, trigger)
    return action, trigger, receipt, rendered


def test_dispatch_writes_pending_intent_before_effect(tmp_path: Path) -> None:
    chain = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
    _action, _trigger, receipt, rendered = _dispatch_fixture()

    saw_pending: list[bool] = []

    def effect(_rendered: object) -> str:
        pending = [
            e for e in chain.query(event_type=EVENT_AUTOMATION_ACTION) if e.details.get("result_status") == "pending"
        ]
        saw_pending.append(bool(pending))
        return "paused"

    dispatch_action(chain=chain, receipt=receipt, rendered_action=rendered, effect=effect)  # type: ignore[arg-type]
    # The intent/pending record is on the chain before the effect runs.
    assert saw_pending == [True]


def test_dispatch_records_failure_outcome_on_effect_exception(tmp_path: Path) -> None:
    chain = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
    _action, _trigger, receipt, rendered = _dispatch_fixture()

    def effect(_rendered: object) -> str:
        raise RuntimeError("boom")

    with pytest.raises(RuntimeError, match="boom"):
        dispatch_action(chain=chain, receipt=receipt, rendered_action=rendered, effect=effect)  # type: ignore[arg-type]

    statuses = [e.details.get("result_status") for e in chain.query(event_type=EVENT_AUTOMATION_ACTION)]
    # No audit gap: an intent and a failure outcome both landed despite the raise.
    assert "pending" in statuses
    assert "failed" in statuses
    ok, errors = chain.verify()
    assert ok, errors


def test_dispatch_is_idempotent_on_replay(tmp_path: Path) -> None:
    chain = AuditChainStore(tmp_path / "audit", key=b"k" * 32)
    _action, _trigger, receipt, rendered = _dispatch_fixture()

    calls: list[int] = []

    def effect(_rendered: object) -> str:
        calls.append(1)
        return "paused"

    first = dispatch_action(chain=chain, receipt=receipt, rendered_action=rendered, effect=effect)  # type: ignore[arg-type]
    second = dispatch_action(chain=chain, receipt=receipt, rendered_action=rendered, effect=effect)  # type: ignore[arg-type]

    # The effect ran exactly once; the replay is keyed on the fire receipt.
    assert calls == [1]
    assert first.effect_result == "paused"
    assert second.effect_result is None
    completed = [
        e for e in chain.query(event_type=EVENT_AUTOMATION_ACTION) if e.details.get("result_status") == "dispatched"
    ]
    assert len(completed) == 1
    ok, errors = chain.verify()
    assert ok, errors
