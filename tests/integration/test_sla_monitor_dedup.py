"""An ongoing SLA breach is attested once, not once per tick (#4579 follow-up).

The supervisor holds one ``SLAMonitor`` and calls ``evaluate`` on every tick.
Before deduplication, a breach that stayed unresolved produced a fresh signed
receipt, one ``sla.violation`` chain event and one trigger event on *every*
tick - a supervisor ticking at seconds turned one fact into thousands of
attestations. These tests drive the monitor exactly as the supervisor does,
over a real ``.sdd`` tree, and pin the contract:

* the first tick that finds a breach attests it (receipt + chain + trigger);
* the next tick over unchanged state attests nothing new;
* a breach that resolves and recurs is a new fact and is attested again.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from bernstein.adapters.base import record_artifact_write
from bernstein.core.orchestration.sla_monitor import SLAMonitor, build_monitor_from_sdd
from bernstein.core.persistence.work_ledger import (
    run_ledger_dir,  # noqa: F401 - fixture parity with test_sla_audit_chain
)
from bernstein.core.planning.sla_store import SLAStore, build_contract
from bernstein.core.security.audit import load_or_create_audit_key
from bernstein.core.security.audit_chain import AuditChainStore

if TYPE_CHECKING:
    from pathlib import Path

    from bernstein.core.tasks.models import TriggerEvent

_NOW = 1_700_000_000
_ARTIFACT = ".sdd/runs/nightly/report.md"


def _seed_artifact(sdd: Path, *, rederived_at: int) -> None:
    record_artifact_write(
        artifact_path=_ARTIFACT,
        content=b"report body",
        actor="nightly",
        step_id="s1",
        model="",
        lineage_root=sdd / "lineage",
        run_id="nightly",
        hmac_key=load_or_create_audit_key(),
        timestamp=rederived_at,
    )


def _monitor(sdd: Path, fired: list[TriggerEvent]) -> tuple[SLAMonitor, AuditChainStore]:
    chain = AuditChainStore(sdd / "audit", key=load_or_create_audit_key())
    return build_monitor_from_sdd(sdd, chain=chain, trigger_sink=fired.append), chain


def _breached_setup(tmp_path: Path) -> Path:
    sdd = tmp_path / ".sdd"
    sdd.mkdir()
    SLAStore(sdd).add(
        build_contract(
            subject_type="schedule",
            subject_id="sched_nightly",
            artifact_freshness_s=90_000,
            artifact_path=_ARTIFACT,
        )
    )
    _seed_artifact(sdd, rederived_at=_NOW - 200_000)  # stale: breached at _NOW
    return sdd


def test_breached_contract_produces_signed_receipt_within_one_tick(tmp_path: Path) -> None:
    sdd = _breached_setup(tmp_path)
    fired: list[TriggerEvent] = []
    monitor, chain = _monitor(sdd, fired)

    receipts = monitor.evaluate(_NOW)

    assert len(receipts) == 1
    assert receipts[0].is_signed
    assert len(fired) == 1
    assert len(chain.query(event_type="sla.violation")) == 1


def test_ongoing_breach_does_not_reemit_every_tick(tmp_path: Path) -> None:
    sdd = _breached_setup(tmp_path)
    fired: list[TriggerEvent] = []
    monitor, chain = _monitor(sdd, fired)

    assert len(monitor.evaluate(_NOW)) == 1
    # Three more ticks over unchanged state: same contract, same breached axes.
    for tick in range(1, 4):
        assert monitor.evaluate(_NOW + tick * 60) == []

    assert len(fired) == 1, "an unresolved breach is one fact, not one per tick"
    assert len(chain.query(event_type="sla.violation")) == 1
    ok, errors = chain.verify()
    assert ok, errors


def test_resolved_then_recurring_breach_is_attested_again(tmp_path: Path) -> None:
    sdd = _breached_setup(tmp_path)
    fired: list[TriggerEvent] = []
    monitor, chain = _monitor(sdd, fired)

    assert len(monitor.evaluate(_NOW)) == 1

    # The artifact is re-derived: the breach resolves.
    _seed_artifact(sdd, rederived_at=_NOW + 100)
    assert monitor.evaluate(_NOW + 200) == []

    # Enough time passes that the fresh derivation is stale again: a new fact.
    later = _NOW + 100 + 200_000
    receipts = monitor.evaluate(later)
    assert len(receipts) == 1, "a breach that resolved and recurred must be attested again"
    assert len(fired) == 2
    assert len(chain.query(event_type="sla.violation")) == 2


def test_tick_without_contracts_attests_nothing(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    sdd.mkdir()
    fired: list[TriggerEvent] = []
    monitor, chain = _monitor(sdd, fired)

    assert monitor.evaluate(_NOW) == []
    assert fired == []
    assert chain.query(event_type="sla.violation") == []
