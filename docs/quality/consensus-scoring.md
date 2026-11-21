# Consensus scoring with detected-by provenance

When several review bots run on the same diff (CodeRabbit, Sourcery,
GitHub Advanced Security, ...), reporting their findings as a flat union
hides signal: a finding raised by one weak bot reads the same as one
three bots raised independently. This module lifts consensus into the
finding shape itself so operators can see which bots agreed, what
fraction of the active bots raised the issue, and which confidence band
the finding lands in.

## Module

`src/bernstein/core/quality/review_consensus.py`

Public API:

- `compute_consensus(findings, *, bots_ran=None, line_window=3, title_overlap=0.5) -> list[ConsensusFinding]`
- `must_address(consensus, *, min_level=ConsensusLevel.CONFIRMED) -> list[ConsensusFinding]`
- `bucket_for_score(score) -> ConsensusLevel`
- `render_provenance(finding) -> str`
- `render_consensus_markdown(consensus) -> str`

## Input shape

Each review bot adapter normalises its raw output into a
`NormalizedFinding` before handing it to the engine:

```python
@dataclass(frozen=True)
class Evidence:
    file: str = ""
    line: int | None = None
    snippet: str = ""
    symbol: str = ""


@dataclass(frozen=True)
class NormalizedFinding:
    bot: str  # e.g. "coderabbit", "sourcery", "gh-advanced-security"
    finding_id: str  # bot-local id, audit only (not part of dedup key)
    severity: Severity  # info | low | medium | high | critical
    category: str  # coarse class: security | perf | style | ...
    title: str  # short title, used for fuzzy dedup + display
    evidence: Evidence = field(default_factory=Evidence)
    confidence: float = 1.0  # bot self-reported, [0.0, 1.0]
```

## Dedup rule

Two findings describe the same issue when they share:

1. The same `file` (or both global) **and** the same `category`. This is
   a hard prerequisite.
2. Then, when both findings carry a concrete `line`, those lines must lie
   within `line_window` (default 3). Distant lines are distinct loci even
   when titles look alike (an "unused variable" at line 10 and line 200
   are two findings).
3. When at least one line is absent, the titles must fuzzy-match: token
   Jaccard at or above `title_overlap` (default 0.5), after lowercasing
   and dropping stopwords / short tokens.

Clustering is a deterministic greedy single pass anchored on the first
finding in each group.

## Scoring

For each merged finding:

```
detected_by      = sorted distinct bots that raised it
agreement_ratio  = min(len(detected_by) / max(bots_ran, 1), 1.0)
max_confidence   = max(clamp(member confidence, 0.0, 1.0))
consensus_score  = agreement_ratio * max_confidence
```

The default `bots_ran` denominator is the number of distinct bots found
across the complete input, not just the bots in that finding's cluster.
Each cluster's `detected_by` likewise counts each reporting bot only once,
regardless of how many matching reports that bot supplied. Pass
`bots_ran` explicitly when the run included bots with no findings; an
explicit denominator is floored at one. The agreement ratio is capped at
one, and every member confidence is clamped to `[0.0, 1.0]` before taking
the maximum.

For example, suppose bot A submits two reports for the same issue at the
same location, each with confidence `0.8`, while bot B reports a different
issue. For the first issue, `detected_by` contains only A, so the duplicate
reports count once. With the default denominator of two distinct bots,
its agreement is `1/2` and its score is `0.5 * 0.8 = 0.4`. If the run
actually had four bots, two silent, passing `bots_ran=4` changes the
agreement to `1/4` and the score to `0.25 * 0.8 = 0.2`.

## Buckets

| Bucket | Threshold | Gate behaviour |
| --- | --- | --- |
| `confirmed` | `consensus_score >= 0.66` | must-address; can block a merge |
| `needs-verification` | `consensus_score >= 0.33` | warning |
| `unverified` | otherwise | informational only |

A CI gate requires `consensus_level >= confirmed` for blocking issues via
`must_address(consensus)`; lower the bar with the `min_level` argument.

## Provenance rendering

`render_provenance` produces the provenance tag appended to each
finding:

```
[detected by 2/4 bots, agreement 50%]
```

`render_consensus_markdown` groups findings by bucket and renders each
with its severity, title, locus, provenance tag, and the bot names that
detected it. The finding-level aggregator
(`src/bernstein/core/quality/pr_review_aggregator.py`) reuses the same
provenance tag in `render_report_markdown`, so the sticky comment shows
one consistent format whether findings flow through the cluster
aggregator or the consensus engine.

## Out of scope

- Bot weighting beyond max-confidence.
- Cross-task consensus aggregation.
