"""Adversarial contracts for universal authenticated run closure (#3469)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from bernstein.core.observability.otlp_ingest_receipt import (
    ATTR_COVERAGE,
    ATTR_COVERAGE_DETAIL,
    ATTR_INGEST_RECEIPT,
    ATTR_SOURCE_KIND,
    ATTR_SOURCE_PROFILE,
)
from bernstein.core.security.audit_chain import EVENT_RUN_CLOSURE, AuditChainStore, record_run_closure
from bernstein.core.security.run_closure import (
    CoverageStatementError,
    RunClosureError,
    RunClosureOutcome,
    RunClosureStatus,
    close_run,
    derive_coverage_statement,
    derive_run_closure,
    project_run_closure,
)

KEY = b"c" * 32
JOURNAL_HEAD = "1" * 64
LEDGER_HEAD = "2" * 64


def _chain(tmp_path: Path) -> AuditChainStore:
    return AuditChainStore(tmp_path / "audit", key=KEY)


def test_absence_is_open_never_complete(tmp_path: Path) -> None:
    projection = project_run_closure(_chain(tmp_path), "run-1")
    assert projection.status is RunClosureStatus.OPEN
    assert projection.outcome is None
    assert not projection.complete_range


@pytest.mark.parametrize("outcome", list(RunClosureOutcome))
def test_all_four_outcomes_are_distinct_authenticated_facts(tmp_path: Path, outcome: RunClosureOutcome) -> None:
    run_id = f"run-{outcome.value}"
    event = close_run(
        chain=_chain(tmp_path),
        run_id=run_id,
        outcome=outcome,
        actor="orchestrator",
        run_journal_head=JOURNAL_HEAD,
        run_journal_event_count=7,
    )
    assert event.event_type == EVENT_RUN_CLOSURE
    projection = project_run_closure(_chain(tmp_path), run_id)
    assert projection.status is RunClosureStatus.CLOSED
    assert projection.outcome is outcome
    assert projection.anchor_kind == "run_journal"
    assert projection.anchor_head == JOURNAL_HEAD
    assert projection.anchor_count == 7


def test_detached_closure_binds_work_ledger_without_pretending_it_is_a_journal(tmp_path: Path) -> None:
    close_run(
        chain=_chain(tmp_path),
        run_id="run-detached",
        outcome=RunClosureOutcome.COMPLETED,
        actor="run_service",
        work_ledger_head=LEDGER_HEAD,
        work_ledger_entry_count=4,
    )
    projection = project_run_closure(_chain(tmp_path), "run-detached")
    assert projection.status is RunClosureStatus.CLOSED
    assert projection.anchor_kind == "work_ledger"
    assert projection.anchor_head == LEDGER_HEAD


def test_retry_after_write_before_ack_returns_existing_event(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    kwargs = {
        "chain": chain,
        "run_id": "run-1",
        "outcome": RunClosureOutcome.COMPLETED,
        "actor": "orchestrator",
        "run_journal_head": JOURNAL_HEAD,
        "run_journal_event_count": 3,
    }
    first = close_run(**kwargs)  # type: ignore[arg-type]
    second = close_run(**kwargs)  # type: ignore[arg-type]
    assert second.hmac == first.hmac
    assert len(chain.query(event_type=EVENT_RUN_CLOSURE, resource_id="run-1")) == 1


def test_two_writers_cannot_race_identical_closure_into_duplicates(tmp_path: Path) -> None:
    audit_dir = tmp_path / "audit"

    def writer() -> str:
        event = close_run(
            chain=AuditChainStore(audit_dir, key=KEY),
            run_id="run-race",
            outcome="completed",
            actor="orchestrator",
            run_journal_head=JOURNAL_HEAD,
            run_journal_event_count=2,
        )
        return event.hmac

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(lambda _: writer(), range(2)))
    assert first == second
    assert len(_chain(tmp_path).query(event_type=EVENT_RUN_CLOSURE, resource_id="run-race")) == 1


@pytest.mark.parametrize(
    ("changed", "value"),
    [
        ("outcome", RunClosureOutcome.FAILED),
        ("run_journal_head", "3" * 64),
        ("run_journal_event_count", 4),
    ],
)
def test_conflicting_retry_fails_closed(tmp_path: Path, changed: str, value: object) -> None:
    chain = _chain(tmp_path)
    kwargs: dict[str, object] = {
        "chain": chain,
        "run_id": "run-1",
        "outcome": RunClosureOutcome.COMPLETED,
        "actor": "orchestrator",
        "run_journal_head": JOURNAL_HEAD,
        "run_journal_event_count": 3,
    }
    close_run(**kwargs)  # type: ignore[arg-type]
    kwargs[changed] = value
    with pytest.raises(RunClosureError, match="conflicting"):
        close_run(**kwargs)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    "anchors",
    [
        {},
        {"run_journal_head": "not-a-hash", "run_journal_event_count": 1},
        {"run_journal_head": JOURNAL_HEAD, "run_journal_event_count": 0},
        {
            "run_journal_head": JOURNAL_HEAD,
            "run_journal_event_count": 1,
            "work_ledger_head": LEDGER_HEAD,
            "work_ledger_entry_count": 1,
        },
    ],
)
def test_malformed_or_ambiguous_state_anchor_is_refused(tmp_path: Path, anchors: dict[str, object]) -> None:
    with pytest.raises(RunClosureError, match="exactly one"):
        close_run(
            chain=_chain(tmp_path),
            run_id="run-1",
            outcome="completed",
            actor="orchestrator",
            **anchors,  # type: ignore[arg-type]
        )


def test_later_same_run_event_invalidates_closure_but_interleaving_does_not(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    close_run(
        chain=chain,
        run_id="run-1",
        outcome="completed",
        actor="orchestrator",
        run_journal_head=JOURNAL_HEAD,
        run_journal_event_count=2,
    )
    chain.log(
        event_type="run.progress",
        actor="other",
        resource_type="run",
        resource_id="run-other",
        details={"run_id": "run-other"},
    )
    assert project_run_closure(chain, "run-1").status is RunClosureStatus.CLOSED

    chain.log(
        event_type="run.progress",
        actor="agent",
        resource_type="run",
        resource_id="run-1",
        details={"run_id": "run-1"},
    )
    projection = project_run_closure(chain, "run-1")
    assert projection.status is RunClosureStatus.INVALIDATED
    assert projection.outcome is RunClosureOutcome.COMPLETED
    assert projection.terminal_boundary is not None


def test_duplicate_markers_are_conflicting_even_when_fields_match(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    for _ in range(2):
        record_run_closure(
            chain=chain,
            run_id="run-1",
            outcome="completed",
            run_journal_head=JOURNAL_HEAD,
            run_journal_event_count=2,
            actor="unsafe-test-writer",
        )
    projection = derive_run_closure(chain.query(include_archived=True), "run-1")
    assert projection.status is RunClosureStatus.CONFLICTING


def test_payload_cannot_select_its_own_witnessed_provenance_mode(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    marker = chain.log(
        event_type=EVENT_RUN_CLOSURE,
        actor="test",
        resource_type="run",
        resource_id="run-1",
        details={
            "run_id": "run-1",
            "outcome": "completed",
            "run_journal_head": JOURNAL_HEAD,
            "run_journal_event_count": 2,
            "work_ledger_head": "",
            "work_ledger_entry_count": 0,
            "_original_hmac": "witnessed-boundary",
        },
    )
    events = chain.query(include_archived=True)
    assert derive_run_closure(events, "run-1").terminal_boundary == marker.hmac
    assert derive_run_closure(events, "run-1", witnessed=True).terminal_boundary == "witnessed-boundary"


def test_mutated_audit_chain_is_tampered_not_open(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    close_run(
        chain=chain,
        run_id="run-1",
        outcome="completed",
        actor="orchestrator",
        run_journal_head=JOURNAL_HEAD,
        run_journal_event_count=2,
    )
    path = next((tmp_path / "audit").glob("*.jsonl"))
    path.write_bytes(path.read_bytes().replace(b'"completed"', b'"abandoned"', 1))
    projection = project_run_closure(_chain(tmp_path), "run-1")
    assert projection.status is RunClosureStatus.TAMPERED
    assert projection.errors


# --------------------------------------------------------------------------- #
# Coverage statement (#4968): executed vs. reported                           #
# --------------------------------------------------------------------------- #


def _log_reported(
    chain: AuditChainStore,
    *,
    resource_id: str,
    profile_name: str = "generic",
    source_kind: str = "collector",
    coverage: str = "not_scheduled_by_bernstein",
    coverage_detail: str = "Bernstein did not schedule this activity.",
) -> None:
    chain.log(
        event_type="otlp_ingest_receipt.foreign_span",
        actor="otlp_ingest_receipt",
        resource_type="otlp_span",
        resource_id=resource_id,
        details={
            ATTR_INGEST_RECEIPT: True,
            ATTR_SOURCE_PROFILE: profile_name,
            ATTR_SOURCE_KIND: source_kind,
            ATTR_COVERAGE: coverage,
            ATTR_COVERAGE_DETAIL: coverage_detail,
        },
    )


def test_derive_coverage_statement_separates_executed_from_reported_by_source(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    chain.log(event_type="run.progress", actor="orchestrator", resource_type="run", resource_id="run-1", details={})
    chain.log(event_type="run.progress", actor="orchestrator", resource_type="run", resource_id="run-1", details={})
    _log_reported(chain, resource_id="span-1", profile_name="otel_collector", coverage_detail="collector gap")
    _log_reported(chain, resource_id="span-2", profile_name="otel_collector", coverage_detail="collector gap")
    _log_reported(chain, resource_id="span-3", profile_name="agent_direct", coverage_detail="agent gap")

    events = chain.query(include_archived=True)
    statement = derive_coverage_statement(events)

    assert statement.executed_count == 2
    assert statement.reported_count == 3
    assert statement.has_reported_activity
    by_profile = {s.profile_name: s for s in statement.sources}
    assert by_profile["otel_collector"].reported_count == 2
    assert by_profile["otel_collector"].coverage_detail == "collector gap"
    assert by_profile["agent_direct"].reported_count == 1
    assert by_profile["agent_direct"].coverage_detail == "agent gap"


def test_derive_coverage_statement_raises_rather_than_omit_a_known_gap(tmp_path: Path) -> None:
    chain = _chain(tmp_path)
    # A malformed ingest record: marked reported, but carries no coverage
    # detail. This must not be summarized as if it were fully covered.
    chain.log(
        event_type="otlp_ingest_receipt.foreign_span",
        actor="otlp_ingest_receipt",
        resource_type="otlp_span",
        resource_id="span-broken",
        details={
            ATTR_INGEST_RECEIPT: True,
            ATTR_SOURCE_PROFILE: "generic",
            ATTR_SOURCE_KIND: "collector",
            ATTR_COVERAGE: "not_scheduled_by_bernstein",
            ATTR_COVERAGE_DETAIL: "",
        },
    )
    events = chain.query(include_archived=True)
    with pytest.raises(CoverageStatementError):
        derive_coverage_statement(events)
