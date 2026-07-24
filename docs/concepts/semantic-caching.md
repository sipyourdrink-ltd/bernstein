# Semantic caching

Bernstein caches two different kinds of LLM work by matching on the
*meaning* of a request, not just its exact text: repeated planning calls for
similar goals, and completed task results for functionally identical tasks.
Both use the same underlying cosine-similarity matcher
(`core/knowledge/semantic_cache.py`) but store different things, at
different thresholds, for different purposes. This is a different caching
layer from the [cache policy engine](cache-policy.md), which governs the
fingerprint, action-cache, and semantic-cache boundaries as a shared
freshness/eviction contract — the mechanics described here are specific to
the semantic matcher itself.

## Two caches, one matcher

| | `SemanticCacheManager` | `ResponseCacheManager` |
|---|---|---|
| Caches | LLM **planning** responses | Completed **task results** (`result_summary`) |
| Keyed on | Goal text | `role:title\ndescription` (task-shape) |
| Similarity threshold | 0.85 | 0.95 (deliberately high — wrong reuse here skips spawning an agent) |
| TTL | 24h | 7 days |
| Max entries | 500 | 1,000 |
| Storage | `.sdd/caching/semantic_cache.jsonl` | `.sdd/caching/response_cache.jsonl` |
| Model-sensitive | Yes — entries are scoped per `model` | No — stores *what was accomplished*, not a model's wording |

Both do an exact SHA-256 match on normalized text first (O(1)); on a miss,
they fall back to a TF-style word-frequency cosine similarity scan (O(n))
over non-expired entries and accept the best match once it clears the
threshold.

## How lookups work

```python
response, similarity = manager.lookup(key_text, model="claude-sonnet-4")
# similarity == 1.0 on an exact hash hit; < threshold means a miss
```

A hit increments the entry's `hit_count` and refreshes `last_used_at`.
Entries older than the TTL are treated as misses and pruned opportunistically.
When the store exceeds its entry cap, the least-recently-used entries are
evicted to make room.

The response cache additionally exposes a canonical key builder,
`ResponseCacheManager.task_key(role, title, description)`, so callers build
the same key shape the orchestrator uses when deciding whether a new task is
"the same" as a previously completed one.

## Where it's wired in

- `core/planning/planner.py` consults `SemanticCacheManager` before issuing a
  planning LLM call for a goal.
- `core/orchestration/orchestrator.py` and `core/tasks/task_lifecycle.py`
  consult `ResponseCacheManager` before spawning an agent for a task, and
  store the result afterward.
- `adapters/caching_adapter.py` wraps a CLI adapter with the response cache
  so adapter-level callers get the same reuse without touching orchestrator
  internals.

## Inspecting the response cache

```
bernstein cache list --limit 25 [--json]
bernstein cache inspect <task-id> [--json]
bernstein cache clear [--unverified] [--yes]
```

`list` and `inspect` read `.sdd/caching/response_cache.jsonl` and show hit
count, verification status, tracked diff-line count, and age per entry.
`clear` removes entries outright (`--unverified` restricts this to entries
that never came from a verified real execution). There is no equivalent CLI
surface for the planning-side `SemanticCacheManager` — it is inspected only
through its `get_stats()` Python API.

`bernstein cache policy` and `bernstein cache evict` operate on the separate
cache-policy engine, not on this matcher — see
[Cache policy engine](cache-policy.md).

## Limitations

- The fuzzy matcher is a hand-rolled TF word-frequency cosine similarity, not
  an embedding model — it catches near-identical phrasing, not semantically
  equivalent but differently worded requests.
- `verified` entries and `unverified` entries are not distinguished during
  lookup — an unverified (never-executed-for-real) response can still serve
  a cache hit unless it has been cleared with `--unverified`.
- A `store()` call refreshes an existing entry's response in place; there is
  no versioned history of what a given cache key has served over time.

## Source

- `src/bernstein/core/knowledge/semantic_cache.py` — `SemanticCacheManager`, `ResponseCacheManager`, `SemanticCacheEntry`.
- `src/bernstein/cli/commands/cache_cmd.py` — `bernstein cache list/inspect/clear`.
