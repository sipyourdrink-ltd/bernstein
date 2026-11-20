"""Scheduled upstream-signal ingestion for the orchestrator backlog.

This module powers the ``bernstein trend-scan run`` CLI and the optional
``trend-scan.yml`` GitHub Actions workflow. It ingests upstream
dependency-relevant signals (release notes, advisories, RFC threads) from a
configurable set of sources, runs a deterministic keyword + relevance filter,
gap-checks each candidate against open / recently-closed issues and the
``.sdd/backlog/`` directory, and writes a markdown rollup for operator
review.

Design constraints:

* No auto-filing. The output is a rollup file; the operator decides which
  candidates become tickets via the existing ``bernstein backlog new`` flow.
* No network calls inside this module. Source fetchers are injected as
  callables so tests can stub them and so the CLI can wire whatever fetch
  primitive the operator prefers (``WebFetch``, ``urllib``, a cached HTTP
  client, etc.).
* Deterministic ordering. All sort keys are total: ``(score desc, source,
  url)``. This keeps the rollup diffable week-over-week.
* No competitor namedrops in source labels or filters - sources are tiered
  by signal stability (Tier 1 = upstream release feeds, Tier 2 = advisory
  feeds), not by any commercial classification.

Public surface:

* :class:`Candidate` - normalised result row.
* :class:`SourceSpec` - per-source filter config.
* :class:`TrendScanConfig` - top-level configuration.
* :func:`run_scan` - orchestrator entry point used by the CLI.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

__all__ = [
    "Candidate",
    "GapStatus",
    "RawItem",
    "SourceSpec",
    "TrendScanConfig",
    "TrendScanResult",
    "build_rollup_markdown",
    "classify_gap",
    "filter_items",
    "load_backlog_keywords",
    "load_default_sources",
    "run_scan",
    "score_item",
]


Tier = Literal[1, 2, 3]
GapStatus = Literal["new", "duplicate", "recently-closed"]


_WORD_RE = re.compile(r"[A-Za-z][\w+-]+", re.ASCII)
_DOTTED_VERSION_RE = re.compile(r"(?<![\w.])\d+(?:\.\d+)+(?!\w|\.\d)", re.ASCII)


def _tokens(text: str) -> list[str]:
    """Lowercased word tokens. Cheap, deterministic, no external deps."""

    return [match.group(0).lower() for match in _WORD_RE.finditer(text)]


@dataclass(frozen=True)
class RawItem:
    """One un-filtered item produced by a source fetcher."""

    title: str
    url: str
    ts: str  # ISO-8601, ``YYYY-MM-DDTHH:MM:SSZ``
    body: str = ""

    def fingerprint(self) -> str:
        """Stable id for dedup across runs and across sources.

        Hashes the URL when present (URLs are canonical), falls back to a
        normalised title hash for sources that only expose a title.
        """

        key = (self.url or self.title).strip().lower()
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


@dataclass(frozen=True)
class SourceSpec:
    """Configuration for one source feed.

    ``keywords`` is the inclusion list - at least one must match the title or
    body for the item to be considered. ``boost_keywords`` adds to the
    relevance score but is not required for inclusion. ``negative_keywords``
    drops the item outright.
    """

    name: str
    tier: Tier
    keywords: tuple[str, ...]
    boost_keywords: tuple[str, ...] = ()
    negative_keywords: tuple[str, ...] = ()
    min_score: float = 0.5


@dataclass(frozen=True)
class Candidate:
    """One filtered, scored candidate ready for the rollup."""

    source: str
    tier: Tier
    title: str
    url: str
    ts: str
    score: float
    matched_keywords: tuple[str, ...]
    gap_status: GapStatus = "new"
    related_refs: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        data = asdict(self)
        data["matched_keywords"] = list(self.matched_keywords)
        data["related_refs"] = list(self.related_refs)
        return data


@dataclass(frozen=True)
class TrendScanConfig:
    """Top-level configuration for one scan run."""

    sources: tuple[SourceSpec, ...]
    backlog_dir: Path
    closed_lookback_days: int = 60
    rollup_dir: Path = field(default_factory=lambda: Path(".sdd/trend-scan"))
    max_candidates_per_source: int = 5


@dataclass(frozen=True)
class TrendScanResult:
    """What :func:`run_scan` returns."""

    candidates: tuple[Candidate, ...]
    rollup_path: Path
    generated_at: str


# ---------------------------------------------------------------------------
# Filtering + scoring
# ---------------------------------------------------------------------------


def score_item(item: RawItem, spec: SourceSpec) -> tuple[float, tuple[str, ...]]:
    """Return (score, matched_keywords).

    Scoring is intentionally simple and deterministic:

    * +1.0 per distinct ``keywords`` hit
    * +0.5 per distinct ``boost_keywords`` hit
    * normalised by ``log2(1 + |title| + |body|)`` so very long pages don't
      drown the signal
    """

    text = f"{item.title}\n{item.body}"
    text_tokens = set(_tokens(text))
    text_tokens.update(_DOTTED_VERSION_RE.findall(text))
    if not text_tokens:
        return 0.0, ()

    neg = {kw.lower() for kw in spec.negative_keywords}
    if neg & text_tokens:
        return 0.0, ()

    raw_hits = sorted(text_tokens & {kw.lower() for kw in spec.keywords})
    if not raw_hits:
        return 0.0, ()

    boost_hits = sorted(text_tokens & {kw.lower() for kw in spec.boost_keywords})
    raw_score = float(len(raw_hits)) + 0.5 * float(len(boost_hits))
    length_norm = math.log2(1.0 + len(text_tokens))
    score = raw_score / max(length_norm, 1.0)

    return round(score, 4), tuple(raw_hits + boost_hits)


def filter_items(items: Iterable[RawItem], spec: SourceSpec) -> list[tuple[RawItem, float, tuple[str, ...]]]:
    """Apply the spec's keyword filter and score the survivors."""

    scored: list[tuple[RawItem, float, tuple[str, ...]]] = []
    seen_fingerprints: set[str] = set()
    for item in items:
        fp = item.fingerprint()
        if fp in seen_fingerprints:
            continue
        seen_fingerprints.add(fp)
        score, matched = score_item(item, spec)
        if score < spec.min_score:
            continue
        scored.append((item, score, matched))

    scored.sort(key=lambda row: (-row[1], (row[0].url or row[0].title).lower()))
    return scored


# ---------------------------------------------------------------------------
# Gap analysis
# ---------------------------------------------------------------------------


def load_backlog_keywords(backlog_dir: Path) -> dict[str, set[str]]:
    """Return ``{relative_path: token_set}`` for every backlog markdown file.

    Used by :func:`classify_gap` to detect ``duplicate`` candidates against
    existing open / claimed tickets without requiring a network call.
    """

    tokens_by_path: dict[str, set[str]] = {}
    if not backlog_dir.exists():
        return tokens_by_path

    for path in sorted(backlog_dir.rglob("*.md")):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        rel = str(path.relative_to(backlog_dir))
        tokens_by_path[rel] = set(_tokens(text))

    return tokens_by_path


def classify_gap(
    candidate_tokens: set[str],
    backlog_tokens: dict[str, set[str]],
    closed_issue_keywords: Sequence[tuple[str, set[str]]],
    *,
    overlap_threshold: int = 4,
) -> tuple[GapStatus, tuple[str, ...]]:
    """Classify a candidate as ``new`` / ``duplicate`` / ``recently-closed``.

    The classifier picks the strongest signal:

    * Duplicate against backlog: token overlap >= ``overlap_threshold`` with
      any backlog file in ``open/`` or ``claimed/``.
    * Recently-closed: overlap against ``closed_issue_keywords``.
    * Otherwise ``new``.

    Returns the status plus a tuple of related refs (file paths or issue
    keys) sorted by descending overlap. Capped at 5 refs so the rollup stays
    scannable.
    """

    if not candidate_tokens:
        return "new", ()

    backlog_hits: list[tuple[int, int, str]] = []
    for path, tokens in backlog_tokens.items():
        overlap = len(candidate_tokens & tokens)
        if overlap >= overlap_threshold:
            # Active tickets (``open/`` / ``claimed/``) sort ahead of
            # ``closed/`` so the operator lands on the live ticket first
            # when both match.
            active_priority = 0 if path.startswith(("open/", "claimed/")) else 1
            backlog_hits.append((active_priority, -overlap, path))

    closed_hits: list[tuple[int, str]] = []
    for ref, tokens in closed_issue_keywords:
        overlap = len(candidate_tokens & tokens)
        if overlap >= overlap_threshold:
            closed_hits.append((overlap, ref))

    if backlog_hits:
        backlog_hits.sort()
        refs = tuple(path for _, _, path in backlog_hits[:5])
        if any(ref.startswith(("open/", "claimed/")) for ref in refs):
            return "duplicate", refs
        # Backlog hit but only inside closed/ - treat as recently-closed.
        return "recently-closed", refs

    if closed_hits:
        closed_hits.sort(key=lambda row: (-row[0], row[1]))
        return "recently-closed", tuple(ref for _, ref in closed_hits[:5])

    return "new", ()


# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------


def load_default_sources() -> tuple[SourceSpec, ...]:
    """A conservative default set of upstream sources.

    Sources are framed by signal type (runtime, packaging, advisories), not
    by any commercial classification. The operator can override this set via
    a JSON config file passed to the CLI.
    """

    return (
        SourceSpec(
            name="python-release-notes",
            tier=1,
            keywords=("python", "cpython", "release", "security"),
            boost_keywords=("3.13", "3.14", "deprecation", "removal"),
        ),
        SourceSpec(
            name="pip-release-notes",
            tier=1,
            keywords=("pip", "pypa", "resolver", "dependency"),
            boost_keywords=("breaking", "deprecation"),
        ),
        SourceSpec(
            name="pypi-advisories",
            tier=2,
            keywords=("vulnerability", "advisory", "cve", "supply", "chain"),
            boost_keywords=("python", "pypi", "wheel"),
        ),
        SourceSpec(
            name="github-actions-changelog",
            tier=2,
            keywords=("actions", "runner", "deprecation", "security"),
            boost_keywords=("workflow", "permissions", "oidc"),
        ),
    )


# ---------------------------------------------------------------------------
# Rollup writer
# ---------------------------------------------------------------------------


def build_rollup_markdown(
    candidates: Sequence[Candidate],
    *,
    generated_at: str,
    sources: Sequence[SourceSpec],
) -> str:
    """Format the candidate list as the rollup markdown.

    The format is intentionally diff-friendly: a single H1, a short
    preamble, one table grouped by tier, and a per-tier footer with totals.
    """

    lines: list[str] = []
    lines.extend(
        (
            "# Trend scan rollup",
            "",
            f"_Generated: {generated_at}_",
            "",
            "Scheduled job that ingests upstream dependency-relevant signals into the "
            "orchestrator state. No tickets are filed automatically - the operator "
            "reviews this rollup and runs `bernstein backlog new` for the rows that "
            "warrant a ticket.",
            "",
            f"Sources scanned: {len(sources)}. Candidates surfaced: {len(candidates)}.",
            "",
        )
    )

    if not candidates:
        lines.extend(("No candidates passed the per-source filters this run.", ""))
        return "\n".join(lines)

    by_tier: dict[int, list[Candidate]] = {}
    for cand in candidates:
        by_tier.setdefault(cand.tier, []).append(cand)

    for tier in sorted(by_tier):
        lines.extend(
            (
                f"## Tier {tier}",
                "",
                "| Source | Title | Status | Score | Matched | Refs |",
                "| --- | --- | --- | --- | --- | --- |",
            )
        )
        for cand in by_tier[tier]:
            title_cell = f"[{_escape(cand.title)}]({cand.url})" if cand.url else _escape(cand.title)
            matched_cell = ", ".join(cand.matched_keywords) or "-"
            refs_cell = ", ".join(cand.related_refs) or "-"
            lines.append(
                f"| {_escape(cand.source)} | {title_cell} | {cand.gap_status} | "
                f"{cand.score:.2f} | {_escape(matched_cell)} | {_escape(refs_cell)} |"
            )
        lines.append("")

    lines.extend(
        (
            "---",
            "",
            "_Operator action: review the `new` rows; ignore `duplicate` and"
            " `recently-closed` unless context has changed._",
            "",
        )
    )
    return "\n".join(lines)


def _escape(value: str) -> str:
    """Escape pipe and backslash for markdown-table-safe cells."""

    return value.replace("\\", "\\\\").replace("|", "\\|")


# ---------------------------------------------------------------------------
# Orchestrator entry point
# ---------------------------------------------------------------------------


SourceFetcher = Callable[[SourceSpec], Iterable[RawItem]]
ClosedIssueLoader = Callable[[int], Sequence[tuple[str, set[str]]]]


def _now_iso() -> str:
    return datetime.now(tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_scan(
    config: TrendScanConfig,
    *,
    fetcher: SourceFetcher,
    closed_issue_loader: ClosedIssueLoader | None = None,
    now_iso: Callable[[], str] = _now_iso,
    output_path: Path | None = None,
) -> TrendScanResult:
    """Run one scan pass and write the rollup.

    Parameters
    ----------
    config:
        Top-level configuration. ``config.sources`` selects which feeds
        participate in this run.
    fetcher:
        Injected callable that returns an iterable of :class:`RawItem` for a
        given source. The CLI wires this to whatever HTTP primitive the
        operator prefers; tests pass a deterministic stub.
    closed_issue_loader:
        Optional callable that returns ``[(ref, token_set), ...]`` for
        recently-closed issues. Defaults to an empty list (purely
        backlog-based gap analysis).
    now_iso:
        Override for the timestamp source. Used by tests to keep the rollup
        deterministic.
    output_path:
        Override the rollup destination. Defaults to
        ``config.rollup_dir / 'rollup-<YYYY-MM-DD>.md'``.
    """

    generated_at = now_iso()
    closed_keywords = list((closed_issue_loader or (lambda _: []))(config.closed_lookback_days))
    backlog_tokens = load_backlog_keywords(config.backlog_dir)

    all_candidates: list[Candidate] = []
    for spec in config.sources:
        scored = filter_items(fetcher(spec), spec)
        per_source = scored[: config.max_candidates_per_source]
        for item, score, matched in per_source:
            cand_tokens = set(_tokens(f"{item.title}\n{item.body}"))
            gap_status, refs = classify_gap(cand_tokens, backlog_tokens, closed_keywords)
            all_candidates.append(
                Candidate(
                    source=spec.name,
                    tier=spec.tier,
                    title=item.title,
                    url=item.url,
                    ts=item.ts,
                    score=score,
                    matched_keywords=matched,
                    gap_status=gap_status,
                    related_refs=refs,
                )
            )

    all_candidates.sort(key=lambda c: (c.tier, -c.score, c.source, c.url))

    rollup_md = build_rollup_markdown(
        all_candidates,
        generated_at=generated_at,
        sources=config.sources,
    )

    if output_path is None:
        day = generated_at[:10]
        output_path = config.rollup_dir / f"rollup-{day}.md"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(rollup_md, encoding="utf-8")

    # Also write a sibling JSON file so downstream tools can diff
    # structurally instead of via markdown.
    json_path = output_path.with_suffix(".json")
    json_path.write_text(
        json.dumps(
            {
                "generated_at": generated_at,
                "candidates": [c.to_dict() for c in all_candidates],
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )

    return TrendScanResult(
        candidates=tuple(all_candidates),
        rollup_path=output_path,
        generated_at=generated_at,
    )
