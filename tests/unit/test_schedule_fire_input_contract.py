"""Schedule fire input contract: zero-spend refusal + params in projection.

Issue #2545, AC2 + AC6. A parameterized schedule whose params fail their
declared contract is refused at fire time: the dispatch callback (the thing that
spawns adapters) is never invoked, and a signed refusal receipt is anchored in
the audit chain. A valid parameterized fire folds the params hash into the
projection.
"""

from __future__ import annotations

from pathlib import Path

from bernstein.core.lineage.identity import generate_keypair
from bernstein.core.orchestration.schedule_supervisor import ScheduleSupervisor
from bernstein.core.planning.schedule_store import ScheduleStore
from bernstein.core.security.audit_chain import EVENT_INPUT_REFUSAL, AuditChainStore
from bernstein.core.security.input_refusal import read_refusal_receipt, verify_refusal_against_chain


class _SpyDispatch:
    """Stands in for the trigger dispatch -- the surface that spawns adapters."""

    def __init__(self) -> None:
        self.calls: list[object] = []

    def __call__(self, event: object) -> None:
        self.calls.append(event)


def _supervisor(tmp_path: Path, dispatch: _SpyDispatch) -> tuple[ScheduleSupervisor, ScheduleStore, AuditChainStore]:
    sdd = tmp_path / ".sdd"
    store = ScheduleStore(sdd)
    chain = AuditChainStore(sdd / "audit", key=b"k" * 32)
    identity = generate_keypair()
    sup = ScheduleSupervisor(
        store,
        dispatch,
        audit_writer=None,
        refusal_chain=chain,
        install_identity=identity,
    )
    return sup, store, chain


def test_missing_required_param_refused_with_zero_dispatch(tmp_path: Path) -> None:
    dispatch = _SpyDispatch()
    sup, store, chain = _supervisor(tmp_path, dispatch)
    # A schedule declaring a required param but supplying none: the schema shape
    # is well-formed so it registers, but the value is missing at fire time.
    schedule = store.add(
        cron="* * * * *",
        goal="nightly report",
        params_schema=[{"name": "retries", "type": "int", "required": True}],
        params={},
    )

    receipts = sup.tick(now=schedule.created_at + 120)
    refused = [r for r in receipts if r.refused]
    assert refused, "expected at least one refused fire"
    assert dispatch.calls == [], "a refused fire must never dispatch (zero adapter spawns)"
    assert refused[0].projection_hash == "", "a refused fire never projects"

    # A signed refusal receipt was anchored in the chain.
    refusal_events = chain.query(event_type=EVENT_INPUT_REFUSAL)
    assert refusal_events
    assert refusal_events[0].details["boundary"] == "schedule.fire"
    assert refusal_events[0].details["json_path"] == "$.params.retries"
    assert refusal_events[0].details["reason_code"] == "missing_required"


def test_bad_value_fire_produces_verifiable_receipt(tmp_path: Path) -> None:
    dispatch = _SpyDispatch()
    sup, store, chain = _supervisor(tmp_path, dispatch)
    # A stored value that cannot coerce to the declared type -- a mistyped edit
    # that the fire-time contract rejects.
    schedule = store.add(
        cron="* * * * *",
        goal="typed report",
        params_schema=[{"name": "retries", "type": "int", "required": True}],
        params={"retries": "not-an-int"},
    )

    receipts = sup.tick(now=schedule.created_at + 120)
    refused = [r for r in receipts if r.refused]
    assert refused
    assert dispatch.calls == []
    assert refused[0].refusal_json_path == "$.params.retries"

    # The receipt persisted to disk verifies offline against the chain.
    receipt_hash = refused[0].refusal_receipt_hash
    assert receipt_hash
    digest = receipt_hash.split(":", 1)[-1]
    receipt_path = (tmp_path / ".sdd") / "input_contracts" / "refusals" / f"{digest}.json"
    receipt = read_refusal_receipt(receipt_path)
    assert receipt is not None
    assert verify_refusal_against_chain(chain, receipt).ok


def test_valid_params_fire_folds_params_hash(tmp_path: Path) -> None:
    dispatch = _SpyDispatch()
    sup, store, _chain = _supervisor(tmp_path, dispatch)
    schedule = store.add(
        cron="* * * * *",
        goal="typed report",
        params_schema=[{"name": "target", "type": "string", "required": True}],
        params={"target": "svc-a"},
    )
    receipts = sup.tick(now=schedule.created_at + 120)
    fired = [r for r in receipts if r.dispatched]
    assert fired, "a valid parameterized schedule should dispatch"
    assert dispatch.calls, "dispatch should have been called for a valid fire"
    assert fired[0].params_hash.startswith("sha256:")
    assert not fired[0].refused

    # Determinism: the same schedule + fire time yields the same params hash.
    from bernstein.core.tasks.param_contract import ParamContract

    contract = ParamContract.from_schema(schedule.params_schema)
    expected = contract.params_hash(contract.validate_and_coerce(schedule.params))
    assert fired[0].params_hash == expected


def test_paramless_schedule_unchanged(tmp_path: Path) -> None:
    # AC5: a schedule without params dispatches exactly as before and carries no
    # params hash.
    dispatch = _SpyDispatch()
    sup, store, _chain = _supervisor(tmp_path, dispatch)
    schedule = store.add(cron="* * * * *", goal="plain nightly")
    receipts = sup.tick(now=schedule.created_at + 120)
    fired = [r for r in receipts if r.dispatched]
    assert fired
    assert dispatch.calls
    assert fired[0].params_hash == ""
    assert not fired[0].refused
