"""Unit tests for apply receipts on the target and the freshness check (#5087).

Each test is named for the property it protects:

1. ``test_apply_writes_a_receipt_readable_back_as_an_attribute`` - an apply
   leaves a receipt on the target that a later pass discovers as an ordinary
   surface attribute, without a side store.
2. ``test_missing_receipt_and_stale_receipt_produce_the_same_finding_name``
   (load-bearing) - "never converged" and "stopped converging" are one signal.
3. ``test_staleness_window_override_per_entity_kind_beats_the_global_default``
   - the configured window resolves global-default-with-per-kind-override.
4. ``test_drift_record_names_attribute_decided_observed_and_probe`` - a drift
   record carries all four fields and is anchored in the run spine.
5. ``test_target_sends_deltas_until_staleness_forces_full_resend`` - the target
   keeps its own last reported state and reports diffs off it.
"""

from __future__ import annotations

import json
from pathlib import Path

from bernstein.core.govern.reconcile_receipts import (
    RECEIPT_ATTRIBUTE,
    RECEIPT_NOT_CURRENT,
    DriftRecord,
    StalenessPolicy,
    TargetReceipt,
    build_state_report,
    check_receipt_current,
    discover_receipt_attribute,
    read_drift_records,
    read_target_receipt,
    record_drift,
    write_target_receipt,
)
from bernstein.core.lineage.spine import LineageSpine

_KEY = b"0" * 32
_POLICY_HASH = "sha256:" + "c" * 64
_KIND = "lane"
_TARGET = "lane-a"


def _lineage_root(workdir: Path) -> Path:
    return workdir / ".sdd" / "lineage"


def _apply(workdir: Path, *, applied_at: int, target_id: str = _TARGET) -> TargetReceipt:
    """Stand in for the apply loop (#5086): produce one applied outcome."""
    return write_target_receipt(
        target_root=workdir,
        lineage_root=_lineage_root(workdir),
        hmac_key=_KEY,
        receipt=TargetReceipt(
            target_id=target_id,
            entity_kind=_KIND,
            policy_set_hash=_POLICY_HASH,
            applied_at=applied_at,
        ),
    )


# ---------------------------------------------------------------------------
# 1 - the receipt lands on the target and reads back as an attribute
# ---------------------------------------------------------------------------


def test_apply_writes_a_receipt_readable_back_as_an_attribute(tmp_path: Path) -> None:
    anchored = _apply(tmp_path, applied_at=1000)

    assert anchored.journal_entry_hash.startswith("sha256:")
    assert read_target_receipt(tmp_path, _KIND, _TARGET) == anchored

    attribute = discover_receipt_attribute(tmp_path, _KIND, _TARGET)
    assert attribute is not None
    assert attribute.surface.endswith(RECEIPT_ATTRIBUTE)
    assert _TARGET in attribute.surface

    observed = json.loads(attribute.observed_value)
    assert observed["policy_set_hash"] == _POLICY_HASH
    assert observed["applied_at"] == 1000
    assert attribute.evidence_ref == anchored.journal_entry_hash

    spine = LineageSpine(_lineage_root(tmp_path), run_id="govern", hmac_key=_KEY)
    assert spine.verify().ok
    assert spine.head_hash() == anchored.journal_entry_hash


def test_apply_receipt_is_absent_before_any_apply(tmp_path: Path) -> None:
    assert read_target_receipt(tmp_path, _KIND, _TARGET) is None
    assert discover_receipt_attribute(tmp_path, _KIND, _TARGET) is None


# ---------------------------------------------------------------------------
# 2 - load-bearing: missing and stale are one finding
# ---------------------------------------------------------------------------


def test_missing_receipt_and_stale_receipt_produce_the_same_finding_name(tmp_path: Path) -> None:
    policy = StalenessPolicy(default_window_s=3600)
    _apply(tmp_path, applied_at=1000)

    missing = check_receipt_current(
        target_root=tmp_path,
        entity_kind=_KIND,
        target_id="never-applied",
        policy=policy,
        now=1000,
    )
    stale = check_receipt_current(
        target_root=tmp_path,
        entity_kind=_KIND,
        target_id=_TARGET,
        policy=policy,
        now=1000 + 3601,
    )
    fresh = check_receipt_current(
        target_root=tmp_path,
        entity_kind=_KIND,
        target_id=_TARGET,
        policy=policy,
        now=1000 + 3599,
    )

    assert missing is not None
    assert stale is not None
    assert fresh is None

    # One finding name covers both: from the operator's chair "never converged"
    # and "stopped converging" are the same fact - this target is not current.
    assert len({missing.name, stale.name}) == 1
    assert missing.name == RECEIPT_NOT_CURRENT
    assert {missing.reason, stale.reason} == {"missing", "stale"}
    assert missing.window_s == stale.window_s == 3600
    assert missing.observed_age_s is None
    assert stale.observed_age_s == 3601


def test_staleness_window_override_per_entity_kind_beats_the_global_default(tmp_path: Path) -> None:
    policy = StalenessPolicy(default_window_s=3600, overrides={_KIND: 60})
    _apply(tmp_path, applied_at=1000)

    assert policy.window_for(_KIND) == 60
    assert policy.window_for("adapter") == 3600

    # Inside the global default but outside this kind's override.
    finding = check_receipt_current(
        target_root=tmp_path,
        entity_kind=_KIND,
        target_id=_TARGET,
        policy=policy,
        now=1000 + 120,
    )
    assert finding is not None
    assert finding.name == RECEIPT_NOT_CURRENT
    assert finding.window_s == 60

    round_tripped = StalenessPolicy.from_dict({"default_window_s": 3600, "windows": {_KIND: 60}})
    assert round_tripped == policy


# ---------------------------------------------------------------------------
# 3 - the drift record names attribute / decided / observed / probe
# ---------------------------------------------------------------------------


def test_drift_record_names_attribute_decided_observed_and_probe(tmp_path: Path) -> None:
    anchored = record_drift(
        lineage_root=_lineage_root(tmp_path),
        hmac_key=_KEY,
        run_id="run-1",
        drift=DriftRecord(
            target_id=_TARGET,
            entity_kind=_KIND,
            attribute="max_parallel",
            decided_value="4",
            observed_value="8",
            probe="lane.config.read",
            timestamp=1000,
        ),
    )

    row = json.loads(anchored.to_canonical_bytes())
    assert row["attribute"] == "max_parallel"
    assert row["decided_value"] == "4"
    assert row["observed_value"] == "8"
    assert row["probe"] == "lane.config.read"

    assert anchored.journal_entry_hash.startswith("sha256:")
    spine = LineageSpine(_lineage_root(tmp_path), run_id="run-1", hmac_key=_KEY)
    assert spine.verify().ok
    assert spine.head_hash() == anchored.journal_entry_hash

    assert read_drift_records(_lineage_root(tmp_path), "run-1") == [anchored]


# ---------------------------------------------------------------------------
# 4 - deltas until staleness forces a full resend
# ---------------------------------------------------------------------------


def test_target_sends_deltas_until_staleness_forces_full_resend(tmp_path: Path) -> None:
    policy = StalenessPolicy(default_window_s=100)
    decided = {"max_parallel": "4", "sandbox": "on"}

    first = build_state_report(
        target_root=tmp_path,
        entity_kind=_KIND,
        target_id=_TARGET,
        observed={"max_parallel": "4", "sandbox": "on"},
        decided=decided,
        probe="lane.config.read",
        now=1000,
        policy=policy,
    )
    assert first.full is True
    assert dict(first.attributes) == {"max_parallel": "4", "sandbox": "on"}
    assert first.drift == ()

    second = build_state_report(
        target_root=tmp_path,
        entity_kind=_KIND,
        target_id=_TARGET,
        observed={"max_parallel": "8", "sandbox": "on"},
        decided=decided,
        probe="lane.config.read",
        now=1050,
        policy=policy,
    )
    assert second.full is False
    assert dict(second.attributes) == {"max_parallel": "8"}
    assert [d.attribute for d in second.drift] == ["max_parallel"]
    assert second.drift[0].decided_value == "4"
    assert second.drift[0].observed_value == "8"
    assert second.drift[0].probe == "lane.config.read"

    # Nothing moved and the window has not elapsed: an empty delta, not a resend.
    third = build_state_report(
        target_root=tmp_path,
        entity_kind=_KIND,
        target_id=_TARGET,
        observed={"max_parallel": "8", "sandbox": "on"},
        decided=decided,
        probe="lane.config.read",
        now=1100,
        policy=policy,
    )
    assert third.full is False
    assert dict(third.attributes) == {}

    # Past the window measured from the last report: a full resend.
    fourth = build_state_report(
        target_root=tmp_path,
        entity_kind=_KIND,
        target_id=_TARGET,
        observed={"max_parallel": "8", "sandbox": "on"},
        decided=decided,
        probe="lane.config.read",
        now=1201,
        policy=policy,
    )
    assert fourth.full is True
    assert dict(fourth.attributes) == {"max_parallel": "8", "sandbox": "on"}
    assert [d.attribute for d in fourth.drift] == ["max_parallel"]
