"""OCSF projection of a run's security evidence (#6037, slice 2).

The mapping doc landed as #6393 and its review settled every open question:
journal **and** audit chain, RFC 8785 SHA-256 digests over all parameters
including command strings, `trace export --format ocsf`, and the #1760 fields
behind a flag that is off by default. These tests hold the encoder to that.

What they pin, in the issue's own terms:

  - same input, same bytes out (the export is a pure function)
  - every event carries the base fields OCSF requires, with enum values that
    exist in 1.9.0
  - a decision and an outcome stay separate, and a decision with no recorded
    outcome is Unknown and never Success
  - absence is explicit rather than implied
  - an event can be traced back to the record it came from
  - request parameters leave as digests, never as text
  - an unmapped journal kind is a recorded omission, not a crash or a drop
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

import pytest

from bernstein.core.observability.ocsf_projection import (
    ABSENT_UNESTABLISHED,
    ACTION_ALLOWED,
    ACTION_DENIED,
    ACTION_UNKNOWN,
    AUDIT_MAPPING,
    CANONICALIZATION,
    JOURNAL_EXCLUDED,
    JOURNAL_MAPPING,
    OCSF_SCHEMA_VERSION,
    STATUS_SUCCESS,
    STATUS_UNKNOWN,
    build_ocsf_events,
    export_ocsf,
    render_ocsf_jsonl,
)

if TYPE_CHECKING:
    from pathlib import Path

_RUN = "ocsf-fixture-run"

# OCSF 1.9.0 enums, as read from ocsf/ocsf-schema at that tag. Duplicated here
# deliberately: if the encoder's own constants drift, comparing it against
# itself would prove nothing.
_VALID_CATEGORY_UIDS = {2, 3, 4, 5, 6, 7, 1, 8}
_VALID_STATUS_IDS = {0, 1, 2, 99}
_VALID_ACTION_IDS = {0, 1, 2, 3, 4, 99}
_VALID_SEVERITY_IDS = {0, 1, 2, 3, 4, 5, 6, 99}
_REQUIRED_FIELDS = (
    "activity_id",
    "category_uid",
    "class_uid",
    "metadata",
    "severity_id",
    "status_id",
    "time",
    "type_uid",
)


# ---------------------------------------------------------------------------
# Fixtures: rows in the shape the two stores actually write
# ---------------------------------------------------------------------------
def _journal_row(index: int, event: str, **payload: Any) -> dict[str, Any]:
    """A journal row as ``EventJournal.record`` writes it."""
    return {
        "ts": 1_700_000_000.5 + index,
        "elapsed_s": 0.001 * index,
        "index": index,
        "event": event,
        "prev_hash": "" if index == 0 else f"hash{index - 1:04d}",
        "payload_hash": f"payload{index:04d}",
        "event_hash": f"hash{index:04d}",
        "run_id": _RUN,
        **payload,
    }


def _audit_entry(event_type: str, details: dict[str, Any], hmac: str = "a" * 64) -> dict[str, Any]:
    """An audit entry as ``AuditLog.log`` writes it."""
    return {
        "actor": "backend",
        "details": details,
        "event_type": event_type,
        "hmac": hmac,
        "prev_hmac": "0" * 64,
        "resource_id": "Bash",
        "resource_type": "tool_call",
        "scheme": 2,
        "timestamp": "2026-10-04T14:27:21.579736Z",
    }


_JOURNAL = [
    _journal_row(0, "run_started"),
    _journal_row(1, "tick_start", tick=1),
    _journal_row(2, "agent_spawned", agent_id="a1"),
    _journal_row(3, "task_claimed", task_id="T-1", role="backend"),
    _journal_row(4, "task_completed", task_id="T-1"),
    _journal_row(5, "run_completed"),
]
_AUDIT = [
    _audit_entry("auto_approve_decision", {"decision": "DENY", "command": "rm -rf /tmp/x"}, "b" * 64),
    _audit_entry("auto_approve_decision", {"decision": "APPROVE", "tool": "Read"}, "c" * 64),
    _audit_entry("approval_pending", {"tool": "Write"}, "d" * 64),
    _audit_entry("admission_refusal", {"reason": "capability not admitted"}, "e" * 64),
]


def _build(**kwargs: Any) -> Any:
    kwargs.setdefault("journal_rows", _JOURNAL)
    kwargs.setdefault("audit_entries", _AUDIT)
    return build_ocsf_events(run_id=_RUN, **kwargs)


def _by_message(result: Any, message: str) -> list[dict[str, Any]]:
    return [e for e in result.events if e["message"] == message]


# ---------------------------------------------------------------------------
# Determinism: "same journal, same bytes out"
# ---------------------------------------------------------------------------
def test_the_same_input_produces_byte_identical_output() -> None:
    assert _build().jsonl == _build().jsonl


def test_no_export_time_wall_clock_enters_the_bytes() -> None:
    """Only times *in the records* appear. A generation timestamp would make
    the export non-reproducible and is what ``metadata.logged_time`` would be."""
    for event in _build().events:
        assert "logged_time" not in event["metadata"]
    # The journal row's own ts is what `time` carries.
    assert _by_message(_build(), "journal:run_started")[0]["time"] == 1_700_000_000_500


def test_rendering_is_one_canonical_object_per_line() -> None:
    result = _build()
    lines = result.jsonl.decode("utf-8").splitlines()
    assert len(lines) == result.event_count
    for line, event in zip(lines, result.events, strict=True):
        assert json.loads(line) == event
        assert line == json.dumps(event, sort_keys=True, separators=(",", ":"))


def test_an_empty_projection_renders_empty_rather_than_a_blank_line() -> None:
    assert render_ocsf_jsonl([]) == b""


# ---------------------------------------------------------------------------
# Schema conformance: required fields, and enum values that exist in 1.9.0
# ---------------------------------------------------------------------------
def test_every_event_carries_the_required_base_fields() -> None:
    for event in _build().events:
        for field in _REQUIRED_FIELDS:
            assert field in event, f"{event['message']} is missing {field}"


def test_every_enum_value_exists_in_the_pinned_schema() -> None:
    for event in _build().events:
        assert event["category_uid"] in _VALID_CATEGORY_UIDS
        assert event["status_id"] in _VALID_STATUS_IDS
        assert event["severity_id"] in _VALID_SEVERITY_IDS
        if "action_id" in event:
            assert event["action_id"] in _VALID_ACTION_IDS


def test_class_uid_is_its_category_times_a_thousand_plus_its_own_uid() -> None:
    """The rule that makes the mapping table checkable rather than asserted."""
    for event in _build().events:
        assert event["class_uid"] // 1000 == event["category_uid"]


def test_type_uid_is_class_uid_times_a_hundred_plus_activity_id() -> None:
    for event in _build().events:
        assert event["type_uid"] == event["class_uid"] * 100 + event["activity_id"]


def test_the_pinned_version_is_announced_on_every_event() -> None:
    for event in _build().events:
        assert event["metadata"]["version"] == OCSF_SCHEMA_VERSION


def test_the_security_control_profile_is_declared_wherever_a_decision_is_carried() -> None:
    """``action_id`` is not on ``base_event``; it arrives with that profile, so
    a validator rejects the event unless the profile is declared."""
    for event in _build().events:
        if "action_id" in event:
            assert "security_control" in event["metadata"]["profiles"]
        else:
            assert "profiles" not in event["metadata"]


# ---------------------------------------------------------------------------
# Decision and outcome are two facts
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("verdict", "expected"),
    [
        ("DENY", ACTION_DENIED),
        ("REJECT", ACTION_DENIED),
        ("APPROVE", ACTION_ALLOWED),
        ("ALLOW", ACTION_ALLOWED),
        ("ASK", ACTION_UNKNOWN),
        ("nonsense", ACTION_UNKNOWN),
    ],
)
def test_a_recorded_verdict_becomes_the_ocsf_decision(verdict: str, expected: int) -> None:
    result = build_ocsf_events(
        run_id=_RUN,
        journal_rows=[],
        audit_entries=[_audit_entry("auto_approve_decision", {"decision": verdict})],
    )
    assert result.events[0]["action_id"] == expected


def test_a_decision_with_no_recorded_outcome_is_unknown_and_never_success() -> None:
    """THE rule from the issue. No audit entry records the effect, so the
    outcome is never established - and must not be inferred from a permit."""
    for event in _build().events:
        if event["unmapped"]["source"]["store"] != "audit":
            continue
        assert event["status_id"] == STATUS_UNKNOWN, (
            f"{event['message']} claimed an outcome the record does not contain"
        )
        assert event["status_id"] != STATUS_SUCCESS


def test_an_allowed_call_does_not_become_a_successful_one() -> None:
    allowed = _by_message(_build(), "audit:auto_approve_decision")
    permitted = [e for e in allowed if e["action_id"] == ACTION_ALLOWED]
    assert permitted, "fixture no longer contains a permitted call"
    assert all(e["status_id"] == STATUS_UNKNOWN for e in permitted)


def test_a_journal_step_that_states_its_outcome_keeps_it() -> None:
    """Control: the Unknown above must not be the encoder refusing to ever
    report an outcome."""
    assert _by_message(_build(), "journal:task_completed")[0]["status_id"] == STATUS_SUCCESS
    assert _by_message(_build(), "journal:run_completed")[0]["status_id"] == STATUS_SUCCESS


# ---------------------------------------------------------------------------
# Absence is explicit
# ---------------------------------------------------------------------------
def test_an_unknown_outcome_says_which_kind_of_absence_it_is() -> None:
    for event in _build().events:
        if event["status_id"] == STATUS_UNKNOWN:
            assert event["status_detail"] == ABSENT_UNESTABLISHED
        else:
            assert "status_detail" not in event


# ---------------------------------------------------------------------------
# Correlation
# ---------------------------------------------------------------------------
def test_every_event_names_the_run_and_the_record_it_came_from() -> None:
    for event in _build().events:
        assert event["metadata"]["correlation_uid"] == _RUN
        source = event["unmapped"]["source"]
        assert source["store"] in ("journal", "audit")
        assert source["record_hash"], f"{event['message']} cannot be traced to a record"


def test_the_record_hash_is_the_journal_event_hash_or_the_audit_hmac() -> None:
    journal_hashes = {
        e["unmapped"]["source"]["record_hash"] for e in _build().events if e["unmapped"]["source"]["store"] == "journal"
    }
    audit_hashes = {
        e["unmapped"]["source"]["record_hash"] for e in _build().events if e["unmapped"]["source"]["store"] == "audit"
    }
    assert journal_hashes <= {row["event_hash"] for row in _JOURNAL}
    assert audit_hashes <= {entry["hmac"] for entry in _AUDIT}


# ---------------------------------------------------------------------------
# Parameters leave as digests
# ---------------------------------------------------------------------------
def test_a_command_string_is_digested_rather_than_forwarded() -> None:
    """The one the review called out: the command is already in the clear in
    .sdd/audit, but forwarding it to a SIEM is a wider disclosure."""
    jsonl = _build().jsonl
    assert b"rm -rf /tmp/x" not in jsonl
    assert b"rm -rf" not in jsonl


def test_raw_data_is_never_populated_and_the_digest_names_its_canonicalisation() -> None:
    for event in _build().events:
        assert "raw_data" not in event
        assert event["raw_data_hash"]["algorithm_id"] == 3  # SHA-256
        assert len(event["raw_data_hash"]["value"]) == 64
        assert event["unmapped"]["canonicalization"] == CANONICALIZATION


def test_the_digest_changes_when_a_parameter_changes() -> None:
    """Otherwise the digest would be decorative."""
    one = build_ocsf_events(
        run_id=_RUN, journal_rows=[], audit_entries=[_audit_entry("admission_refusal", {"reason": "a"})]
    )
    two = build_ocsf_events(
        run_id=_RUN, journal_rows=[], audit_entries=[_audit_entry("admission_refusal", {"reason": "b"})]
    )
    assert one.events[0]["raw_data_hash"]["value"] != two.events[0]["raw_data_hash"]["value"]


def test_the_journal_digest_ignores_the_fields_the_receipt_also_drops() -> None:
    """``ts`` and ``elapsed_s`` are wall clock, so including them would make
    the digest unreproducible for the same logical row."""
    base = _journal_row(0, "run_started")
    later = dict(base, ts=base["ts"] + 99, elapsed_s=base["elapsed_s"] + 99)
    first = build_ocsf_events(run_id=_RUN, journal_rows=[base], audit_entries=[])
    second = build_ocsf_events(run_id=_RUN, journal_rows=[later], audit_entries=[])
    assert first.events[0]["raw_data_hash"]["value"] == second.events[0]["raw_data_hash"]["value"]


# ---------------------------------------------------------------------------
# Gaps are recorded, not dropped
# ---------------------------------------------------------------------------
def test_an_unmapped_journal_kind_is_exported_and_reported() -> None:
    """The journal has no central registry of event kinds, so the table is a
    snapshot. An unknown kind must neither crash nor vanish."""
    rows = [*_JOURNAL, _journal_row(6, "some_future_kind", detail="x")]
    result = build_ocsf_events(run_id=_RUN, journal_rows=rows, audit_entries=[])

    assert "some_future_kind" in result.unmapped_kinds
    exported = _by_message(result, "journal:some_future_kind")
    assert len(exported) == 1
    assert exported[0]["activity_id"] == 0
    assert exported[0]["unmapped"]["unmapped_kind"] == "some_future_kind"


def test_excluded_kinds_are_counted_rather_than_silently_skipped() -> None:
    result = _build()
    assert result.excluded_count == 1  # one tick_start in the fixture
    assert _by_message(result, "journal:tick_start") == []


def test_an_unmapped_audit_kind_is_not_exported_as_a_security_decision() -> None:
    """A journal kind degrades to activity 0; an *audit* kind must not, because
    inventing a decision for an unrecognised security record is worse than
    omitting it."""
    result = build_ocsf_events(
        run_id=_RUN, journal_rows=[], audit_entries=[_audit_entry("some_future_audit_kind", {"x": 1})]
    )
    assert result.events == []


def test_every_excluded_kind_is_absent_from_the_mapping_table() -> None:
    """The two sets must not overlap, or a kind's fate would depend on order."""
    assert not (JOURNAL_EXCLUDED & set(JOURNAL_MAPPING))


# ---------------------------------------------------------------------------
# The proposed extension stays off
# ---------------------------------------------------------------------------
def test_proposed_fields_are_absent_by_default() -> None:
    """Off by default is what makes stock-1.9.0 validation meaningful."""
    for event in _build().events:
        assert "proposed" not in event["unmapped"]


def test_proposed_fields_are_marked_as_proposed_when_asked_for() -> None:
    for event in _build(include_proposed=True).events:
        proposed = event["unmapped"]["proposed"]
        assert proposed["schema_proposal"] == "ocsf/ocsf-schema#1760"
        assert proposed["status"] == "open"


# ---------------------------------------------------------------------------
# Reading the two stores off disk
# ---------------------------------------------------------------------------
def test_export_reads_both_stores_from_disk(tmp_path: Path) -> None:
    from bernstein.core.replay.journal import EventJournal
    from bernstein.core.security.audit import AuditLog

    sdd = tmp_path / ".sdd"
    journal = EventJournal(run_id=_RUN, sdd_dir=sdd)
    journal.record("run_started", run_id=_RUN)
    journal.record("run_completed", run_id=_RUN)
    AuditLog(audit_dir=sdd / "audit").log(
        event_type="auto_approve_decision",
        actor="backend",
        resource_type="tool_call",
        resource_id="Bash",
        details={"decision": "DENY", "command": "rm -rf /"},
    )

    result = export_ocsf(sdd, _RUN)

    stores = {e["unmapped"]["source"]["store"] for e in result.events}
    assert stores == {"journal", "audit"}, "a store was not read"
    denied = _by_message(result, "audit:auto_approve_decision")
    assert denied[0]["action_id"] == ACTION_DENIED
    assert b"rm -rf /" not in result.jsonl


def test_no_audit_omits_the_security_decisions(tmp_path: Path) -> None:
    """``--no-audit`` is the journal-only shape, and it loses the decisions -
    which is exactly why the default reads both."""
    from bernstein.core.replay.journal import EventJournal
    from bernstein.core.security.audit import AuditLog

    sdd = tmp_path / ".sdd"
    EventJournal(run_id=_RUN, sdd_dir=sdd).record("run_started", run_id=_RUN)
    AuditLog(audit_dir=sdd / "audit").log(
        event_type="admission_refusal", actor="o", resource_type="agent", resource_id="a1", details={"reason": "x"}
    )

    result = export_ocsf(sdd, _RUN, include_audit=False)

    assert {e["unmapped"]["source"]["store"] for e in result.events} == {"journal"}


def test_a_missing_run_projects_nothing_rather_than_raising(tmp_path: Path) -> None:
    result = export_ocsf(tmp_path / ".sdd", "no-such-run")
    assert result.events == []
    assert result.jsonl == b""


# ---------------------------------------------------------------------------
# The mapping tables themselves
# ---------------------------------------------------------------------------
def test_every_audit_security_kind_in_the_doc_is_mapped() -> None:
    """The eight audit kinds the mapping doc commits to."""
    assert set(AUDIT_MAPPING) == {
        "auto_approve_decision",
        "human_approval_decision",
        "approval_pending",
        "approval_resolved",
        "admission_refusal",
        "capability_matrix_refusal",
        "always_allow_promotion",
        "lineage_tamper_detected",
    }


def test_every_mapped_class_is_one_the_encoder_can_name() -> None:
    """A row pointing at a class with no name or category would KeyError at
    export time rather than at import time."""
    for table in (JOURNAL_MAPPING, AUDIT_MAPPING):
        for kind, mapped in table.items():
            event = build_ocsf_events(
                run_id=_RUN,
                journal_rows=[_journal_row(0, kind)] if table is JOURNAL_MAPPING else [],
                audit_entries=[] if table is JOURNAL_MAPPING else [_audit_entry(kind, {})],
            )
            assert event.events, f"{kind} produced no event"
            assert event.events[0]["class_uid"] == mapped.class_uid
