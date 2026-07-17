"""Citation-lineage report model for the research modality (issue #2524).

The typed activity boundary already ships a ``RESEARCH`` modality and a
:class:`~bernstein.core.orchestration.activity_modalities.ResearchActivity` that
content-addresses every fetched page at fetch time, but a fetched-page store on
its own is not a *report*. This module is the report: a research report is not
prose with links, it is a citation-lineage artifact where every claim is bound
to the exact bytes it was derived from.

The shape is deliberate. A links-in-markdown report degrades the moment a cited
page is edited, moved, or rewritten -- the link still resolves, but to different
bytes, and no reader can tell. A citation-lineage report cannot: each
:class:`CitationRecord` pins the ``sha256:`` content hash of the fetched page
(the same hash the page was stored under in the run
:class:`~bernstein.core.orchestration.activity_modalities.ContentStore` at fetch
time) plus the exact quoted span the claim rests on. Verification is then a pure
offline function -- re-read the cited bytes by hash, confirm they still hash to
the pinned value, confirm the quote still occurs in them -- so a claim stays
checkable months later with the network disabled, and a single altered source
page fails verification naming the claim and the mismatched hash.

The module owns two halves of that contract:

* :func:`validate_research_report` -- the boundary check. A report whose every
  claim carries at least one well-formed citation passes; a report with an
  *uncited* claim (or a citation with an empty quote / a malformed page hash) is
  refused with :class:`~bernstein.core.orchestration.activity.ActivityRejected`
  *before* the result is built and dispatched, so it never reaches the journal.
* :func:`verify_research_report` -- the offline resolver. For every claim it
  reattaches each cited page's bytes from the content store by hash, re-hashes
  them to detect tamper, and confirms the quoted span is present, emitting a
  per-claim :class:`ClaimVerdict`. The verdict order is the report's claim order,
  so two verify runs over the same report produce byte-identical output.

The report itself is a JSON-serialisable :class:`ResearchReport`; the research
worker stores its canonical bytes content-addressed and anchors that hash as the
``artifact_hash`` of the dispatched
:class:`~bernstein.core.orchestration.activity.ActivityResult`, so
:func:`~bernstein.core.orchestration.activity_modalities.verify_run_activities`
reattaches and re-verifies the report from the run's content store alone.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from bernstein.core.orchestration.activity import ActivityRejected

if TYPE_CHECKING:
    from bernstein.core.orchestration.activity_modalities import ContentStore

__all__ = [
    "CitationRecord",
    "ClaimVerdict",
    "ResearchClaim",
    "ResearchReport",
    "ResearchReportVerdict",
    "report_to_canonical_bytes",
    "validate_research_report",
    "verify_research_report",
]


def _sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _content_hash(data: bytes) -> str:
    """Return the ``sha256:``-prefixed content hash of raw bytes."""
    return "sha256:" + _sha256_hex(data)


@dataclass(frozen=True, slots=True)
class CitationRecord:
    """One claim-to-source binding: a quoted span pinned to fetched-page bytes.

    A citation is the atom of report lineage. It names the claim it supports, the
    exact span quoted from the source, the human-facing provenance reference, and
    -- the load-bearing field -- the ``sha256:`` content hash of the fetched page
    as stored in the run content store at fetch time. Verification resolves the
    hash back to bytes and confirms the quote occurs in them, so the citation is
    only satisfiable against the exact bytes the claim was derived from.

    Attributes:
        claim_id: The id of the claim this citation supports.
        quote: The exact span quoted from the source page (must occur in it).
        source_ref: Human-facing provenance reference (the page URL / label).
        page_content_hash: ``sha256:`` hash of the fetched page bytes in the store.
    """

    claim_id: str
    quote: str
    source_ref: str
    page_content_hash: str

    def to_dict(self) -> dict[str, str]:
        """Return the JSON projection stored on the report."""
        return {
            "claim_id": self.claim_id,
            "quote": self.quote,
            "source_ref": self.source_ref,
            "page_content_hash": self.page_content_hash,
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> CitationRecord:
        """Rebuild a citation from its report projection."""
        return cls(
            claim_id=str(row.get("claim_id", "")),
            quote=str(row.get("quote", "")),
            source_ref=str(row.get("source_ref", "")),
            page_content_hash=str(row.get("page_content_hash", "")),
        )


@dataclass(frozen=True, slots=True)
class ResearchClaim:
    """A single claim in a research report plus its supporting citations.

    A claim is an assertion the report makes. It is only admissible if it carries
    at least one :class:`CitationRecord` binding it to fetched bytes; the boundary
    check refuses a report the moment a claim has none.

    Attributes:
        claim_id: Stable id for the claim (unique within the report).
        statement: The claim text.
        citations: The citations supporting the claim (at least one required).
    """

    claim_id: str
    statement: str
    citations: tuple[CitationRecord, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON projection stored on the report."""
        return {
            "claim_id": self.claim_id,
            "statement": self.statement,
            "citations": [c.to_dict() for c in self.citations],
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> ResearchClaim:
        """Rebuild a claim from its report projection."""
        raw_citations = row.get("citations", [])
        citations = (
            tuple(CitationRecord.from_dict(c) for c in raw_citations if isinstance(c, dict))
            if isinstance(raw_citations, list)
            else ()
        )
        return cls(
            claim_id=str(row.get("claim_id", "")),
            statement=str(row.get("statement", "")),
            citations=citations,
        )


@dataclass(frozen=True, slots=True)
class ResearchReport:
    """A sourced research report: the query, a summary, and cited claims.

    The report is the activity's ``artifact``. Its canonical JSON projection is
    hashed as the ``artifact_hash`` of the dispatched
    :class:`~bernstein.core.orchestration.activity.ActivityResult` and stored
    content-addressed, so an offline verifier reattaches and re-verifies it from
    the run content store by that hash alone.

    Attributes:
        query: The top-level research question the report answers.
        claims: The cited claims that make up the report body.
        summary: A short free-text synthesis (never a substitute for citations).
    """

    query: str
    claims: tuple[ResearchClaim, ...] = ()
    summary: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON artifact projection (hashed as ``artifact_hash``)."""
        return {
            "query": self.query,
            "summary": self.summary,
            "claims": [c.to_dict() for c in self.claims],
        }

    @classmethod
    def from_dict(cls, row: dict[str, Any]) -> ResearchReport:
        """Rebuild a report from its artifact projection."""
        raw_claims = row.get("claims", [])
        claims = (
            tuple(ResearchClaim.from_dict(c) for c in raw_claims if isinstance(c, dict))
            if isinstance(raw_claims, list)
            else ()
        )
        return cls(
            query=str(row.get("query", "")),
            claims=claims,
            summary=str(row.get("summary", "")),
        )


def validate_research_report(report: ResearchReport) -> ResearchReport:
    """Refuse a report with any uncited or malformed claim at the boundary (AC1).

    Enforces every invariant the citation-lineage guarantee rests on:

    * every claim carries a non-empty ``claim_id`` and at least one citation;
    * every citation names its own claim, quotes a non-empty span, and pins a
      ``sha256:``-shaped page content hash.

    Raised as :class:`~bernstein.core.orchestration.activity.ActivityRejected` so
    the research worker's build path refuses the report *before* the
    :class:`~bernstein.core.orchestration.activity.ActivityResult` is dispatched
    -- an uncited claim never reaches the journal or the audit chain.

    Args:
        report: The report to validate.

    Returns:
        The same report, unchanged, when valid.

    Raises:
        ActivityRejected: On the first uncited or malformed claim.
    """
    seen_ids: set[str] = set()
    for claim in report.claims:
        if not claim.claim_id.strip():
            raise ActivityRejected("research report claim has an empty claim_id")
        if claim.claim_id in seen_ids:
            raise ActivityRejected(f"research report has a duplicate claim_id: {claim.claim_id!r}")
        seen_ids.add(claim.claim_id)
        if not claim.citations:
            raise ActivityRejected(
                f"research report claim {claim.claim_id!r} has no citation "
                "(every claim must carry at least one citation record)"
            )
        for citation in claim.citations:
            if citation.claim_id != claim.claim_id:
                raise ActivityRejected(
                    f"citation on claim {claim.claim_id!r} names a different claim: {citation.claim_id!r}"
                )
            if not citation.quote:
                raise ActivityRejected(f"citation on claim {claim.claim_id!r} has an empty quote span")
            if not citation.page_content_hash.startswith("sha256:"):
                raise ActivityRejected(
                    f"citation on claim {claim.claim_id!r} has a malformed page hash: {citation.page_content_hash!r}"
                )
    return report


@dataclass(frozen=True, slots=True)
class ClaimVerdict:
    """Per-claim outcome of :func:`verify_research_report`.

    Attributes:
        claim_id: The claim this verdict covers.
        ok: True only when every citation resolved: bytes present, bytes still
            hash to the pinned value, and the quoted span occurs in them.
        citations_checked: The number of citations resolved for the claim.
        reason: A short explanation naming the claim (and, on tamper, the
            mismatched hash) when ``ok`` is False, else empty.
    """

    claim_id: str
    ok: bool
    citations_checked: int
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        """Return the JSON projection surfaced by the CLI."""
        return {
            "claim_id": self.claim_id,
            "ok": self.ok,
            "citations_checked": self.citations_checked,
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class ResearchReportVerdict:
    """Outcome of resolving every citation in a report offline.

    Attributes:
        ok: True only when every claim verdict is ``ok``.
        claims: Per-claim verdicts in the report's claim order (so two verify
            runs over the same report emit byte-identical output).
        reason: A short top-level explanation when the report itself is empty.
    """

    ok: bool
    claims: tuple[ClaimVerdict, ...] = ()
    reason: str = ""


def _verify_claim(claim: ResearchClaim, *, store: ContentStore) -> ClaimVerdict:
    """Resolve every citation on one claim against the content store."""
    if not claim.citations:
        return ClaimVerdict(
            claim_id=claim.claim_id,
            ok=False,
            citations_checked=0,
            reason=f"claim {claim.claim_id!r} has no citation",
        )
    checked = 0
    for citation in claim.citations:
        try:
            content = store.get(citation.page_content_hash)
        except KeyError:
            return ClaimVerdict(
                claim_id=claim.claim_id,
                ok=False,
                citations_checked=checked,
                reason=(
                    f"claim {claim.claim_id!r} citation unresolved: "
                    f"page bytes missing from store for {citation.page_content_hash!r}"
                ),
            )
        recomputed = _content_hash(content)
        if recomputed != citation.page_content_hash:
            return ClaimVerdict(
                claim_id=claim.claim_id,
                ok=False,
                citations_checked=checked,
                reason=(
                    f"claim {claim.claim_id!r} citation failed: cited page content hash mismatch "
                    f"(pinned {citation.page_content_hash!r}, recomputed {recomputed!r})"
                ),
            )
        if citation.quote.encode("utf-8") not in content:
            return ClaimVerdict(
                claim_id=claim.claim_id,
                ok=False,
                citations_checked=checked,
                reason=(
                    f"claim {claim.claim_id!r} citation failed: quoted span not found in cited page "
                    f"{citation.page_content_hash!r}"
                ),
            )
        checked += 1
    return ClaimVerdict(claim_id=claim.claim_id, ok=True, citations_checked=checked)


def verify_research_report(report: ResearchReport, *, store: ContentStore) -> ResearchReportVerdict:
    """Resolve every citation in *report* offline against the content store (AC2).

    For each claim, in report order, reattach each cited page's bytes from the
    content-addressed store by its pinned hash, re-hash them to detect an altered
    source page, and confirm the quoted span still occurs in them. Any failure
    marks the claim -- and the report -- as not ``ok`` with a reason naming the
    claim and, on tamper, the mismatched hash. The check touches only the store,
    so it holds with the network disabled, and its output is a pure function of
    the report and the stored bytes, so two runs produce identical verdicts.

    Args:
        report: The report whose citations to resolve.
        store: The content-addressed store holding the fetched page bytes.

    Returns:
        A :class:`ResearchReportVerdict`. ``ok`` requires every claim to resolve.
    """
    if not report.claims:
        return ResearchReportVerdict(ok=False, reason="research report holds no claims")
    verdicts = tuple(_verify_claim(claim, store=store) for claim in report.claims)
    return ResearchReportVerdict(ok=all(v.ok for v in verdicts), claims=verdicts)


def report_to_canonical_bytes(report: ResearchReport) -> bytes:
    """Return the canonical JSON bytes hashed as the report's ``artifact_hash``.

    Matches the canonicalisation the activity boundary hashes with, so the
    content hash of these bytes equals the anchored ``artifact_hash`` and the
    report reattaches from the store by that hash.
    """
    return json.dumps(report.to_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True, default=str).encode(
        "utf-8"
    )
