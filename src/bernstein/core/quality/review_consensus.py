"""Consensus scoring with detected-by provenance for review findings.

Where :mod:`bernstein.core.quality.pr_review_aggregator` clusters, votes,
ranks and renders free-text reviewer issues, this module owns the strict
*finding-level consensus shape*: when several independent review bots
(CodeRabbit, Sourcery, GitHub Advanced Security, ...) flag the same diff
locus, we dedup them, attach the list of bots that raised the finding,
and compute a consensus score plus a confidence bucket.

Why this exists separately from the aggregator
-----------------------------------------------
The legacy aggregator emits a flattened union: a finding raised by one
weak bot reads the same as a finding three bots raised independently.
Lifting consensus into the finding shape itself (which bots raised it,
what fraction agreed, what confidence band) lets operators filter on
agreement and lets gates require a minimum consensus level before a
finding blocks a merge.

Core shapes
-----------
* :class:`NormalizedFinding` - one bot's structured finding, with a
  strict schema (``bot``, ``finding_id``, ``severity``, ``category``,
  ``title`` and an :class:`Evidence` locus).
* :class:`ConsensusFinding` - the merged finding carrying
  ``detected_by`` (the distinct bots that raised it), ``agreement_ratio``
  (``len(detected_by) / bots_ran``), ``consensus_score``
  (``agreement_ratio * max-confidence``) and a :class:`ConsensusLevel`
  bucket.

Dedup rule
----------
Two findings describe the same issue when they share the same
``(file, line, category)`` key, **or** when they share file + category
and their titles are a fuzzy match (token Jaccard at or above
:data:`DEFAULT_TITLE_OVERLAP`). The line component is widened by
:data:`DEFAULT_LINE_WINDOW` so bots that round to the nearest hunk line
still merge.

Bucket thresholds (from the ticket spec)
-----------------------------------------
* ``consensus_score >= 0.66`` -> :attr:`ConsensusLevel.CONFIRMED`
* ``consensus_score >= 0.33`` -> :attr:`ConsensusLevel.NEEDS_VERIFICATION`
* otherwise                   -> :attr:`ConsensusLevel.UNVERIFIED`

A finding promoted to ``CONFIRMED`` is the "must-address" tier a CI gate
can require; ``NEEDS_VERIFICATION`` is a warning and ``UNVERIFIED`` is
informational only.

Pure logic, no network or LLM calls - fully unit testable.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Iterable

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Severity tags, ordered by ascending seriousness. Mirrors the aggregator
#: so the two modules speak the same vocabulary.
Severity = Literal["info", "low", "medium", "high", "critical"]

_SEVERITY_ORDER: dict[Severity, int] = {
    "info": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}

#: Bucket boundary at or above which a finding is "confirmed" / must-address.
CONFIRMED_THRESHOLD: float = 0.66

#: Bucket boundary at or above which a finding is "needs-verification".
NEEDS_VERIFICATION_THRESHOLD: float = 0.33

#: Two findings must point within this many lines of each other to be
#: candidates for the same locus. Bots often round to the nearest hunk
#: line, so a small window avoids false splits.
DEFAULT_LINE_WINDOW: int = 3

#: Minimum token Jaccard overlap between two titles for them to fuzzy-match
#: when file + category agree but the line differs or is absent.
DEFAULT_TITLE_OVERLAP: float = 0.5

#: Tokens stripped before computing title similarity - they add no
#: discriminative signal across bots.
_STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "for",
        "from",
        "if",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "should",
        "the",
        "this",
        "that",
        "to",
        "with",
        "would",
    }
)


class ConsensusLevel(StrEnum):
    """Confidence bucket a :class:`ConsensusFinding` lands in.

    Values are stable strings because they appear in gate config, audit
    records, and the rendered sticky comment.
    """

    CONFIRMED = "confirmed"
    NEEDS_VERIFICATION = "needs-verification"
    UNVERIFIED = "unverified"

    @property
    def rank(self) -> int:
        """Ordinal so callers can compare ``level >= ConsensusLevel.X`` cheaply."""
        return _LEVEL_RANK[self]


_LEVEL_RANK: dict[ConsensusLevel, int] = {
    ConsensusLevel.UNVERIFIED: 0,
    ConsensusLevel.NEEDS_VERIFICATION: 1,
    ConsensusLevel.CONFIRMED: 2,
}


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Evidence:
    """The diff locus a finding points at.

    Attributes:
        file: Repository-relative path. Empty string means global /
            unattributed.
        line: 1-indexed line number when known; ``None`` otherwise.
        snippet: Optional code snippet the bot cited (display only).
        symbol: Optional enclosing symbol (function / class) name.
    """

    file: str = ""
    line: int | None = None
    snippet: str = ""
    symbol: str = ""


@dataclass(frozen=True)
class NormalizedFinding:
    """A single review bot's structured finding.

    This is the strict input shape the consensus engine consumes. Each
    review bot adapter is responsible for normalising its raw output into
    this shape before handing it over.

    Attributes:
        bot: Identifier of the bot that raised the finding (e.g.
            ``coderabbit``, ``sourcery``, ``gh-advanced-security``).
        finding_id: Bot-local stable id for the finding (used for audit;
            not part of the dedup key).
        severity: Normalised severity tag.
        category: Coarse class of the issue (e.g. ``security``, ``perf``,
            ``style``). Part of the dedup key.
        title: Short human-readable title; used for fuzzy dedup + display.
        evidence: The diff locus.
        confidence: Bot self-reported confidence in ``[0.0, 1.0]``;
            defaults to ``1.0`` when the bot does not report one.
    """

    bot: str
    finding_id: str
    severity: Severity
    category: str
    title: str
    evidence: Evidence = field(default_factory=Evidence)
    confidence: float = 1.0

    @property
    def locus_key(self) -> tuple[str, int | None, str]:
        """The ``(file, line, category)`` dedup anchor for this finding."""
        return (self.evidence.file, self.evidence.line, self.category)


@dataclass(frozen=True)
class ConsensusFinding:
    """A merged finding with detected-by provenance and a consensus score.

    Attributes:
        file: Anchoring file path; empty string for global findings.
        line: Anchoring line (smallest non-null across members) or ``None``.
        category: Shared category across members.
        title: Most informative (longest) member title.
        severity: Highest severity across members - surface the worst.
        detected_by: Distinct bot ids that raised this finding, sorted for
            deterministic output.
        bots_ran: Total active bots in the run (denominator for agreement).
        agreement_ratio: ``len(detected_by) / bots_ran``, clamped to
            ``[0.0, 1.0]``.
        max_confidence: Highest member confidence.
        consensus_score: ``agreement_ratio * max_confidence``.
        level: The :class:`ConsensusLevel` bucket the score falls in.
        members: All findings folded into this finding (audit / drill-down).
    """

    file: str
    line: int | None
    category: str
    title: str
    severity: Severity
    detected_by: tuple[str, ...]
    bots_ran: int
    agreement_ratio: float
    max_confidence: float
    consensus_score: float
    level: ConsensusLevel
    members: tuple[NormalizedFinding, ...]

    @property
    def detected_by_count(self) -> int:
        """Number of distinct bots that raised this finding."""
        return len(self.detected_by)


# ---------------------------------------------------------------------------
# Scoring helpers
# ---------------------------------------------------------------------------


def bucket_for_score(score: float) -> ConsensusLevel:
    """Map a consensus score to its :class:`ConsensusLevel` bucket.

    Boundaries are inclusive on the lower edge per the ticket spec:
    ``>= 0.66`` confirmed, ``>= 0.33`` needs-verification, else unverified.
    """
    if score >= CONFIRMED_THRESHOLD:
        return ConsensusLevel.CONFIRMED
    if score >= NEEDS_VERIFICATION_THRESHOLD:
        return ConsensusLevel.NEEDS_VERIFICATION
    return ConsensusLevel.UNVERIFIED


def _normalise_tokens(text: str) -> frozenset[str]:
    """Lowercase, drop stopwords / short tokens, return a set for Jaccard."""
    tokens = re.findall(r"[^\W\d]\w+", text.lower(), flags=re.ASCII)
    return frozenset(t for t in tokens if t not in _STOPWORDS and len(t) >= 3)


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    """Jaccard similarity in ``[0, 1]``. Returns 0 for two empty sets."""
    if not a and not b:
        return 0.0
    union = a | b
    if not union:
        return 0.0
    return len(a & b) / len(union)


def _max_severity(members: Iterable[NormalizedFinding]) -> Severity:
    """Return the highest severity across *members*; ``info`` when empty."""
    best: Severity = "info"
    best_rank = -1
    for m in members:
        rank = _SEVERITY_ORDER[m.severity]
        if rank > best_rank:
            best_rank = rank
            best = m.severity
    return best


# ---------------------------------------------------------------------------
# Dedup / matching
# ---------------------------------------------------------------------------


def _findings_match(
    a: NormalizedFinding,
    b: NormalizedFinding,
    *,
    line_window: int,
    title_overlap: float,
) -> bool:
    """Decide whether *a* and *b* describe the same underlying issue.

    Rules:

    1. Same file (or both global) and same category - a hard prerequisite.
    2. When both lines are present, they must lie within ``line_window``;
       two findings at distant lines are distinct loci even if their
       titles are similar (e.g. "unused variable" at lines 10 and 200).
       Within the window the locus match is strong enough on its own.
    3. When at least one line is absent, fall back to a fuzzy title match
       at or above ``title_overlap``.
    """
    if a.category != b.category:
        return False
    if a.evidence.file != b.evidence.file:
        return False

    la, lb = a.evidence.line, b.evidence.line
    if la is not None and lb is not None:
        # Both anchored to a concrete line: proximity is decisive.
        return abs(la - lb) <= line_window

    overlap = _jaccard(_normalise_tokens(a.title), _normalise_tokens(b.title))
    return overlap >= title_overlap


def _finalise_cluster(
    members: list[NormalizedFinding],
    *,
    bots_ran: int,
) -> ConsensusFinding:
    """Fold a cluster of matched findings into one :class:`ConsensusFinding`."""
    detected_by = tuple(sorted({m.bot for m in members}))
    lines = [m.evidence.line for m in members if m.evidence.line is not None]
    line = min(lines) if lines else None
    title = max(members, key=lambda m: len(m.title)).title
    severity = _max_severity(members)
    # Clamp each member confidence into [0.0, 1.0] before taking the max so a
    # misbehaving adapter that reports out-of-range confidence cannot push the
    # consensus score outside its band and skew gate promotion.
    max_conf = max(min(max(m.confidence, 0.0), 1.0) for m in members)

    denom = max(bots_ran, 1)
    agreement = min(len(detected_by) / denom, 1.0)
    score = agreement * max_conf
    level = bucket_for_score(score)

    return ConsensusFinding(
        file=members[0].evidence.file,
        line=line,
        category=members[0].category,
        title=title,
        severity=severity,
        detected_by=detected_by,
        bots_ran=denom,
        agreement_ratio=agreement,
        max_confidence=max_conf,
        consensus_score=score,
        level=level,
        members=tuple(members),
    )


def compute_consensus(
    findings: Iterable[NormalizedFinding],
    *,
    bots_ran: int | None = None,
    line_window: int = DEFAULT_LINE_WINDOW,
    title_overlap: float = DEFAULT_TITLE_OVERLAP,
) -> list[ConsensusFinding]:
    """Dedup *findings* and score each merged finding by bot consensus.

    Greedy single-pass clustering by anchor - deterministic because input
    order is preserved. Real review-bot runs rarely exceed a few hundred
    findings, so the ``O(N*K)`` cost is irrelevant in practice.

    Args:
        findings: Per-bot :class:`NormalizedFinding` objects.
        bots_ran: Total distinct active bots in the run (the agreement
            denominator). Pass it explicitly when some bots produced zero
            findings, otherwise it is derived from the input bots and will
            under-count, inflating agreement.
        line_window: Line proximity tolerance for the locus match.
        title_overlap: Minimum title Jaccard for the fuzzy match.

    Returns:
        :class:`ConsensusFinding` objects sorted descending by
        ``consensus_score`` then severity then file then line, for
        deterministic rendering.
    """
    findings_list = list(findings)
    denom = bots_ran if bots_ran is not None else len({f.bot for f in findings_list})
    denom = max(denom, 1)

    clusters: list[list[NormalizedFinding]] = []
    for finding in findings_list:
        placed = False
        for members in clusters:
            if _findings_match(
                members[0],
                finding,
                line_window=line_window,
                title_overlap=title_overlap,
            ):
                members.append(finding)
                placed = True
                break
        if not placed:
            clusters.append([finding])

    consensus = [_finalise_cluster(members, bots_ran=denom) for members in clusters]
    consensus.sort(
        key=lambda c: (
            -c.consensus_score,
            -_SEVERITY_ORDER[c.severity],
            c.file,
            c.line if c.line is not None else 10**9,
        )
    )
    return consensus


def must_address(
    consensus: Iterable[ConsensusFinding],
    *,
    min_level: ConsensusLevel = ConsensusLevel.CONFIRMED,
) -> list[ConsensusFinding]:
    """Filter to findings that meet or exceed *min_level*.

    A CI gate uses this to require ``consensus_level >= confirmed`` for
    blocking issues; ``needs-verification`` is a warning tier and
    ``unverified`` is informational only.
    """
    return [c for c in consensus if c.level.rank >= min_level.rank]


# ---------------------------------------------------------------------------
# Rendering - the PR review summary provenance line
# ---------------------------------------------------------------------------


def render_provenance(finding: ConsensusFinding) -> str:
    """Render the ``[detected by N/M bots, agreement Y%]`` provenance tag.

    This is the snippet the PR review summary renderer appends
    to each finding so an operator can see consensus at a glance.
    """
    percent = round(finding.agreement_ratio * 100)
    return f"[detected by {finding.detected_by_count}/{finding.bots_ran} bots, agreement {percent}%]"


def render_consensus_markdown(consensus: Iterable[ConsensusFinding]) -> str:
    """Render consensus findings as a markdown block for the sticky comment.

    Emoji-free, GitHub-Flavoured-Markdown safe, deterministic ordering.
    Each finding line carries its severity, title, bot provenance tag, and
    the bot names that detected it.
    """
    items = list(consensus)
    lines: list[str] = ["## Review consensus", ""]
    if not items:
        lines.append("_No findings to report._")
        return "\n".join(lines)

    for level in (
        ConsensusLevel.CONFIRMED,
        ConsensusLevel.NEEDS_VERIFICATION,
        ConsensusLevel.UNVERIFIED,
    ):
        bucket = [c for c in items if c.level is level]
        if not bucket:
            continue
        lines.extend(["", f"### {level.value}", ""])
        for finding in bucket:
            location = ""
            if finding.file:
                location = f"`{finding.file}"
                location += f":{finding.line}`" if finding.line is not None else "`"
                location = f" {location}"
            bots = ", ".join(finding.detected_by)
            lines.append(
                f"- **[{finding.severity}]** {finding.title}{location} {render_provenance(finding)} _(by {bots})_"
            )
    return "\n".join(lines)


__all__ = [
    "CONFIRMED_THRESHOLD",
    "DEFAULT_LINE_WINDOW",
    "DEFAULT_TITLE_OVERLAP",
    "NEEDS_VERIFICATION_THRESHOLD",
    "ConsensusFinding",
    "ConsensusLevel",
    "Evidence",
    "NormalizedFinding",
    "Severity",
    "bucket_for_score",
    "compute_consensus",
    "must_address",
    "render_consensus_markdown",
    "render_provenance",
]
