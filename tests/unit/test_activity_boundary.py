"""Tests for the typed activity boundary (issue #2311).

Bernstein's deterministic scheduler is validated for coding agents. The
``ActivityResult`` boundary is the typed contract a non-coding modality --
research, browser, data, ops -- would participate through as a replayable step:
every activity returns an artifact plus the hashes needed to replay it, so the
scheduler stays deterministic and the agent stays an opaque stochastic activity
behind a hash-in / hash-out contract. ``bernstein activity browser run`` is the
one non-coding entry point an operator can drive; research, data and ops stay
Python-API only, and dispatching a non-coding modality from a seed, plan, or
backlog file is not wired yet. See the scope note in
:mod:`bernstein.core.orchestration.activity`, "Reachability today" in
``docs/operations/activity-boundary.md``, and issues #2996 and #3110.

These tests pin the modality-agnostic substrate:

* AC2 -- every activity result is schema-validated at the boundary; a malformed
  result is rejected with a typed refusal.
* AC3 -- ``evidence_set_hash`` for each stage is recorded in the run journal.
* AC4 -- the scheduler refuses a stage that introduces no new evidence hash and
  logs the refusal.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.orchestration.activity import (
    ACTIVITY_RESULT_EVENT,
    ActivityKind,
    ActivityRejected,
    ActivityResult,
    Observation,
    RedundancyLedger,
    RedundantEvidenceRefused,
    TerminalState,
    dispatch_activity,
    evidence_set_hash,
    validate_activity_result,
)
from bernstein.core.replay.journal import EventJournal, load_events


def _journal(tmp_path: Path, run_id: str = "run-1") -> EventJournal:
    return EventJournal(run_id=run_id, sdd_dir=tmp_path / ".sdd")


def _obs(ref: str, content: bytes) -> Observation:
    return Observation.of(kind="page", ref=ref, content=content)


def _result(
    *,
    observations: tuple[Observation, ...] = (),
    artifact: object = "report",
    terminal_state: TerminalState = TerminalState.COMPLETED,
    reason_code: str = "ok",
) -> ActivityResult:
    return ActivityResult.build(
        kind=ActivityKind.RESEARCH,
        artifact=artifact,
        observations=observations,
        terminal_state=terminal_state,
        reason_code=reason_code,
    )


# ---------------------------------------------------------------------------
# evidence_set_hash determinism
# ---------------------------------------------------------------------------


def test_evidence_set_hash_is_order_independent() -> None:
    a = _obs("https://a", b"alpha")
    b = _obs("https://b", b"beta")
    # The evidence *set* is a set: two stages that gathered the same bytes in a
    # different fetch order carry the same exogenous signal and must hash equal.
    assert evidence_set_hash((a, b)) == evidence_set_hash((b, a))


def test_evidence_set_hash_changes_with_content() -> None:
    a = _obs("https://a", b"alpha")
    a2 = _obs("https://a", b"alpha-v2")
    assert evidence_set_hash((a,)) != evidence_set_hash((a2,))


def test_empty_evidence_set_hash_is_stable() -> None:
    assert evidence_set_hash(()) == evidence_set_hash(())


def test_observation_content_hash_is_content_addressed() -> None:
    o = _obs("https://a", b"alpha")
    assert o.content_hash == Observation.of(kind="page", ref="ignored", content=b"alpha").content_hash
    assert o.content_hash.startswith("sha256:")


# ---------------------------------------------------------------------------
# AC2 -- schema validation at the boundary; malformed -> typed refusal
# ---------------------------------------------------------------------------


def test_valid_activity_result_passes_boundary() -> None:
    result = _result(observations=(_obs("https://a", b"alpha"),))
    validated = validate_activity_result(result)
    assert validated.terminal_state is TerminalState.COMPLETED
    assert validated.artifact_hash.startswith("sha256:")
    assert validated.evidence_set_hash.startswith("sha256:")


def test_malformed_result_missing_artifact_hash_is_rejected() -> None:
    # A hand-forged result whose artifact_hash does not match its artifact is a
    # tampered / malformed boundary crossing.
    good = _result(artifact="report")
    forged = ActivityResult(
        kind=good.kind,
        artifact=good.artifact,
        artifact_hash="sha256:deadbeef",
        evidence_set_hash=good.evidence_set_hash,
        terminal_state=good.terminal_state,
        reason_code=good.reason_code,
        observations=good.observations,
    )
    with pytest.raises(ActivityRejected) as exc:
        validate_activity_result(forged)
    assert "artifact_hash" in str(exc.value)


def test_malformed_result_mismatched_evidence_hash_is_rejected() -> None:
    good = _result(observations=(_obs("https://a", b"alpha"),))
    forged = ActivityResult(
        kind=good.kind,
        artifact=good.artifact,
        artifact_hash=good.artifact_hash,
        evidence_set_hash="sha256:0000",
        terminal_state=good.terminal_state,
        reason_code=good.reason_code,
        observations=good.observations,
    )
    with pytest.raises(ActivityRejected):
        validate_activity_result(forged)


def test_malformed_result_bad_terminal_state_is_rejected() -> None:
    good = _result()
    forged = ActivityResult(
        kind=good.kind,
        artifact=good.artifact,
        artifact_hash=good.artifact_hash,
        evidence_set_hash=good.evidence_set_hash,
        terminal_state="finished-ish",  # type: ignore[arg-type]
        reason_code=good.reason_code,
        observations=good.observations,
    )
    with pytest.raises(ActivityRejected):
        validate_activity_result(forged)


def test_malformed_result_empty_reason_code_is_rejected() -> None:
    good = _result(reason_code="")
    with pytest.raises(ActivityRejected):
        validate_activity_result(good)


def test_dispatch_rejects_malformed_result_before_journaling(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    forged = ActivityResult(
        kind=ActivityKind.RESEARCH,
        artifact="x",
        artifact_hash="sha256:wrong",
        evidence_set_hash="sha256:wrong",
        terminal_state=TerminalState.COMPLETED,
        reason_code="ok",
        observations=(),
    )
    with pytest.raises(ActivityRejected):
        dispatch_activity(forged, stage_id="s0", journal=journal)
    # Rejected before any event was anchored.
    assert journal.event_count() == 0


# ---------------------------------------------------------------------------
# AC3 -- evidence_set_hash for each stage is recorded in the journal
# ---------------------------------------------------------------------------


def test_dispatch_anchors_evidence_set_hash_in_journal(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    result = _result(observations=(_obs("https://a", b"alpha"), _obs("https://b", b"beta")))
    outcome = dispatch_activity(result, stage_id="stage-0", journal=journal)

    assert outcome.journal_index == 0
    assert journal.event_count() == 1
    rows = load_events(journal.path)
    assert rows[0]["event"] == ACTIVITY_RESULT_EVENT
    assert rows[0]["evidence_set_hash"] == result.evidence_set_hash
    assert rows[0]["artifact_hash"] == result.artifact_hash
    assert rows[0]["kind"] == ActivityKind.RESEARCH.value
    assert rows[0]["stage_id"] == "stage-0"


def test_dispatch_returns_journal_event_hash(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    result = _result(observations=(_obs("https://a", b"alpha"),))
    outcome = dispatch_activity(result, stage_id="s0", journal=journal)
    assert outcome.journal_event_hash == journal.head()
    assert outcome.journal_event_hash


def test_evidence_hash_is_replay_invariant_across_journals(tmp_path: Path) -> None:
    # Two independent runs that gathered the same evidence anchor the same
    # evidence_set_hash, so a replay can reattach the same bytes (AC1/AC3).
    obs = (_obs("https://a", b"alpha"),)
    j1 = _journal(tmp_path / "r1", run_id="r1")
    j2 = _journal(tmp_path / "r2", run_id="r2")
    dispatch_activity(_result(observations=obs), stage_id="s0", journal=j1)
    dispatch_activity(_result(observations=obs), stage_id="s0", journal=j2)
    rows1 = load_events(j1.path)
    rows2 = load_events(j2.path)
    assert rows1[0]["evidence_set_hash"] == rows2[0]["evidence_set_hash"]


# ---------------------------------------------------------------------------
# AC4 -- refuse a stage that introduces no new evidence hash; log the refusal
# ---------------------------------------------------------------------------


def test_redundancy_ledger_admits_first_evidence_set() -> None:
    ledger = RedundancyLedger()
    result = _result(observations=(_obs("https://a", b"alpha"),))
    # First occurrence of this evidence set is admitted.
    ledger.admit(result.evidence_set_hash, stage_id="s0")
    assert result.evidence_set_hash in ledger.seen_hashes()


def test_redundancy_ledger_refuses_duplicate_evidence_set() -> None:
    ledger = RedundancyLedger()
    ev = evidence_set_hash((_obs("https://a", b"alpha"),))
    ledger.admit(ev, stage_id="s0")
    with pytest.raises(RedundantEvidenceRefused) as exc:
        ledger.admit(ev, stage_id="s1")
    # The refusal names the prior stage that already contributed this signal.
    assert "s0" in str(exc.value)


def test_dispatch_refuses_stage_with_no_new_evidence(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    ledger = RedundancyLedger()
    obs = (_obs("https://a", b"alpha"),)
    dispatch_activity(_result(observations=obs), stage_id="s0", journal=journal, redundancy_ledger=ledger)
    # A second stage with the identical evidence set introduces no new exogenous
    # signal and is refused.
    with pytest.raises(RedundantEvidenceRefused):
        dispatch_activity(_result(observations=obs), stage_id="s1", journal=journal, redundancy_ledger=ledger)


def test_dispatch_refusal_is_logged_and_not_anchored(tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
    import logging

    journal = _journal(tmp_path)
    ledger = RedundancyLedger()
    obs = (_obs("https://a", b"alpha"),)
    dispatch_activity(_result(observations=obs), stage_id="s0", journal=journal, redundancy_ledger=ledger)
    with caplog.at_level(logging.WARNING, logger="bernstein.core.orchestration.activity"):
        with pytest.raises(RedundantEvidenceRefused):
            dispatch_activity(_result(observations=obs), stage_id="s1", journal=journal, redundancy_ledger=ledger)
    # The refusal is logged (AC4) and the redundant stage was not anchored.
    assert any("refused" in rec.getMessage().lower() for rec in caplog.records)
    assert any("no new evidence" in rec.getMessage().lower() for rec in caplog.records)
    assert journal.event_count() == 1


def test_dispatch_admits_stage_with_new_evidence(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    ledger = RedundancyLedger()
    dispatch_activity(
        _result(observations=(_obs("https://a", b"alpha"),)),
        stage_id="s0",
        journal=journal,
        redundancy_ledger=ledger,
    )
    # A different evidence set introduces new signal and is admitted.
    dispatch_activity(
        _result(observations=(_obs("https://b", b"beta"),)),
        stage_id="s1",
        journal=journal,
        redundancy_ledger=ledger,
    )
    assert journal.event_count() == 2


def test_refused_stage_does_not_pollute_ledger(tmp_path: Path) -> None:
    # A refused (duplicate) stage must not add a new entry; the ledger stays at
    # exactly the admitted set.
    journal = _journal(tmp_path)
    ledger = RedundancyLedger()
    obs = (_obs("https://a", b"alpha"),)
    dispatch_activity(_result(observations=obs), stage_id="s0", journal=journal, redundancy_ledger=ledger)
    with pytest.raises(RedundantEvidenceRefused):
        dispatch_activity(_result(observations=obs), stage_id="s1", journal=journal, redundancy_ledger=ledger)
    assert len(ledger.seen_hashes()) == 1


# ---------------------------------------------------------------------------
# terminal-state / reason-code typing
# ---------------------------------------------------------------------------


def test_refused_terminal_state_carries_reason_code(tmp_path: Path) -> None:
    journal = _journal(tmp_path)
    result = _result(
        observations=(_obs("https://a", b"alpha"),),
        terminal_state=TerminalState.REFUSED,
        reason_code="policy_denied",
    )
    outcome = dispatch_activity(result, stage_id="s0", journal=journal)
    rows = load_events(journal.path)
    assert rows[0]["terminal_state"] == TerminalState.REFUSED.value
    assert rows[0]["reason_code"] == "policy_denied"
    assert outcome.result.reason_code == "policy_denied"


def test_all_terminal_states_validate(tmp_path: Path) -> None:
    for state in TerminalState:
        result = _result(terminal_state=state, reason_code="ok")
        assert validate_activity_result(result).terminal_state is state
