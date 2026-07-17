# Memory: source-adapter provenance and the cross-adapter read filter

`SQLiteMemoryStore` (`src/bernstein/core/memory/sqlite_store.py`) is the
persistent, tag-indexed store that backs Bernstein's cross-session memory.
Every row records the content, type, tags, importance, and a timestamp; each
row can also record the CLI adapter that produced it.

The provenance field and the low-level read filters (`query`,
`get_relevant`, `CrossTaskKB.subscribe`) are opt-in: existing direct callers
see no behaviour change unless they pass the new keywords. The
spawned-agent prompt path is the one exception - see "Enforcement on the
spawned-agent prompt path" below.

## What changed

| Surface | Old | New |
|---------|-----|-----|
| Schema | `memory(... source_agent, source_model)` | adds `source_adapter TEXT` |
| `SQLiteMemoryStore.add` | unchanged | accepts `source_adapter: str \| None = None` |
| `SQLiteMemoryStore.add_many` | n/a | bulk insert with per-row `source_adapter` |
| `SQLiteMemoryStore.query` | n/a | yields rows; supports `read_only_from_adapters` allow-list |
| `SQLiteMemoryStore.get_relevant` | unchanged | accepts `read_only_from_adapters` allow-list (same contract as `query`) |
| `MemoryEntry` | `source_agent`, `source_model` | adds `source_adapter: str \| None` |
| `CrossTaskKB.publish` | unchanged | forwards `source_adapter` to the store |
| `CrossTaskKB.subscribe` | unchanged | accepts `read_only_from_adapters` |
| `bernstein.core.memory.trust_policy` | n/a | `MemoryTrustPolicy` + `active_trust_policy()` - default-enforced provenance policy for spawned prompts |
| `spawner_core._load_persistent_memory` | replayed every row unfiltered | applies `MemoryTrustPolicy` before injecting content into a prompt |

The migration is additive: `_migrate_columns` adds `source_adapter` only if
absent. Existing rows backfill with `NULL` and continue to surface from
`list()`, `query()`, and `get_relevant()` by default (no adapter filter
passed = legacy behaviour, unchanged).

## Threat model

Bernstein routes tasks across a heterogeneous set of adapters
(claude-code, codex, gemini-cli, ...). A row written by adapter A is, by
default, indistinguishable from a row written by adapter B once it lands in
the shared SQLite store. A subscriber operating under adapter B will replay
adapter A's payload verbatim.

That is the shape of a documented cross-adapter memory-poisoning attack
class: a payload written by one adapter steers a later, unrelated adapter
into following injected instructions or exfiltrating data. The orchestrator
sits at the boundary where this isolation has to live; no single adapter can
enforce it for the others.

Recording the producing adapter and offering an allow-list on read is the
primitive that draws that boundary. On the low-level store API
(`query`, `get_relevant`, `CrossTaskKB.subscribe`) that allow-list stays
opt-in, matching the rest of the store's API surface. On the one call site
that actually reaches a live, injectable prompt -
`spawner_core._load_persistent_memory` - the boundary is enforced by
default; see the next section.

## Enforcement on the spawned-agent prompt path

`_load_persistent_memory` builds the "Persistent Memory" section of every
spawned agent's prompt from `SQLiteMemoryStore.get_relevant()`, injecting
`MemoryEntry.content` verbatim. Because that is a live, model-facing
surface, it does not rely on a caller remembering to pass
`read_only_from_adapters=`. Instead it runs every candidate row through
`bernstein.core.memory.trust_policy.MemoryTrustPolicy`:

- Untagged rows (`source_adapter IS NULL`) stay trusted by default. This
  covers pre-migration rows and today's operator/CLI writes
  (`bernstein memory add`, `bernstein memory share`), which do not yet tag
  provenance, so existing manual workflows are unaffected.
- Rows tagged with an explicit `source_adapter` are untrusted by default
  unless that adapter has been opted into the policy's allow-list. This is
  what stops adapter A's payload from steering adapter B's spawned prompt.

Configuration (process environment, read once per `_load_persistent_memory`
call via `active_trust_policy()`):

| Env var | Effect |
|---------|--------|
| `BERNSTEIN_MEMORY_TRUST_POLICY` | Set to `0`/`false`/`no`/`off` to disable enforcement entirely (legacy replay-everything). Enabled by default. |
| `BERNSTEIN_MEMORY_TRUST_ADAPTERS` | Comma-separated allow-list of extra `source_adapter` values to trust alongside untagged rows, e.g. `claude-code,codex`. |

Callers that build their own policy object (tests, or future orchestration
code that knows the current adapter identity) can pass
`trust_policy=MemoryTrustPolicy(...)` directly to
`_load_persistent_memory`, bypassing the environment lookup.

### Known residual gap

`bernstein memory add` and `bernstein memory share` do not currently accept
or auto-derive a `source_adapter` value, so both a genuine operator and a
spawned agent with shell access invoking that CLI produce an identical,
untagged (trusted) row. Tagging those write paths with the invoking
adapter's identity (where available) is tracked as follow-up work; until
then, an adversarial agent with a shell tool can still add an untagged row
through the CLI escape hatch rather than through `SQLiteMemoryStore.add()`/
`CrossTaskKB.publish()` with an explicit foreign `source_adapter`.

## Usage

### Write with provenance

```python
from pathlib import Path
from bernstein.core.memory.sqlite_store import SQLiteMemoryStore

store = SQLiteMemoryStore(Path(".sdd/memory/memory.db"))

store.add(
    type="learning",
    content="HTTP 429 from the upstream API means back off, not retry-now.",
    tags=["http", "retries"],
    source_adapter="claude-code",
)
```

`add_many` accepts the same per-row keyword set and writes the batch in a
single transaction:

```python
store.add_many(
    [
        {"type": "learning", "content": "...", "source_adapter": "claude-code"},
        {"type": "learning", "content": "...", "source_adapter": "codex"},
    ]
)
```

### Read with an adapter allow-list

```python
# Default: every row, including pre-migration NULL-provenance rows.
for entry in store.query():
    ...

# Strict allow-list: rows whose source_adapter is in the list, no NULLs.
for entry in store.query(read_only_from_adapters=["claude-code"]):
    ...
```

Passing an empty list (`read_only_from_adapters=[]`) is treated as "no
adapter is allowed" and returns nothing. This is intentional: an explicit
empty allow-list is a safe default for callers that want a fail-closed read.

### Forwarding through `CrossTaskKB`

Adapters that already use the publish/subscribe facade pick up the feature
without extra wiring:

```python
from bernstein.core.memory.cross_task_kb import CrossTaskKB

kb = CrossTaskKB(store, run_id="r-1", producer_task_id="t-7")
kb.publish(
    tag="api-schema",
    key="users",
    value="...",
    scope="run",
    source_adapter="claude-code",
)

for fact in kb.subscribe(
    tag="api-schema",
    scope="run",
    read_only_from_adapters=["claude-code"],
):
    ...
```

## Migration notes

- New databases pick up `source_adapter` on first open.
- Existing databases gain the column the next time
  `SQLiteMemoryStore.__init__` runs. The migration is idempotent: re-opening
  the same database does not raise on the duplicate `ADD COLUMN`.
- Pre-migration rows persist with `source_adapter = NULL` and are returned by
  default `list()`, `query()`, and `get_relevant()` calls so existing tooling
  sees no read regression.
- Rows with `NULL` provenance are excluded from `query(read_only_from_adapters=...)`
  and `get_relevant(read_only_from_adapters=...)` results because the filter
  is a strict allow-list, not a default-deny. `MemoryTrustPolicy` (used by
  `_load_persistent_memory`) is the layer that keeps untagged rows trusted
  by default instead - see "Enforcement on the spawned-agent prompt path"
  above.

## Out of scope

- Tagging `bernstein memory add` / `bernstein memory share` with the
  invoking adapter's identity - see "Known residual gap" above.
- Lineage-style content-hash chains across rows. `cross_task_kb_meta`
  already carries `content_hash` per published fact.
- Provenance on the JSONL log under `memory/jsonl_log.py`. Add it
  separately if the SQLite primitive proves out.
