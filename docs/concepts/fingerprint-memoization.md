# Fingerprint memoization

Bernstein recomputes several expensive things on every tick: cross-model
verification, knowledge-graph extraction, RAG re-embedding. Each call
site historically invented its own cache key, and most of those keys
hashed only the *inputs* - never the *function body*. A bug fix that
should re-derive the cache silently kept serving stale entries.

The `fingerprint` module replaces the ad-hoc keys with a single
content-addressed store keyed by `hash(canonicalised_input)
xor hash(function_AST)`. Change the function body, the key changes.

## Why it exists

Two failure modes drove this:

1. **Stale cache after refactor** - fix a bug, re-run, get the buggy
   result back because the cache key didn't include the source.
2. **Per-site key duplication** - cross-model verifier, knowledge
   graph, and RAG each rolled their own; they all needed the same
   invariant and none had it consistently.

## How to use it

Decorate any deterministic function whose result is expensive to
recompute and depends only on its arguments:

```python
from pathlib import Path
from bernstein.core.persistence.fingerprint import (
    default_store, memoize_persistent,
)

store = default_store(Path("."))  # rooted at .sdd/runtime/memo

@memoize_persistent(store, site="embed")
def embed_chunk(chunk_text: str, embedder_id: str) -> list[float]:
    # expensive embedding call
    ...
```

On second invocation with the same arguments the call is served from
`.sdd/runtime/memo/<sha>/` without re-execution. Edit the function
body, redeploy, the next call misses cache and re-derives.

The decorator is already applied at three call sites:

| Site | Key |
|---|---|
| `quality/cross_model_verifier.py` | `(model_id, prompt, output, verifier_fn_hash)` |
| `knowledge/knowledge_graph.py` | `(file_sha, extractor_fn_hash)` |
| `knowledge/rag.py` | `(chunk_sha, embedder_id, chunker_fn_hash)` |

## Configuration

| Knob | Default | Controls |
|---|--:|---|
| `defaults.MEMO_MAX_MB` | `200` | Max disk used by the store before LRU eviction kicks in. |
| Memo store path | `.sdd/runtime/memo/` | Pinned to `.sdd/` so air-gap runs do not write to `~/.cache/`. |

Metrics exposed on `/metrics`:

- `bernstein_memo_hits_total{site}`
- `bernstein_memo_misses_total{site}`
- `bernstein_memo_size_bytes`

## Limitations

- Single host. The store is on-disk and not shared across machines.
- The fingerprint hashes the *immediate* function body only - not the
  transitive closure of called helpers. If you rely on a helper that
  changed, decorate that helper too, or invalidate manually.
- Functions with hidden state (env vars read at call time, file IO,
  network calls) are unsafe to memoize. Restrict use to pure
  functions.
- The decorator does not replace `semantic_cache.py` - that's a
  vector cache for semantic-similarity lookup, a different concern.

## Cache policy engine

The `MemoStore` here is the single backing store the [cache policy
engine](cache-policy.md) composes over. A task that declares a `cache_policy`
gets a key composed from an ordered ingredient recipe (mandatory model version,
adapter version, base worktree commit, and tool schema hash, plus declared
optional ingredients), a pure drift-based freshness verdict over repo state, and
transitive eviction over `served_from` lineage edges - all on top of this same
content-addressed store.

## Related

- Source: `src/bernstein/core/persistence/fingerprint.py`
- Policy engine: `src/bernstein/core/persistence/cache_policy.py`
- PR #995
