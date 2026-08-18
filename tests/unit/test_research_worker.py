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

import http.server
import sys
import threading
import urllib.request
from collections.abc import Iterator
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


# ---------------------------------------------------------------------------
# rendering source fetcher (#3120): captured bytes contain the cited span
# ---------------------------------------------------------------------------

# Built by string concatenation in the page script so the *static* response
# does not contain the marker as a contiguous string -- only the rendered DOM
# does. A fetcher that silently falls back to static fetching would therefore
# fail the positive assertion below.
_RENDER_MARKER = "CITED-SPAN-MARKER-9x7"
_RENDER_PAGE = """<!DOCTYPE html><html><body>
<p>Static shell paragraph without the marker.</p>
<script>
document.body.appendChild(Object.assign(document.createElement('span'), {
  id: 'cite',
  textContent: 'CITED' + '-SPAN-MARKER-9x7'
}));
</script>
</body></html>"""


class _FixtureHandler(http.server.BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        body = _RENDER_PAGE.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        pass


@pytest.fixture(scope="module")
def fixture_server() -> str:
    """Local fixture page served over loopback; tests never reach the internet."""
    server = http.server.HTTPServer(("127.0.0.1", 0), _FixtureHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}/page.html"
    server.shutdown()


_BROWSER_AVAILABLE: bool | None = None


def _browser_available() -> bool:
    """Whether a headless Chromium can actually launch in this process.

    Probed once and cached. The unit lane installs no browser binary, so
    contributors and CI see a skip rather than a red; the dedicated
    rendering lane (``.github/workflows/rendering-lane.yml``) installs the
    browser and raises the suite memory ceiling, so the probe passes there
    and the assertions below really run.
    """
    global _BROWSER_AVAILABLE
    if _BROWSER_AVAILABLE is not None:
        return _BROWSER_AVAILABLE
    try:
        import asyncio

        from playwright.async_api import async_playwright

        async def _probe() -> None:
            async with async_playwright() as p:
                browser = await p.chromium.launch(headless=True)
                await browser.close()

        asyncio.run(_probe())
        _BROWSER_AVAILABLE = True
    except Exception:
        # Executable absent (unit lane) or launch refused (e.g. the suite's
        # 2 GB RLIMIT_AS guard kills the child before it can start).
        _BROWSER_AVAILABLE = False
    return _BROWSER_AVAILABLE


@pytest.fixture
def _rendering_browser() -> Iterator[None]:
    """Skip unless a headless Chromium can actually launch in this process.

    These tests deliberately do not lift the suite's session-wide 2 GB
    RLIMIT_AS guard (``tests/conftest.py``): lifting it inside a test would
    leave every later test in the worker unguarded if the process dies
    between the lift and the restore. The rendering lane raises the ceiling
    up front via ``BERNSTEIN_MEM_GUARD_GB`` instead.
    """
    if not _browser_available():
        pytest.skip("headless Chromium unavailable (installed by the rendering lane)")
    yield


def test_rendering_fetch_records_script_injected_span(fixture_server: str, _rendering_browser: Iterator[None]) -> None:
    """Captured bytes contain the cited span when the static bytes do not."""
    from bernstein.core.orchestration.rendering_fetcher import make_rendering_fetcher

    # Half 1: the static fetcher's bytes do NOT contain the span.
    static = urllib.request.urlopen(fixture_server, timeout=10).read()
    assert _RENDER_MARKER.encode() not in static

    # Half 2: the rendering fetcher's bytes DO contain it. Asserting both
    # halves means a regression that silently drops back to static fetching
    # fails instead of passing quietly.
    rendered = make_rendering_fetcher()(fixture_server)
    assert _RENDER_MARKER.encode() in rendered


def test_rendering_fetch_refuses_typed_error_naming_source(
    _rendering_browser: Iterator[None],
) -> None:
    """A page that cannot render raises a typed refusal naming the source."""
    from bernstein.core.orchestration.rendering_fetcher import (
        RenderingFetchError,
        make_rendering_fetcher,
    )

    # Port 1 on loopback refuses connections; there is nothing to render.
    unreachable = "http://127.0.0.1:1/unreachable"
    with pytest.raises(RenderingFetchError) as exc_info:
        make_rendering_fetcher()(unreachable)
    assert unreachable in str(exc_info.value)


def test_failed_render_never_reaches_activity_fetch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _rendering_browser: Iterator[None],
) -> None:
    """A failed render records no observation: activity.fetch is never hit."""
    from bernstein.core.orchestration.activity_modalities import ResearchActivity
    from bernstein.core.orchestration.rendering_fetcher import (
        RenderingFetchError,
        make_rendering_fetcher,
    )

    calls: list[tuple[str, bytes]] = []
    original = ResearchActivity.fetch

    def spy(self: ResearchActivity, source_ref: str, content: bytes) -> Observation:
        calls.append((source_ref, content))
        return original(self, source_ref, content)

    monkeypatch.setattr(ResearchActivity, "fetch", spy)

    worker = _worker(tmp_path, max_fetches=5)
    with pytest.raises(RenderingFetchError):
        worker.run(
            query="q",
            sources=["http://127.0.0.1:1/unreachable"],
            fetch_fn=make_rendering_fetcher(),
            synthesise=_synth_two,
        )
    assert calls == []


def test_rendering_fetch_refuses_when_backend_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absent backend yields a typed unavailable error naming the package."""
    from bernstein.core.orchestration.rendering_fetcher import (
        RenderingBackendUnavailableError,
        make_rendering_fetcher,
    )

    # Pre-imported siblings (the probe above imports the package) make a
    # bare `sys.modules["playwright"] = None` a no-op on 3.12+: the parent
    # entry is ignored when the child module is already cached. Null the
    # child module so the from-import halts with a real ImportError.
    monkeypatch.setitem(sys.modules, "playwright.async_api", None)
    with pytest.raises(RenderingBackendUnavailableError, match="playwright"):
        make_rendering_fetcher()
