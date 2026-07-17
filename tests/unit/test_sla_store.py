"""Unit tests for the SLA contract store (#2549)."""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.planning.sla_store import (
    SLAContractError,
    SLAStore,
    build_contract,
)


def test_add_is_idempotent_by_derived_id(tmp_path: Path) -> None:
    store = SLAStore(tmp_path / ".sdd")
    contract = build_contract(subject_type="schedule", subject_id="sched_x", fire_frequency_s=3600)
    first = store.add(contract, now=1.0)
    second = store.add(contract, now=999.0)
    assert first.id == second.id
    assert first.created_at == second.created_at == 1.0
    assert len(store.list()) == 1


def test_persist_and_reload_preserves_hash(tmp_path: Path) -> None:
    store = SLAStore(tmp_path / ".sdd")
    contract = build_contract(
        subject_type="envelope",
        subject_id="subscription",
        spend_rate_usd_per_hour=2.5,
        remediation_cost_usd=1.0,
    )
    stored = store.add(contract)
    reloaded = store.get(stored.id)
    assert reloaded is not None
    assert reloaded.contract_hash == stored.contract_hash
    assert reloaded.spend_rate_usd_per_hour == 2.5


def test_for_subject_filters(tmp_path: Path) -> None:
    store = SLAStore(tmp_path / ".sdd")
    a = store.add(build_contract(subject_type="schedule", subject_id="sched_a", fire_frequency_s=60))
    store.add(build_contract(subject_type="schedule", subject_id="sched_b", fire_frequency_s=60))
    hits = store.for_subject("schedule", "sched_a")
    assert [c.id for c in hits] == [a.id]


def test_remove(tmp_path: Path) -> None:
    store = SLAStore(tmp_path / ".sdd")
    contract = store.add(build_contract(subject_type="schedule", subject_id="s", fire_frequency_s=60))
    assert store.remove(contract.id) is True
    assert store.get(contract.id) is None
    assert store.remove(contract.id) is False


def test_contract_with_no_axis_is_rejected() -> None:
    with pytest.raises(SLAContractError):
        build_contract(subject_type="schedule", subject_id="s")


def test_freshness_axis_requires_artifact_path() -> None:
    with pytest.raises(SLAContractError):
        build_contract(subject_type="schedule", subject_id="s", artifact_freshness_s=3600)


def test_unknown_subject_type_is_rejected() -> None:
    with pytest.raises(SLAContractError):
        build_contract(subject_type="galaxy", subject_id="s", fire_frequency_s=60)
