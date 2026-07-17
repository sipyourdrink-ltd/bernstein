"""Research worker: planning, budgeted fetching, dispatch, offline verify (#2524).

The worker runs the deterministic half of a research task -- plan, fetch through
:meth:`ResearchActivity.fetch` (content-addressing every page), apply the cost
cap, assemble a citation-lineage report -- and hands the result to the same
dispatch path a coding spawn uses. These tests prove the end-to-end guarantee:

* every produced report carries at least one citation per claim; an uncited or
  unbound claim is refused before dispatch (AC1);
* the dispatched report is content-addressed, anchored as ``artifact_hash``, and
  mirrored into the audit chain (AC3);
* with only the content store, ``verify_run_activities`` resolves every citation
  and passes; altering a stored source page fails naming the claim (AC2);
* research runs dispatch next to coding tasks under one journal with cost caps
  applied (AC4); and two verify runs produce identical verdicts (AC5).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.orchestration.activity import (
    ActivityKind,
    ActivityRejected,
    ActivityResult,
    Observation,
    TerminalState,
    dispatch_activity,
)
from bernstein.core.orchestration.activity_modalities import ContentStore, verify_run_activities
from bernstein.core.orchestration.research_worker import (
    ClaimDraft,
    ResearchBudget,
    ResearchBudgetExceeded,
    ResearchPlan,
    ResearchWorker,
    SpanRef,
)
from bernstein.core.replay.journal import EventJournal
from bernstein.core.security.audit_chain import EVENT_ACTIVITY_RESULT, AuditChainStore

_PAGES = {
    "https://a": b"<html>Python 3.13 ships an optional free-threaded build.</html>",
    "https://b": b"<html>Module foo is deprecated and slated for removal.</html>",
    "https://c": b"<html>The wire format gained a new length prefix field.</html>",
}


def _fetch(url: str) -> bytes:
    return _PAGES[url]


def _synth_two(query: str, fetched: tuple[object, ...]) -> list[ClaimDraft]:
    return [
        ClaimDraft(
            statement="3.13 has an optional free-threaded build",
            spans=(SpanRef(source_ref="https://a", quote="optional free-threaded build"),),
        ),
        ClaimDraft(
            statement="foo is deprecated",
            spans=(SpanRef(source_ref="https://b", quote="deprecated and slated for removal"),),
        ),
    ]


def _worker(tmp_path: Path, *, max_fetches: int = 10, **budget: float) -> ResearchWorker:
    store = ContentStore(tmp_path / ".sdd" / "cas")
    return ResearchWorker(store=store, budget=ResearchBudget(max_fetches=max_fetches, **budget))


# ---------------------------------------------------------------------------
# planning + content-addressed fetching
# ---------------------------------------------------------------------------


def test_plan_dedupes_and_preserves_order() -> None:
    plan = ResearchPlan.derive(query="q", sources=["u1", "u2", "u1", "u3", "u2"])
    assert plan.sources == ("u1", "u2", "u3")


def test_run_content_addresses_each_fetched_page(tmp_path: Path) -> None:
    worker = _worker(tmp_path)
    run = worker.run(query="what changed", sources=list(_PAGES), fetch_fn=_fetch, synthesise=_synth_two)
    # Every claim's citation pins the content hash of the page it was drawn from.
    for claim in run.report.claims:
        for citation in claim.citations:
            assert citation.page_content_hash.startswith("sha256:")
    assert run.result.kind is ActivityKind.RESEARCH
    assert run.result.terminal_state is TerminalState.COMPLETED


def test_run_refuses_uncited_claim_before_dispatch(tmp_path: Path) -> None:
    worker = _worker(tmp_path)

    def synth_uncited(query: str, fetched: tuple[object, ...]) -> list[ClaimDraft]:
        return [ClaimDraft(statement="unsupported", spans=())]

    with pytest.raises(ActivityRejected, match="no span"):
        worker.run(query="q", sources=["https://a"], fetch_fn=_fetch, synthesise=synth_uncited)


def test_run_refuses_claim_citing_unfetched_source(tmp_path: Path) -> None:
    worker = _worker(tmp_path)

    def synth_bad(query: str, fetched: tuple[object, ...]) -> list[ClaimDraft]:
        return [ClaimDraft(statement="s", spans=(SpanRef(source_ref="https://never", quote="x"),))]

    with pytest.raises(ActivityRejected, match="was not fetched"):
        worker.run(query="q", sources=["https://a"], fetch_fn=_fetch, synthesise=synth_bad)


# ---------------------------------------------------------------------------
# cost caps (AC4)
# ---------------------------------------------------------------------------


def test_max_fetches_cap_refuses_before_overspending(tmp_path: Path) -> None:
    worker = _worker(tmp_path, max_fetches=2)
    with pytest.raises(ResearchBudgetExceeded, match="max_fetches=2"):
        worker.run(query="q", sources=list(_PAGES), fetch_fn=_fetch, synthesise=_synth_two)


def test_cost_unit_cap_refuses(tmp_path: Path) -> None:
    worker = _worker(tmp_path, max_fetches=10, max_cost_units=1.5, cost_per_fetch=1.0)
    with pytest.raises(ResearchBudgetExceeded, match="max_cost_units"):
        worker.run(query="q", sources=list(_PAGES), fetch_fn=_fetch, synthesise=_synth_two)


def test_run_within_budget_succeeds(tmp_path: Path) -> None:
    worker = _worker(tmp_path, max_fetches=2)
    run = worker.run(query="q", sources=["https://a", "https://b"], fetch_fn=_fetch, synthesise=_synth_two)
    assert len(run.fetched) == 2


# ---------------------------------------------------------------------------
# dispatch + audit chain mirror + offline verify (AC2, AC3, AC5)
# ---------------------------------------------------------------------------


def test_report_is_content_addressed_and_verifies_offline(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    store = ContentStore(sdd / "cas")
    worker = ResearchWorker(store=store, budget=ResearchBudget(max_fetches=5))
    run = worker.run(query="q", sources=list(_PAGES), fetch_fn=_fetch, synthesise=_synth_two)

    # The report's canonical bytes are stored under the anchored artifact_hash.
    assert store.get(run.result.artifact_hash)

    journal = EventJournal(run_id="run-r", sdd_dir=sdd)
    dispatch_activity(run.result, stage_id="research-0", journal=journal)

    # Offline: verification touches only the content store, no network.
    verified = verify_run_activities(sdd, run_id="run-r", store=store)
    assert verified.ok
    stage = verified.stages[0]
    assert stage.kind == "research"
    assert stage.evidence_reattached
    assert [c.claim_id for c in stage.claim_verdicts] == ["c1", "c2"]
    assert all(c.ok for c in stage.claim_verdicts)


def test_dispatch_mirrors_into_audit_chain(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    store = ContentStore(sdd / "cas")
    worker = ResearchWorker(store=store, budget=ResearchBudget(max_fetches=5))
    run = worker.run(query="q", sources=["https://a", "https://b"], fetch_fn=_fetch, synthesise=_synth_two)

    chain = AuditChainStore(tmp_path / "audit", key=b"0" * 32)
    journal = EventJournal(run_id="run-r", sdd_dir=sdd)
    dispatch_activity(run.result, stage_id="research-0", journal=journal, chain=chain)

    rows = chain.query(event_type=EVENT_ACTIVITY_RESULT)
    assert len(rows) == 1
    details = rows[0].details
    assert details["kind"] == "research"
    assert details["artifact_hash"] == run.result.artifact_hash
    # The chain never carries the report body, only its hash.
    assert "claims" not in details


def test_verify_fails_naming_claim_when_source_altered(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    store = ContentStore(sdd / "cas")
    worker = ResearchWorker(store=store, budget=ResearchBudget(max_fetches=5))
    run = worker.run(query="q", sources=list(_PAGES), fetch_fn=_fetch, synthesise=_synth_two)
    journal = EventJournal(run_id="run-r", sdd_dir=sdd)
    dispatch_activity(run.result, stage_id="research-0", journal=journal)

    tampered_hash = run.report.claims[0].citations[0].page_content_hash
    store.force_put(tampered_hash, b"<html>rewritten, quote gone</html>")

    verified = verify_run_activities(sdd, run_id="run-r", store=store)
    assert not verified.ok
    stage = verified.stages[0]
    assert not stage.ok
    # The failure names the claim and the mismatched hash.
    assert "c1" in stage.reason
    assert tampered_hash in stage.reason
    failed = next(c for c in stage.claim_verdicts if not c.ok)
    assert failed.claim_id == "c1"


def test_two_verify_runs_produce_identical_verdicts(tmp_path: Path) -> None:
    sdd = tmp_path / ".sdd"
    store = ContentStore(sdd / "cas")
    worker = ResearchWorker(store=store, budget=ResearchBudget(max_fetches=5))
    run = worker.run(query="q", sources=list(_PAGES), fetch_fn=_fetch, synthesise=_synth_two)
    journal = EventJournal(run_id="run-r", sdd_dir=sdd)
    dispatch_activity(run.result, stage_id="research-0", journal=journal)

    first = verify_run_activities(sdd, run_id="run-r", store=store)
    second = verify_run_activities(sdd, run_id="run-r", store=store)
    assert first.ok == second.ok
    assert [
        (s.stage_id, s.ok, s.reason, [(c.claim_id, c.ok, c.reason) for c in s.claim_verdicts]) for s in first.stages
    ] == [(s.stage_id, s.ok, s.reason, [(c.claim_id, c.ok, c.reason) for c in s.claim_verdicts]) for s in second.stages]


def test_research_runs_dispatch_next_to_coding_tasks_with_cost_caps(tmp_path: Path) -> None:
    # A coding activity and a budgeted research activity anchor into the same run
    # journal through the one deterministic dispatch path (AC4).
    sdd = tmp_path / ".sdd"
    store = ContentStore(sdd / "cas")
    journal = EventJournal(run_id="run-mixed", sdd_dir=sdd)

    coding = ActivityResult.build(
        kind=ActivityKind.CODING,
        artifact={"diff": "patch"},
        observations=(Observation.of(kind="artifact", ref="spec", content=b"spec-bytes"),),
        terminal_state=TerminalState.COMPLETED,
        reason_code="ok",
    )
    dispatch_activity(coding, stage_id="coding-0", journal=journal)

    worker = ResearchWorker(store=store, budget=ResearchBudget(max_fetches=2))
    run = worker.run(query="q", sources=["https://a", "https://b"], fetch_fn=_fetch, synthesise=_synth_two)
    dispatch_activity(run.result, stage_id="research-0", journal=journal)

    verified = verify_run_activities(sdd, run_id="run-mixed", store=store)
    kinds = {s.stage_id: s.kind for s in verified.stages}
    assert kinds == {"coding-0": "coding", "research-0": "research"}
    # The research stage stayed inside its 2-fetch cap.
    assert len(run.fetched) == 2
