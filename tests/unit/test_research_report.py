"""Citation-lineage report model and offline verification (issue #2524).

A research report is a citation-lineage artifact, not prose with links. These
tests prove the two halves of that contract:

* the boundary check refuses a report with an uncited or malformed claim before
  it is ever dispatched (AC1); and
* offline verification resolves every citation from the content store alone,
  passes on intact bytes, fails naming the claim and the mismatched hash on an
  altered source page, and is deterministic across runs (AC2 / AC5). A
  links-in-markdown report carries no page hash and so cannot satisfy this.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from bernstein.core.orchestration.activity import ActivityRejected
from bernstein.core.orchestration.activity_modalities import ContentStore
from bernstein.core.orchestration.research_report import (
    CitationRecord,
    ResearchClaim,
    ResearchReport,
    report_to_canonical_bytes,
    validate_research_report,
    verify_research_report,
)


def _store_page(store: ContentStore, content: bytes) -> str:
    return store.put(content)


def _report_over(store: ContentStore) -> ResearchReport:
    """A two-claim report whose citations resolve against *store*."""
    h1 = _store_page(store, b"<html>Python 3.13 ships an optional free-threaded build.</html>")
    h2 = _store_page(store, b"<html>Module foo is deprecated and slated for removal.</html>")
    return ResearchReport(
        query="what changed",
        summary="two findings",
        claims=(
            ResearchClaim(
                claim_id="c1",
                statement="3.13 has an optional free-threaded build",
                citations=(
                    CitationRecord(
                        claim_id="c1",
                        quote="optional free-threaded build",
                        source_ref="https://a",
                        page_content_hash=h1,
                    ),
                ),
            ),
            ResearchClaim(
                claim_id="c2",
                statement="foo is deprecated",
                citations=(
                    CitationRecord(
                        claim_id="c2",
                        quote="deprecated and slated for removal",
                        source_ref="https://b",
                        page_content_hash=h2,
                    ),
                ),
            ),
        ),
    )


# ---------------------------------------------------------------------------
# boundary check: uncited / malformed claims are refused (AC1)
# ---------------------------------------------------------------------------


def test_valid_report_passes_boundary(tmp_path: Path) -> None:
    store = ContentStore(tmp_path / "cas")
    report = _report_over(store)
    assert validate_research_report(report) is report


def test_uncited_claim_is_refused() -> None:
    report = ResearchReport(
        query="q",
        claims=(ResearchClaim(claim_id="c1", statement="unsupported", citations=()),),
    )
    with pytest.raises(ActivityRejected, match="no citation"):
        validate_research_report(report)


def test_empty_quote_is_refused() -> None:
    report = ResearchReport(
        query="q",
        claims=(
            ResearchClaim(
                claim_id="c1",
                statement="s",
                citations=(CitationRecord(claim_id="c1", quote="", source_ref="r", page_content_hash="sha256:aa"),),
            ),
        ),
    )
    with pytest.raises(ActivityRejected, match="empty quote"):
        validate_research_report(report)


def test_malformed_page_hash_is_refused() -> None:
    report = ResearchReport(
        query="q",
        claims=(
            ResearchClaim(
                claim_id="c1",
                statement="s",
                citations=(CitationRecord(claim_id="c1", quote="x", source_ref="r", page_content_hash="not-a-hash"),),
            ),
        ),
    )
    with pytest.raises(ActivityRejected, match="malformed page hash"):
        validate_research_report(report)


def test_citation_naming_wrong_claim_is_refused() -> None:
    report = ResearchReport(
        query="q",
        claims=(
            ResearchClaim(
                claim_id="c1",
                statement="s",
                citations=(CitationRecord(claim_id="c2", quote="x", source_ref="r", page_content_hash="sha256:aa"),),
            ),
        ),
    )
    with pytest.raises(ActivityRejected, match="names a different claim"):
        validate_research_report(report)


def test_duplicate_claim_id_is_refused() -> None:
    cite = CitationRecord(claim_id="c1", quote="x", source_ref="r", page_content_hash="sha256:aa")
    report = ResearchReport(
        query="q",
        claims=(
            ResearchClaim(claim_id="c1", statement="a", citations=(cite,)),
            ResearchClaim(claim_id="c1", statement="b", citations=(cite,)),
        ),
    )
    with pytest.raises(ActivityRejected, match="duplicate claim_id"):
        validate_research_report(report)


# ---------------------------------------------------------------------------
# offline verification: resolve every citation from the store (AC2)
# ---------------------------------------------------------------------------


def test_verify_resolves_every_citation(tmp_path: Path) -> None:
    store = ContentStore(tmp_path / "cas")
    report = _report_over(store)
    verdict = verify_research_report(report, store=store)
    assert verdict.ok
    assert [c.claim_id for c in verdict.claims] == ["c1", "c2"]
    assert all(c.ok for c in verdict.claims)
    assert all(c.citations_checked == 1 for c in verdict.claims)


def test_verify_fails_when_cited_page_altered_naming_claim_and_hash(tmp_path: Path) -> None:
    store = ContentStore(tmp_path / "cas")
    report = _report_over(store)
    tampered_hash = report.claims[0].citations[0].page_content_hash
    # Alter the stored bytes under the pinned hash: the recomputed hash diverges.
    store.force_put(tampered_hash, b"<html>rewritten, quote gone</html>")

    verdict = verify_research_report(report, store=store)
    assert not verdict.ok
    bad = verdict.claims[0]
    assert not bad.ok
    assert bad.claim_id == "c1"
    assert tampered_hash in bad.reason
    # The untouched claim still resolves.
    assert verdict.claims[1].ok


def test_verify_fails_when_quote_absent(tmp_path: Path) -> None:
    store = ContentStore(tmp_path / "cas")
    page = store.put(b"<html>the page body says something else entirely</html>")
    report = ResearchReport(
        query="q",
        claims=(
            ResearchClaim(
                claim_id="c1",
                statement="s",
                citations=(
                    CitationRecord(claim_id="c1", quote="not present here", source_ref="r", page_content_hash=page),
                ),
            ),
        ),
    )
    verdict = verify_research_report(report, store=store)
    assert not verdict.ok
    assert "quoted span not found" in verdict.claims[0].reason


def test_verify_fails_when_page_missing_from_store(tmp_path: Path) -> None:
    store = ContentStore(tmp_path / "cas")
    report = ResearchReport(
        query="q",
        claims=(
            ResearchClaim(
                claim_id="c1",
                statement="s",
                citations=(
                    CitationRecord(claim_id="c1", quote="x", source_ref="r", page_content_hash="sha256:" + "0" * 64),
                ),
            ),
        ),
    )
    verdict = verify_research_report(report, store=store)
    assert not verdict.ok
    assert "missing from store" in verdict.claims[0].reason


def test_verify_is_deterministic_across_runs(tmp_path: Path) -> None:
    store = ContentStore(tmp_path / "cas")
    report = _report_over(store)
    # Tamper one page so a failure verdict is produced (the more interesting case
    # for determinism), then verify twice.
    store.force_put(report.claims[1].citations[0].page_content_hash, b"gone")
    first = verify_research_report(report, store=store)
    second = verify_research_report(report, store=store)
    assert [(c.claim_id, c.ok, c.reason, c.citations_checked) for c in first.claims] == [
        (c.claim_id, c.ok, c.reason, c.citations_checked) for c in second.claims
    ]


def test_report_roundtrips_through_dict(tmp_path: Path) -> None:
    store = ContentStore(tmp_path / "cas")
    report = _report_over(store)
    restored = ResearchReport.from_dict(report.to_dict())
    assert restored == report
    # Canonical bytes are stable, so the report reattaches from the store by hash.
    assert report_to_canonical_bytes(restored) == report_to_canonical_bytes(report)
