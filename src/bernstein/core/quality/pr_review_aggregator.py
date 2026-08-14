"""Cluster, vote, score and rank PR-review findings across N reviewers.

This module is the *finding-level* aggregator that complements
:mod:`bernstein.core.quality.review_pipeline.verdict` (which aggregates
*verdicts*).  Where the verdict aggregator answers "did the pipeline
pass?", this module answers "which specific issues did multiple
reviewers agree on, and what should we surface to the PR author?".

Pipeline:

1. Parse each free-text issue string into a :class:`PRFinding` -
   best-effort extraction of file, line, severity, and a normalised
   message used for cross-reviewer dedupe.
2. Cluster findings into :class:`FindingCluster` groups using
   ``(file, line ± LINE_WINDOW, normalised-message-token-overlap)``.
3. Vote: keep clusters that either (a) were raised by ≥ ``min_reviewers``
   distinct reviewer roles, or (b) carry ``critical``/``high`` severity
   (single-reviewer veto for security-class issues).
4. Score each surviving cluster with a deterministic weighted formula
   and rank top-K, grouped by file for readable output.

Designed as the smallest viable slice of multi-pass PR review with
voting + autofix.  No LLM calls live here - the module is pure logic
over already-collected :class:`AgentVerdict` data so it is fully unit
testable without network access.

Deferred (tracked in follow-up tickets):
- Embedding-based similarity for clustering (currently lexical only).
- Severity inference via a dedicated LLM scoring pass.
- Posting the grouped report through ``gh pr review``.
- Autofix orchestration per surviving cluster.
"""

from __future__ import annotations

import logging
import math
import re
from collections import defaultdict
from dataclasses import dataclass
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from bernstein.core.quality.review_pipeline.verdict import (
        AgentVerdict,
        PipelineVerdict,
    )

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Constants - tuneable knobs surfaced as module-level for ease of override.
# ---------------------------------------------------------------------------

#: Severity tags the parser recognises, ordered by ascending seriousness.
Severity = Literal["info", "low", "medium", "high", "critical"]

_SEVERITY_ORDER: dict[Severity, int] = {
    "info": 0,
    "low": 1,
    "medium": 2,
    "high": 3,
    "critical": 4,
}

#: Severity → score weight used by :func:`score_cluster`.  Higher = more
#: prominent in the ranked output.
_SEVERITY_WEIGHT: dict[Severity, float] = {
    "info": 0.1,
    "low": 0.3,
    "medium": 0.6,
    "high": 1.0,
    "critical": 1.5,
}

#: Severities that bypass the minimum-reviewer-count vote - a single
#: reviewer flagging a critical/high finding still surfaces to the
#: author.  Mirrors security-team conventions where a lone hit on a
#: real CVE class should never be silenced by majority vote.
_SINGLE_REVIEWER_VETO_SEVERITIES: frozenset[Severity] = frozenset({"critical", "high"})

#: Two findings must point within this many lines of each other to be
#: candidates for the same cluster.  Reviewers often round to the
#: nearest 5-line block when a hunk is small, so 3 is a safe default.
DEFAULT_LINE_WINDOW: int = 3

#: Minimum Jaccard token-overlap between two normalised messages for
#: them to be considered the "same" finding when file/line agree.
DEFAULT_TOKEN_OVERLAP: float = 0.4

#: Default cap on how many clusters to surface in the final report.
DEFAULT_TOP_K: int = 15

#: Tokens stripped before computing message similarity - they add no
#: discriminative signal across reviewers.
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


# Match `path/to/file.ext:NN` or `path/to/file.ext` only when followed by
# whitespace / colon-space.  Tightened to reduce false positives on
# arbitrary dotted names that happen to look like file paths.
_FILE_LINE_RE = re.compile(
    r"(?<![A-Za-z0-9_.:/-])"
    r"(?P<file>(?:[A-Za-z0-9_.-]+/)*[A-Za-z0-9_-]+\.[A-Za-z]{1,8})(?::(?P<line>\d+))?"
    r"(?![A-Za-z0-9_.:/-])",
)

# Severity tag forms seen in the wild from cheap-tier reviewers:
#   `[critical]`  `(high)`  `severity: medium`  `**LOW**`
_SEVERITY_RE = re.compile(
    r"(?:severity\s*[:=]\s*|\[|\(|\*\*)?"
    r"(?P<sev>critical|high|medium|low|info)"
    r"(?:\]|\)|\*\*)?",
    re.IGNORECASE,
)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PRFinding:
    """A structured finding parsed from one reviewer's free-text issue.

    Attributes:
        file: Path to the offending file when extractable; empty string
            otherwise.  Empty file means "global / unattributed".
        line: 1-indexed line number when extractable; ``None`` otherwise.
        severity: Normalised severity tag; defaults to ``medium`` when
            the reviewer did not flag one explicitly.
        message: Cleaned-up reviewer message with file/line/severity
            markers stripped - used for display and similarity.
        source_role: Reviewer role that produced this finding (e.g.
            ``security``, ``qa``).  Drives the "≥N distinct roles"
            voting threshold.
        source_model: Reviewer model identifier for audit / diversity
            scoring.  Two findings from the same role but different
            models still count as one role for voting purposes.
        confidence: Reviewer self-reported confidence in [0.0, 1.0].
            Defaults to 1.0 to match :class:`AgentVerdict` defaults.
    """

    file: str
    line: int | None
    severity: Severity
    message: str
    source_role: str
    source_model: str
    confidence: float = 1.0


@dataclass(frozen=True)
class FindingCluster:
    """A group of cross-reviewer findings deemed to describe the same issue.

    Attributes:
        file: Anchoring file path; empty string for global findings.
        line: Anchoring line (median across members) or ``None``.
        severity: Highest severity across members - surface the worst.
        canonical_message: Longest member message, used for display.
        members: All findings folded into this cluster.
        score: Final ranking score from :func:`score_cluster`.
    """

    file: str
    line: int | None
    severity: Severity
    canonical_message: str
    members: tuple[PRFinding, ...]
    score: float = 0.0

    @property
    def reviewer_roles(self) -> frozenset[str]:
        """Distinct reviewer roles that raised this cluster."""
        return frozenset(m.source_role for m in self.members)

    @property
    def reviewer_count(self) -> int:
        """Distinct reviewer roles count - drives the voting threshold."""
        return len(self.reviewer_roles)


@dataclass(frozen=True)
class PRReviewReport:
    """Final aggregated report emitted to the operator / GitHub poster.

    Attributes:
        clusters: Surviving, ranked, top-K findings.  Already sorted
            descending by :attr:`FindingCluster.score`.
        by_file: Same clusters grouped by file (preserves rank order
            within each file).  Empty-string key holds global findings.
        total_input: Total raw findings considered.
        total_clusters: Distinct clusters formed before voting.
        n_reviewers: Distinct reviewer roles seen across the input.
    """

    clusters: tuple[FindingCluster, ...]
    by_file: Mapping[str, tuple[FindingCluster, ...]]
    total_input: int
    total_clusters: int
    n_reviewers: int


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _detect_severity(text: str) -> Severity:
    """Return the highest severity tag present in *text*, or ``medium``.

    Multiple matches are possible (e.g. reviewer wrote ``[high] ... low``
    in a comparison); we surface the most serious to err on the side of
    visibility.
    """
    best: Severity = "medium"
    best_rank = -1
    for match in _SEVERITY_RE.finditer(text):
        sev = match.group("sev").lower()
        if sev not in _SEVERITY_ORDER:
            continue
        sev_typed: Severity = sev  # type: ignore[assignment]
        rank = _SEVERITY_ORDER[sev_typed]
        if rank > best_rank:
            best_rank = rank
            best = sev_typed
    if best_rank < 0:
        # Hint words that are nearly always serious.  Cheap heuristic;
        # avoids false negatives when reviewers skip the explicit tag.
        lower = text.lower()
        if any(kw in lower for kw in ("rce", "sql injection", "secret leak", "hardcoded credential")):
            return "critical"
        if any(kw in lower for kw in ("xss", "csrf", "missing auth", "race condition")):
            return "high"
    return best


def _detect_file_line(text: str) -> tuple[str, int | None]:
    """Return ``(file, line)`` extracted from *text* or ``("", None)``.

    Picks the first plausible match - reviewers occasionally cite multiple
    files in a single bullet; the first is usually the primary one.
    """
    match = _FILE_LINE_RE.search(text)
    if match is None:
        return "", None
    file_path = match.group("file")
    line_str = match.group("line")
    line: int | None = None
    if line_str is not None:
        try:
            line = int(line_str)
        except ValueError:
            line = None
    return file_path, line


def _strip_markers(text: str, file: str) -> str:
    """Remove severity/file markers from *text* for display + similarity.

    Conservative: only strips the *first* file occurrence to keep enough
    context when a finding mentions related files.
    """
    out = text
    if file:
        # Strip `file:line` form first, then bare file.  The order matters
        # - the colon-line form is a strict superset of the bare path.
        out = re.sub(rf"{re.escape(file)}:\d+", "", out, count=1)
        out = out.replace(file, "", 1)
    out = _SEVERITY_RE.sub("", out)
    # Tidy up artifacts left behind: empty brackets, double spaces,
    # leading punctuation.
    out = re.sub(r"\[\s*\]|\(\s*\)|\*\*\s*\*\*", "", out)
    out = re.sub(r"\s{2,}", " ", out).strip(" -:.,")
    return out


def parse_finding(
    raw: str,
    *,
    source_role: str,
    source_model: str,
    confidence: float = 1.0,
) -> PRFinding | None:
    """Parse one free-text reviewer issue into a :class:`PRFinding`.

    Returns ``None`` for empty / whitespace-only inputs so callers can
    use it as a filter without an extra guard.

    Args:
        raw: Reviewer issue text - what comes out of
            :class:`AgentVerdict.issues`.
        source_role: Reviewer role tag.
        source_model: Reviewer model identifier.
        confidence: Self-reported confidence.

    Returns:
        :class:`PRFinding` or ``None`` when *raw* is empty.
    """
    text = raw.strip()
    if not text:
        return None
    file_path, line = _detect_file_line(text)
    severity = _detect_severity(text)
    message = _strip_markers(text, file_path) or text  # never produce empty
    return PRFinding(
        file=file_path,
        line=line,
        severity=severity,
        message=message,
        source_role=source_role,
        source_model=source_model,
        confidence=confidence,
    )


def parse_findings_from_pipeline(verdict: PipelineVerdict) -> list[PRFinding]:
    """Flatten a :class:`PipelineVerdict` into a list of :class:`PRFinding`.

    Walks every stage's every agent's ``issues`` list, parsing each
    string into a finding tagged with the agent's role + model.  Skips
    agents that approved (no issues) and unparseable inputs.
    """
    findings: list[PRFinding] = []
    for stage in verdict.stages:
        for agent in stage.agents:
            findings.extend(parse_findings_from_agent(agent))
    return findings


def parse_findings_from_agent(agent: AgentVerdict) -> list[PRFinding]:
    """Parse one agent's verdict into structured findings."""
    out: list[PRFinding] = []
    for issue in agent.issues:
        parsed = parse_finding(
            issue,
            source_role=agent.role,
            source_model=agent.model,
            confidence=agent.confidence,
        )
        if parsed is not None:
            out.append(parsed)
    return out


# ---------------------------------------------------------------------------
# Clustering
# ---------------------------------------------------------------------------


def _normalise_tokens(text: str) -> frozenset[str]:
    """Lowercase, drop stopwords, return a token set for Jaccard similarity."""
    tokens = re.findall(r"[^\W\d]\w+", text.lower(), flags=re.ASCII)
    return frozenset(t for t in tokens if t not in _STOPWORDS and len(t) >= 3)


def _jaccard(a: frozenset[str], b: frozenset[str]) -> float:
    """Jaccard similarity ∈ [0, 1].  Returns 0 for two empty sets."""
    if not a and not b:
        return 0.0
    intersection = a & b
    union = a | b
    if not union:
        return 0.0
    return len(intersection) / len(union)


def _findings_match(
    a: PRFinding,
    b: PRFinding,
    *,
    line_window: int,
    token_overlap: float,
) -> bool:
    """Decide whether *a* and *b* describe the same underlying issue.

    Rules (all must hold):

    1. Same file (or both global / file-less).
    2. Lines within ``line_window`` of each other when both present;
       otherwise treated as compatible.
    3. Token-overlap ≥ ``token_overlap`` *unless* both file AND line
       agree exactly (in which case file/line agreement is strong
       enough on its own - reviewers often phrase the same issue very
       differently).
    """
    if a.file != b.file:
        return False
    if a.line is not None and b.line is not None:
        if abs(a.line - b.line) > line_window:
            return False
        if a.line == b.line and a.file:
            return True  # exact file:line match overrides similarity
    overlap = _jaccard(_normalise_tokens(a.message), _normalise_tokens(b.message))
    return overlap >= token_overlap


def cluster_findings(
    findings: Iterable[PRFinding],
    *,
    line_window: int = DEFAULT_LINE_WINDOW,
    token_overlap: float = DEFAULT_TOKEN_OVERLAP,
) -> list[FindingCluster]:
    """Group near-duplicate findings into :class:`FindingCluster` instances.

    Greedy single-pass union-by-anchor: the first finding in each cluster
    becomes the comparison anchor.  This is O(N·K) where K is the
    cluster count; deterministic across runs because input order is
    preserved.

    Multi-stage matching against an anchor is acceptable here because
    real PR-review inputs rarely exceed a few hundred raw findings -
    embedding-based similarity is tracked as a follow-up.
    """
    clusters_members: list[list[PRFinding]] = []
    for finding in findings:
        placed = False
        for members in clusters_members:
            anchor = members[0]
            if _findings_match(
                anchor,
                finding,
                line_window=line_window,
                token_overlap=token_overlap,
            ):
                members.append(finding)
                placed = True
                break
        if not placed:
            clusters_members.append([finding])

    return [_finalise_cluster(members) for members in clusters_members]


def _finalise_cluster(members: list[PRFinding]) -> FindingCluster:
    """Pick canonical fields for a cluster from its members.

    Severity = max across members; line = median (when present); message
    = the longest member message (proxy for the most informative
    explanation, cheap to compute, no LLM needed).
    """
    severity: Severity = "info"
    severity_rank = -1
    for m in members:
        rank = _SEVERITY_ORDER[m.severity]
        if rank > severity_rank:
            severity_rank = rank
            severity = m.severity

    lines = sorted(m.line for m in members if m.line is not None)
    line: int | None = lines[len(lines) // 2] if lines else None

    canonical = max(members, key=lambda m: len(m.message)).message
    file = members[0].file  # all members share file by construction

    return FindingCluster(
        file=file,
        line=line,
        severity=severity,
        canonical_message=canonical,
        members=tuple(members),
    )


# ---------------------------------------------------------------------------
# Voting
# ---------------------------------------------------------------------------


def _default_min_reviewers(n_reviewers: int) -> int:
    """Threshold for keeping a cluster: ⌈N/2⌉, floor 1.

    Mirrors the ticket spec ("majority-vote: keep clusters with
    ≥ceil(N/2) reviewers").  When N=1 the threshold collapses to 1 so
    a single-reviewer pipeline still surfaces every finding.
    """
    return max(1, math.ceil(n_reviewers / 2))


def vote_clusters(
    clusters: list[FindingCluster],
    *,
    n_reviewers: int,
    min_reviewers: int | None = None,
) -> list[FindingCluster]:
    """Filter clusters by reviewer-count + severity-veto rules.

    A cluster survives when *either*:

    * ≥ ``min_reviewers`` distinct reviewer roles raised it, **or**
    * its severity is in :data:`_SINGLE_REVIEWER_VETO_SEVERITIES`
      (critical / high) - security-class issues are never silenced by
      majority vote.

    Returns the surviving clusters in input order - ranking happens in
    :func:`rank_clusters`.
    """
    threshold = min_reviewers if min_reviewers is not None else _default_min_reviewers(n_reviewers)
    survivors: list[FindingCluster] = []
    for cluster in clusters:
        meets_quorum = cluster.reviewer_count >= threshold
        veto = cluster.severity in _SINGLE_REVIEWER_VETO_SEVERITIES
        if meets_quorum or veto:
            survivors.append(cluster)
        else:
            logger.debug(
                "pr_review_aggregator: dropping cluster file=%s line=%s severity=%s (reviewers=%d/%d, veto=False)",
                cluster.file,
                cluster.line,
                cluster.severity,
                cluster.reviewer_count,
                threshold,
            )
    return survivors


# ---------------------------------------------------------------------------
# Scoring + ranking
# ---------------------------------------------------------------------------


def score_cluster(cluster: FindingCluster, *, n_reviewers: int) -> float:
    """Compute a deterministic ranking score for *cluster*.

    Formula (all components ∈ [0, ~2]):

    * **severity_weight**: from :data:`_SEVERITY_WEIGHT`.
    * **agreement**: ``reviewer_count / max(n_reviewers, 1)``.
    * **mean_confidence**: average self-reported confidence across
      members, falling back to 1.0 when none reported.

    Score = ``severity_weight + agreement + 0.5·mean_confidence``.
    Empirically gives security/critical findings a steady lead over
    style nits while still letting unanimous low-severity issues bubble
    above lone-flagger medium ones.  Coefficients are tuned for
    behaviour, not statistical optimality - revisit when real telemetry
    is available.
    """
    severity_weight = _SEVERITY_WEIGHT[cluster.severity]
    agreement = cluster.reviewer_count / max(n_reviewers, 1)
    confidences = [m.confidence for m in cluster.members]
    mean_conf = sum(confidences) / len(confidences) if confidences else 1.0
    return severity_weight + agreement + 0.5 * mean_conf


def rank_clusters(
    clusters: Iterable[FindingCluster],
    *,
    n_reviewers: int,
    top_k: int = DEFAULT_TOP_K,
) -> list[FindingCluster]:
    """Score, sort descending, and cap at ``top_k`` clusters.

    Stable sort secondary key: reverse reviewer_count, then file, then
    line (puts file-less / line-less findings at the bottom of ties).
    Determinism across runs matters for snapshot tests downstream.
    """
    scored = [
        FindingCluster(
            file=c.file,
            line=c.line,
            severity=c.severity,
            canonical_message=c.canonical_message,
            members=c.members,
            score=score_cluster(c, n_reviewers=n_reviewers),
        )
        for c in clusters
    ]
    scored.sort(
        key=lambda c: (
            -c.score,
            -c.reviewer_count,
            c.file,
            c.line if c.line is not None else 10**9,
        )
    )
    if top_k <= 0:
        return scored
    return scored[:top_k]


def group_by_file(
    clusters: Iterable[FindingCluster],
) -> dict[str, tuple[FindingCluster, ...]]:
    """Bucket clusters by file while preserving input order within each bucket.

    The empty-string key collects clusters with no file attribution.
    Used by the GitHub poster to emit one inline-comment block per file.
    """
    buckets: dict[str, list[FindingCluster]] = defaultdict(list[FindingCluster])
    for cluster in clusters:
        buckets[cluster.file].append(cluster)
    return {file: tuple(items) for file, items in buckets.items()}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def aggregate_pr_review(
    findings: Iterable[PRFinding],
    *,
    n_reviewers: int | None = None,
    min_reviewers: int | None = None,
    top_k: int = DEFAULT_TOP_K,
    line_window: int = DEFAULT_LINE_WINDOW,
    token_overlap: float = DEFAULT_TOKEN_OVERLAP,
) -> PRReviewReport:
    """End-to-end: cluster → vote → rank → group, returning a report.

    Args:
        findings: Already-parsed findings from
            :func:`parse_findings_from_pipeline` or constructed manually.
        n_reviewers: Total distinct reviewer roles in the run.  When
            ``None``, derived from *findings* - pass it explicitly when
            some reviewers approved (and therefore contributed zero
            findings), otherwise the threshold will under-count.
        min_reviewers: Override the ``⌈N/2⌉`` quorum.  ``None`` keeps
            the spec default.
        top_k: Cap on the surfaced cluster count.  ``≤0`` disables.
        line_window: Tolerance for clustering by line proximity.
        token_overlap: Jaccard threshold for clustering by message
            similarity.

    Returns:
        :class:`PRReviewReport` ready for display / posting.
    """
    findings_list = list(findings)
    derived_n = n_reviewers if n_reviewers is not None else len({f.source_role for f in findings_list})
    derived_n = max(derived_n, 1)

    clusters = cluster_findings(
        findings_list,
        line_window=line_window,
        token_overlap=token_overlap,
    )
    survivors = vote_clusters(
        clusters,
        n_reviewers=derived_n,
        min_reviewers=min_reviewers,
    )
    ranked = rank_clusters(survivors, n_reviewers=derived_n, top_k=top_k)
    by_file = group_by_file(ranked)

    return PRReviewReport(
        clusters=tuple(ranked),
        by_file=by_file,
        total_input=len(findings_list),
        total_clusters=len(clusters),
        n_reviewers=derived_n,
    )


def aggregate_from_pipeline(
    verdict: PipelineVerdict,
    *,
    min_reviewers: int | None = None,
    top_k: int = DEFAULT_TOP_K,
) -> PRReviewReport:
    """Convenience wrapper: parse a :class:`PipelineVerdict` then aggregate.

    Counts every distinct (stage, role) pair as a reviewer so two
    stages of the same role run twice and contribute independent votes.
    """
    findings = parse_findings_from_pipeline(verdict)
    n_reviewers = len({(stage.stage, agent.role) for stage in verdict.stages for agent in stage.agents})
    return aggregate_pr_review(
        findings,
        n_reviewers=max(n_reviewers, 1),
        min_reviewers=min_reviewers,
        top_k=top_k,
    )


def render_report_markdown(report: PRReviewReport) -> str:
    """Render *report* as markdown for stdout / PR comment posting.

    Intentionally simple - emoji-free, GitHub-Flavoured-Markdown safe,
    deterministic line ordering.  Downstream code (e.g. a future
    ``post_grouped_review``) wraps this in a PR-review API call.

    Each surfaced cluster now carries a detected-by provenance tag
    (``[detected by N/M bots, agreement Y%]``) sourced from
    :mod:`bernstein.core.quality.review_consensus` so the PR review
    summary comment shows cross-reviewer agreement at a glance.
    """
    lines: list[str] = [
        "# PR review summary",
        "",
        (
            f"Reviewed by {report.n_reviewers} reviewer(s); "
            f"{report.total_input} raw finding(s) -> "
            f"{report.total_clusters} cluster(s) -> "
            f"{len(report.clusters)} surfaced after vote + rank."
        ),
        "",
    ]
    if not report.clusters:
        lines.append("_No findings reached the voting threshold._")
        return "\n".join(lines)

    # Stable file order: scored clusters keep their rank position so the
    # by-file traversal is well-defined.
    seen_files = list(dict.fromkeys(cluster.file for cluster in report.clusters))

    for file in seen_files:
        header = file or "_(unattributed)_"
        lines.extend(["", f"## {header}", ""])
        for cluster in report.by_file[file]:
            location = f":{cluster.line}" if cluster.line is not None else ""
            provenance = _cluster_provenance(cluster, n_reviewers=report.n_reviewers)
            lines.append(
                f"- **[{cluster.severity}]** {cluster.canonical_message} "
                f"_(score={cluster.score:.2f}, reviewers={cluster.reviewer_count}"
                f"{', file' + location if location else ''})_ "
                f"{provenance}"
            )
    return "\n".join(lines)


def _cluster_provenance(cluster: FindingCluster, *, n_reviewers: int) -> str:
    """Render the detected-by provenance tag for one cluster.

    Bridges the aggregator's :class:`FindingCluster` (which counts
    distinct reviewer *roles*) to the consensus engine's provenance line
    so the sticky comment renders one consistent format.
    """
    from bernstein.core.quality.review_consensus import (
        ConsensusFinding,
        ConsensusLevel,
        render_provenance,
    )

    detected_by = tuple(sorted(cluster.reviewer_roles))
    denom = max(n_reviewers, 1)
    agreement = min(len(detected_by) / denom, 1.0)
    proxy = ConsensusFinding(
        file=cluster.file,
        line=cluster.line,
        category="",
        title=cluster.canonical_message,
        severity=cluster.severity,
        detected_by=detected_by,
        bots_ran=denom,
        agreement_ratio=agreement,
        max_confidence=1.0,
        consensus_score=agreement,
        level=ConsensusLevel.UNVERIFIED,
        members=(),
    )
    return render_provenance(proxy)


__all__ = [
    "DEFAULT_LINE_WINDOW",
    "DEFAULT_TOKEN_OVERLAP",
    "DEFAULT_TOP_K",
    "FindingCluster",
    "PRFinding",
    "PRReviewReport",
    "Severity",
    "aggregate_from_pipeline",
    "aggregate_pr_review",
    "cluster_findings",
    "group_by_file",
    "parse_finding",
    "parse_findings_from_agent",
    "parse_findings_from_pipeline",
    "rank_clusters",
    "render_report_markdown",
    "score_cluster",
    "vote_clusters",
]
