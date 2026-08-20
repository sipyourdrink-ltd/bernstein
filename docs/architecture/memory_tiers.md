# Tiered Context Compaction

**How does Bernstein decide when to compact context, and how much to spend
doing it?**

A single compaction strategy has to compromise. Compact too often and you
lose recall on small turns; wait until the window is full and you compact
everything at once, losing fidelity. Bernstein replaces the single call site
with a tiered strategy: a policy picks exactly one tier per call based on
budget pressure, applying the cheap tier on most turns and reserving the
expensive cross-session tier for the points where it actually pays back.

Source: `src/bernstein/core/memory/compaction/`.

---

## The tiers

Each tier is a module with a documented trigger predicate, a cost
annotation, and a `compact(ctx)` function. Cheapest first:

| Tier | Module | Trigger | Cost weight | Reduction |
|---|---|---|---|---|
| `micro` | `micro.py` | turn >= 2, mild context use | 0.05 | Collapse long tool-call result bodies (structural, no LLM) |
| `time_based` | `time_based.py` | live session, idle >= 300 s | 0.10 | Drop blocks tagged `[age:N]` older than the cutoff |
| `auto` | `auto.py` | live session, context use >= 70 percent | 0.50 | Strip media and summarise tool runs (LLM-backed when supplied) |
| `session_memory` | `session_memory.py` | session complete | 1.00 | Build a durable cross-session summary; keep summary only |
| `none` | (policy) | no trigger fires | 0.00 | No-op; context unchanged |

The cost weight is a multiplier on the per-token rate. A structural prune
that issues no model call costs a small fraction of a tier that summarises
through an LLM. The weights live in `TIER_COST_WEIGHT`
(`src/bernstein/core/memory/compaction/tiers.py`).

---

## The policy selector

`select_tier(pressure)` in `policy.py` inspects a `BudgetPressure` bundle
and returns one tier. Selection is priority ordered so the tier whose
trigger best matches the current pressure wins:

1. `session_memory` - `session_complete` is set.
2. `auto` - live session past the context threshold.
3. `time_based` - live session idle past the threshold.
4. `micro` - mild pressure on an active session.
5. `none` - nothing fires.

`BudgetPressure` carries four inputs:

| Field | Meaning |
|---|---|
| `turn_count` | 1-based turn number for the session |
| `context_pct_used` | fraction of the context window consumed, `[0.0, 1.0]` |
| `idle_seconds` | seconds since the last turn |
| `session_complete` | whether the session has finished |

`compact(ctx)` is the convenience entrypoint: it selects a tier from
`ctx.pressure` and runs it, returning a `TierResult`. When no tier fires it
returns a `Tier.NONE` result that leaves the context unchanged and
attributes zero cost (the no-pressure no-op).

---

## Cost attribution

Every `TierResult` carries a `cost_estimate` in USD, computed as:

```
cost_estimate = (tokens_saved / 1000) * cost_per_1k_tokens * COST_WEIGHT
```

This attributes compaction spend back to the tier so operators can audit
"how much do I spend on memory upkeep" and see which tier drives it. A tier
that saves nothing costs nothing.

---

## Trace recording and content hashing

Every `TierResult` anchors the summarized region with content hashes:

- `source_content_hash` - SHA-256 content hash of the exact pre-compaction UTF-8
  bytes, computed before the tier runs.
- `referenced_content_hashes` - mapping of referenced artifact paths to content
  hashes captured at compaction time, explicitly recording `"absent"` for files
  that did not exist on disk.

`record_tier_event(trace, result)` records a compaction event in the
existing trace store. It builds a `compact` `TraceStep` (see
`src/bernstein/core/observability/traces.py`) carrying:

- `tier` - the tier name, in the step detail.
- `before_tokens` / `after_tokens` - via the namespaced
  `compaction_tokens_before` / `compaction_tokens_after` fields.
- `cost_estimate` - in the step detail.
- `correlation_id` - via `compaction_correlation_id`.
- `compaction_source_content_hash` - content hash of the pre-compaction region.
- `compaction_referenced_content_hashes` - map of referenced artifact paths to
  hashes captured at compaction time.

The event is auditable from a plain JSONL tail without a special reader.

---

## Verification

A verification helper `verify_compacted_step(step)` (and
`verify_compaction_references(references)`) checks whether referenced artifacts
still match what they hashed to when the compaction occurred. Mismatches are
reported as explicit `ArtifactDivergence` records rather than silent drifts,
detecting post-compaction modifications, deletions, or unexpected creations.

---

## Legacy entrypoint

The original single-strategy call site is preserved as
`run_legacy_compaction(...)`, a thin shim that defers to the policy with
full-context pressure (which selects the `auto` tier). New code should call
`compact(ctx)` with an explicit `TierContext`.

---

## Out of scope

- Embedding-based retrieval is orthogonal to compaction.
- The existing session-memory store
  (`src/bernstein/core/memory/session_memory.py`) is unchanged; the
  `session_memory` tier produces a summary, it does not replace the store.
