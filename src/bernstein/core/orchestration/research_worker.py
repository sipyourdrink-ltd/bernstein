"""Research worker: sourced reports with offline-resolvable citation lineage (#2524).

The activity boundary ships a ``RESEARCH`` modality and a
:class:`~bernstein.core.orchestration.activity_modalities.ResearchActivity` that
content-addresses fetched pages at fetch time, but no worker turns that substrate
into a *report*. This module is that worker. It runs the deterministic half of a
research task -- plan the sources, fetch each one through
:meth:`ResearchActivity.fetch` so every page is content-addressed as it lands,
apply the cost cap, and assemble the fetched bytes and the synthesiser's drafts
into a citation-lineage :class:`~bernstein.core.orchestration.research_report.ResearchReport`
-- and hands a built
:class:`~bernstein.core.orchestration.activity.ActivityResult` to the same
:func:`~bernstein.core.orchestration.activity.dispatch_activity` path a coding
spawn uses.

The split is what keeps the guarantee deterministic. The *synthesiser* -- the
stochastic step that reads pages and drafts claims (a model in production) -- is
injected as an opaque callable; the worker never inspects how it drafts, only
that every quoted span it emits resolves to a source the worker actually fetched
and content-addressed. A draft that cites an unfetched source, or a claim with no
citation, is refused with
:class:`~bernstein.core.orchestration.activity.ActivityRejected` before the
result is built, so an uncited claim never reaches the journal. Everything the
worker itself does -- planning, fetching, budgeting, report assembly, hashing --
is a pure function of the query, the plan, and the fetched bytes, so two operators
with the same inputs assemble the byte-identical report and anchor the same
``artifact_hash``.

Cost caps are a first-class refusal, not advice: :class:`ResearchBudget` bounds
how many sources a run may fetch, and the worker raises
:class:`ResearchBudgetExceeded` the moment a fetch would cross the cap -- before
the fetch happens -- so a research task scheduled next to coding tasks stays
inside its budget the same way a coding task does.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from bernstein.core.orchestration.activity import (
    ActivityRejected,
    ActivityResult,
    TerminalState,
)
from bernstein.core.orchestration.activity_modalities import ContentStore, ResearchActivity
from bernstein.core.orchestration.research_report import (
    CitationRecord,
    ResearchClaim,
    ResearchReport,
    report_to_canonical_bytes,
    validate_research_report,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

__all__ = [
    "ClaimDraft",
    "FetchedSource",
    "ResearchBudget",
    "ResearchBudgetExceeded",
    "ResearchPlan",
    "ResearchRunResult",
    "ResearchWorker",
    "SpanRef",
]


class ResearchBudgetExceeded(RuntimeError):
    """A research run was refused because a fetch would cross its cost cap.

    Raised by :meth:`ResearchWorker.run` *before* the fetch that would exceed the
    cap, so a budgeted research task never spends past its cap. The message names
    the cap that was hit.
    """


@dataclass(frozen=True, slots=True)
class ResearchBudget:
    """The cost cap applied to a research run.

    A run may fetch at most ``max_fetches`` sources, and may spend at most
    ``max_cost_units`` (each fetch costs ``cost_per_fetch``). The cap is checked
    before every fetch, so it bounds the work a research task does the same way a
    coding task's budget bounds its spawns.

    Attributes:
        max_fetches: Maximum number of source fetches the run may perform.
        max_cost_units: Maximum total cost the run may spend (defaults to no
            cost ceiling beyond ``max_fetches``).
        cost_per_fetch: Cost charged per fetch.
    """

    max_fetches: int
    max_cost_units: float = math.inf
    cost_per_fetch: float = 1.0

    def __post_init__(self) -> None:
        if self.max_fetches < 0:
            raise ValueError("max_fetches must be non-negative")
        if self.cost_per_fetch < 0:
            raise ValueError("cost_per_fetch must be non-negative")


@dataclass(frozen=True, slots=True)
class ResearchPlan:
    """The ordered, de-duplicated set of sources a research run will consult.

    Planning is a pure projection of the query and the candidate sources: the
    plan preserves first-seen order and drops duplicates, so two operators with
    the same query and candidates derive the identical plan. It carries no live
    call -- it only decides *which* sources to fetch, not what they say.

    Attributes:
        query: The top-level research question.
        sources: The ordered, de-duplicated source references to fetch.
    """

    query: str
    sources: tuple[str, ...]

    @classmethod
    def derive(cls, *, query: str, sources: Sequence[str]) -> ResearchPlan:
        """Derive a plan from a query and candidate source references."""
        ordered: list[str] = []
        seen: set[str] = set()
        for source in sources:
            if source not in seen:
                seen.add(source)
                ordered.append(source)
        return cls(query=query, sources=tuple(ordered))


@dataclass(frozen=True, slots=True)
class FetchedSource:
    """A source fetched and content-addressed during a research run.

    Attributes:
        source_ref: The source reference (URL / label) fetched.
        content_hash: The ``sha256:`` hash the page bytes were stored under.
        content: The exact page bytes fetched.
    """

    source_ref: str
    content_hash: str
    content: bytes


@dataclass(frozen=True, slots=True)
class SpanRef:
    """A quoted span a synthesised claim rests on, tied to a source reference.

    Attributes:
        source_ref: The source the span was quoted from (must be one the worker
            fetched, so the span can be bound to content-addressed bytes).
        quote: The exact span quoted from that source.
    """

    source_ref: str
    quote: str


@dataclass(frozen=True, slots=True)
class ClaimDraft:
    """One claim the synthesiser drafted, with the spans it quotes.

    The worker turns each span into a :class:`CitationRecord` by resolving its
    ``source_ref`` to the content hash of the page the worker fetched, so the
    drafted claim becomes a claim bound to fetched bytes.

    Attributes:
        statement: The claim text.
        spans: The quoted spans supporting the claim (at least one required for
            the claim to survive the boundary check).
        claim_id: Optional stable id; the worker assigns ``c1``, ``c2`` ... when
            empty.
    """

    statement: str
    spans: tuple[SpanRef, ...] = ()
    claim_id: str = ""


@dataclass(frozen=True, slots=True)
class ResearchRunResult:
    """The outcome of a completed research run.

    Attributes:
        report: The assembled citation-lineage report (the activity artifact).
        result: The built :class:`ActivityResult` ready for
            :func:`~bernstein.core.orchestration.activity.dispatch_activity`.
        plan: The plan the run executed.
        fetched: The sources fetched, in fetch order.
    """

    report: ResearchReport
    result: ActivityResult
    plan: ResearchPlan
    fetched: tuple[FetchedSource, ...] = field(default_factory=tuple)


class ResearchWorker:
    """Runs the deterministic half of a research task under a cost cap (#2524).

    The worker plans the sources, fetches each through
    :meth:`ResearchActivity.fetch` (content-addressing every page at fetch time),
    enforces the :class:`ResearchBudget`, invokes the injected synthesiser to
    draft claims, and assembles a citation-lineage
    :class:`~bernstein.core.orchestration.research_report.ResearchReport`. The
    report's canonical bytes are stored content-addressed under the activity's
    ``artifact_hash`` so an offline verifier reattaches and re-verifies the report
    from the run content store alone.

    Args:
        store: The content-addressed store fetched pages and the report land in.
        budget: The cost cap the run must stay inside.
    """

    def __init__(self, *, store: ContentStore, budget: ResearchBudget) -> None:
        self._store = store
        self._budget = budget

    def run(
        self,
        *,
        query: str,
        sources: Sequence[str],
        fetch_fn: Callable[[str], bytes],
        synthesise: Callable[[str, tuple[FetchedSource, ...]], Sequence[ClaimDraft]],
        summary: str = "",
    ) -> ResearchRunResult:
        """Plan, fetch, synthesise, and assemble a citation-lineage report.

        Args:
            query: The top-level research question.
            sources: Candidate source references to consult (planned in order,
                de-duplicated).
            fetch_fn: Retrieves the exact bytes for a source reference. This is
                the only place the run touches the outside world; every returned
                blob is content-addressed at fetch time.
            synthesise: The opaque synthesiser drafting cited claims from the
                fetched sources.
            summary: An optional free-text synthesis stored on the report.

        Returns:
            A :class:`ResearchRunResult` whose ``result`` is ready to dispatch.

        Raises:
            ResearchBudgetExceeded: When a fetch would cross the cost cap.
            ActivityRejected: When the synthesiser drafts a claim with no span,
                or cites a source the worker did not fetch (an uncited claim never
                reaches the journal).
        """
        plan = ResearchPlan.derive(query=query, sources=sources)
        activity = ResearchActivity(store=self._store)

        fetched: list[FetchedSource] = []
        by_ref: dict[str, str] = {}
        spent = 0.0
        for index, source_ref in enumerate(plan.sources):
            if index + 1 > self._budget.max_fetches:
                raise ResearchBudgetExceeded(
                    f"research run exceeds max_fetches={self._budget.max_fetches} at source {source_ref!r}"
                )
            projected = spent + self._budget.cost_per_fetch
            if projected > self._budget.max_cost_units:
                raise ResearchBudgetExceeded(
                    f"research run exceeds max_cost_units={self._budget.max_cost_units} at source {source_ref!r}"
                )
            content = fetch_fn(source_ref)
            observation = activity.fetch(source_ref, content)
            spent = projected
            fetched.append(FetchedSource(source_ref=source_ref, content_hash=observation.content_hash, content=content))
            # Last write wins if a synthesiser later quotes an ambiguous ref; the
            # content hash still binds the citation to fetched bytes.
            by_ref[source_ref] = observation.content_hash

        drafts = synthesise(query, tuple(fetched))
        claims = self._assemble_claims(drafts, by_ref=by_ref)
        report = ResearchReport(query=query, claims=claims, summary=summary)

        # Boundary refusal (AC1): an uncited or malformed claim is refused here,
        # before the result is built and dispatched.
        validate_research_report(report)

        # Store the report's canonical bytes content-addressed so an offline
        # verifier reattaches it by the anchored artifact_hash.
        self._store.put(report_to_canonical_bytes(report))

        result = activity.finish(
            artifact=report.to_dict(),
            terminal_state=TerminalState.COMPLETED,
            reason_code="ok",
        )
        return ResearchRunResult(report=report, result=result, plan=plan, fetched=tuple(fetched))

    @staticmethod
    def _assemble_claims(
        drafts: Sequence[ClaimDraft],
        *,
        by_ref: dict[str, str],
    ) -> tuple[ResearchClaim, ...]:
        """Bind each drafted span to the content hash of the source it quotes."""
        claims: list[ResearchClaim] = []
        for index, draft in enumerate(drafts):
            claim_id = draft.claim_id.strip() or f"c{index + 1}"
            if not draft.spans:
                raise ActivityRejected(
                    f"synthesiser drafted claim {claim_id!r} with no span "
                    "(every claim must carry at least one citation record)"
                )
            citations: list[CitationRecord] = []
            for span in draft.spans:
                content_hash = by_ref.get(span.source_ref)
                if content_hash is None:
                    raise ActivityRejected(
                        f"claim {claim_id!r} cites source {span.source_ref!r} that was not fetched "
                        "(a citation must bind to a content-addressed page)"
                    )
                citations.append(
                    CitationRecord(
                        claim_id=claim_id,
                        quote=span.quote,
                        source_ref=span.source_ref,
                        page_content_hash=content_hash,
                    )
                )
            claims.append(ResearchClaim(claim_id=claim_id, statement=draft.statement, citations=tuple(citations)))
        return tuple(claims)
