# Cross-task knowledge share

`CrossTaskKB` is the public surface on top of the existing tag-indexed SQLite
memory store. It lets one task **publish** a fact under a tag and another task
**subscribe** on that tag, without writing files into a shared worktree path
and hoping the next agent reads them.

The facade is a thin wrapper. It does not introduce new storage. Every fact
is persisted as a row in the existing `memory` table (with `type='cross_task'`)
plus an attribution sidecar row that carries the lineage-style triple
`(producer_task_id, ts_ns, content_hash)`.

## Contract

```python
from bernstein.core.memory.cross_task_kb import CrossTaskKB
from bernstein.core.memory.sqlite_store import SQLiteMemoryStore

store = SQLiteMemoryStore(Path(".sdd/memory/memory.db"))
facade = CrossTaskKB(store, run_id="r-1", producer_task_id="t-7")

# Publish
facade.publish(tag="api-schema", key="users", value="...", scope="run")

# Subscribe (yields newest-first, one fact per key)
for fact in facade.subscribe(tag="api-schema", scope="run"):
    use(fact.value)
```

### Fact attribution

Every fact carries the same triple a lineage entry would:

| Field | Type | Meaning |
|-------|------|---------|
| `producer_task_id` | `str` | Task ID of the publisher. |
| `ts_ns` | `int` | Wall-clock nanoseconds at publish time. |
| `content_hash` | `str` | `sha256:<hex>` over the UTF-8 value bytes. |

This mirrors `bernstein.core.lineage.signed_write.seal_write`, so
an auditor can correlate a fact with the lineage entry of the artefact that
produced it.

## Scope rules

| Scope | Visibility | Use when |
|-------|------------|----------|
| `run` | Same `run_id` only. | Short-lived hand-offs between agents in the same orchestration run. |
| `project` | Any task in the same `.sdd/` root. | Conventions, decisions, schemas the whole project should see across runs. |

Both scopes are single-host. Cross-machine replication is out of scope; the
ticket calls it out explicitly as v2.

## Conflict resolution

Two tasks publishing under the same `(scope, tag, key)`: **last-write-wins**.
The earlier rows are not deleted, so the audit log retains every version, but
`subscribe()` only returns the newest one per key. A warning is logged into
the trace when a prior fact under the same identity is overwritten.

Operator-policy resolution (allow/deny/prompt) is a v2 follow-up.

## Lifecycle hook

Each publish fires the `kb.fact_published` lifecycle event. Hook plugins
receive the attribution payload plus `tag`, `key`, `scope`. The hook is
best-effort: a misbehaving plugin cannot block the publish path.

## CLI

```
bernstein memory share <key> <value> --tag <tag> --scope run|project
bernstein memory query --tag <tag> --scope run|project [--raw]
```

`query` redacts emails, phone numbers, SSNs, and credit card numbers in the
output by default. Pass `--raw` to print stored values verbatim - useful for
operator debugging when the redaction is itself the issue.

The CLI reads `BERNSTEIN_RUN_ID` and `BERNSTEIN_TASK_ID` from the environment
for attribution. Outside an orchestrator-spawned session the producer task ID
defaults to `manual-cli`.

## Telemetry

`CrossTaskKB.counter.snapshot()` returns `(publish, subscribe)` counts for the
current process. The orchestrator reads these into the run summary.

## Worked example

A researcher publishes the discovered API schema:

```python
researcher = CrossTaskKB(store, run_id="r-1", producer_task_id="researcher-1")
researcher.publish(
    tag="api-schema",
    key="users",
    value="{name: str, email: str}",
    scope="project",
)
```

A backend agent in a later task subscribes on the same tag:

```python
backend = CrossTaskKB(store, run_id="r-1", producer_task_id="backend-1")
for fact in backend.subscribe(tag="api-schema", scope="project"):
    print(fact.key, fact.value, "from", fact.producer_task_id)
```

The backend agent sees `users / {name: str, email: str} / from researcher-1`.

## Out of scope (v1)

- Vector or embedding similarity for tag matching. v1 = exact tag string only.
- Cross-machine replication.
- Conflict resolution beyond last-write-wins.
- Auto-injection into role prompts. Agents have to opt in by calling the
  facade or the CLI.
