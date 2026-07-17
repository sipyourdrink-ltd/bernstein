# Cache policy engine

Bernstein carries three result caches - fingerprint memoization
(`core/persistence/fingerprint.py`), the action cache
(`core/persistence/action_cache.py`), and the semantic response cache
(`core/knowledge/semantic_cache.py`). Each historically hard-coded its own key
recipe with no shared vocabulary, and none captured the model version, adapter
version, base worktree commit, or tool schema hash. The only staleness signal
was the wall clock. A cached agent output could therefore survive a model
upgrade, an adapter bump, a tool-schema change, or a diff that touched files
which have since changed - and a wrong hit injects a stale artifact into a run
that then completes with a proof-of-done receipt built on bad input.

The **cache policy engine** defines what a cache decision *means* at the cache
boundary: which changes count as relevant (keys), when an entry is stale
(expiry), how two fleet workers avoid duplicating expensive work (dedup), and
how a bad value is revoked together with everything derived from it (eviction).
Its user-visible artifacts are lineage records, signed receipts, and
audit-chain events rather than ad hoc store rows.

A task opts in by declaring a `cache_policy` on its spec. A task that declares
none runs byte-identically to the pre-policy spawn path.

## Composable key recipes

A `CachePolicy` (`core/persistence/cache_policy.py`) names an ordered set of
optional key ingredients on top of five **mandatory** ingredients that are
always folded in:

| Mandatory ingredient | Source |
| --- | --- |
| `model_id` | adapter contract |
| `model_version` | adapter contract |
| `adapter_version` | adapter contract |
| `base_commit` | `git_basic.rev_parse_head` |
| `tool_schema_hash` | the task's declared tool surface |

The optional ingredients are `task_inputs`, `producer_code`, and
`run_parameters`. `compose_recipe` lists the mandatory ingredients first, then
the policy's declared optional ones, hashing each value to a `sha256:` anchor;
`compose_key` is the `sha256` of the canonical recipe. Any change to a mandatory
ingredient - or a declared optional one - alters the recipe and therefore the
key, so a cached output never survives a model, adapter, repo-base, or
tool-surface change the policy accounts for. The policy hash is folded into the
recipe, so two policies over identical inputs still produce distinct keys.

```json
{
  "ingredients": ["task_inputs"],
  "expiry_mode": "both",
  "drift_window": 3,
  "ttl_seconds": 604800,
  "verified_only": true,
  "world_facing": false,
  "store_scope": "backend"
}
```

Inspect a policy file and its hash:

```bash
bernstein cache policy --file policy.json
```

## Drift-based expiry (a pure verdict, not a clock race)

`evaluate_freshness(entry, policy, repo_state)` returns a `FreshnessVerdict`
that is a **pure function** of `(entry, policy, repo_state)`. It reads no clock,
no filesystem outside the injected repo state, and no network. An entry is fresh
while:

1. every file its recorded diff touched still hashes to the recorded content
   hash, and
2. its base commit is an ancestor within `drift_window` commits of the current
   head.

Two operators who assemble the same repo state compute the byte-identical
verdict JSON (a `fresh`/`stale` boolean plus a machine-readable reason token)
instead of racing a clock. A wall-clock TTL remains only as a declared backstop
for `world_facing` tasks (web research, external API state) and is honoured only
through an injected timestamp.

## Hits are spine entries

Every policy cache hit emits a `served_from` entry on the always-on
`LineageSpine` (`record_served_from` in
`core/persistence/cache_served_from.py`), at the same write boundary where every
artifact write is intercepted. Because the spine is Merkle-chained and
HMAC-tagged, a run that served steps from cache replays to the identical spine
head; suppressing or mutating any single `served_from` entry fails spine
verification, so a hidden or forged cache hit is falsification-evident rather
than merely unlogged.

## Fleet dedup as a claim, not a lock

When two fleet workers resolve the same cache key, cache-key contention routes
through the atomic claim primitive (`CacheKeyArbiter` in
`core/persistence/cache_dedup.py`, backed by `core/tasks/claim.py`). Exactly one
worker wins the spawn; every loser receives a signed `DuplicateOfReceipt`
binding it to the winner's key and claim position and completes by a lineage
edge to the winner's verified output rather than blocking on an hours-long run.
If the winner fails, the claim releases deterministically and the next contender
proceeds. The receipt verifies offline: mutating the winner reference, the cache
key, or the claim position breaks the HMAC and `verify_duplicate_receipt` names
the mismatching field.

## Surgical, transitive eviction

```bash
bernstein cache evict <key> --reason pr_reverted
```

Eviction tombstones the key and walks the `served_from` ledger transitively,
tombstoning every entry reachable over `served_from` edges
(`core/persistence/cache_eviction.py`). It prints the recall set of consuming
runs, emits a `cache.eviction` audit-chain event, and writes a recall report
under `.sdd/caching/policy/`. A tombstoned key is a hard miss forever - it can
never serve again, even when its drift verdict is fresh.

## Refresh for one run

```bash
bernstein run <plan> --refresh-cache
```

`--refresh-cache` exports `BERNSTEIN_REFRESH_CACHE=1`; the cache boundary treats
every policy lookup as a miss for that run and repopulates with the fresh
outputs. Tasks with no declared policy are unaffected.

## Audit-chain events

Every hit, miss, dedup claim, and eviction is an audit-chain event carrying the
policy hash and recipe hash (`record_cache_hit`, `record_cache_miss`,
`record_cache_dedup_claim`, `record_cache_eviction` in
`core/security/audit_chain.py`), so the fact that a cache decision was taken
under a named policy is itself chain-attested. Only hashes, identifiers, and the
decision are recorded - never prompt or output payloads.

## Determinism and verifiability

* **Determinism.** Policy JSON, recipes, composed keys, freshness verdicts, and
  recall sets are all canonical JSON (sorted keys, minimal separators, UTF-8),
  so two byte-identical inputs produce byte-identical bytes and hashes across
  processes and platforms. The freshness verdict reads no clock, filesystem, or
  network.
* **Verifiability.** Hits are Merkle-chained spine entries, dedup edges are
  HMAC-signed receipts, and evictions are audit-chain events - each
  independently checkable offline rather than trusted.

## Relationship to `#2519`

`#2519` seals which cache strategy a dispatch decision assumed into the dispatch
receipt; this engine defines what a cache policy means at the cache boundary
(keys, expiry, dedup, eviction). A natural follow-up folds the resolved recipe
hash into the knob selection there.
