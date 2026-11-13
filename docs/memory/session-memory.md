# Long-running session memory

`SessionMemory` is the durable, two-layer memory subsystem that lets a
Bernstein agent remember what it did across days, across context-window
compactions, and across machine reboots. It replicates on local disk
what paid persistent-agent vendors sell as a hosted service.

The store has two layers:

| Layer | What it holds | Where it lives |
|-------|---------------|----------------|
| Episodic | Verbatim turn-by-turn log, append-only JSONL, content-addressed | `.sdd/memory/episodic/<task_id>/<session_id>.jsonl` |
| Semantic | SQLite FTS5 index over the episodic content, tags, role | `.sdd/memory/semantic.sqlite` |

The episodic layer is the source of truth; the semantic layer is a
secondary index optimised for BM25 recall.

## Contract

```python
from pathlib import Path

from bernstein.core.memory.session_memory import SessionMemory, Turn

mem = SessionMemory(
    root=Path(".sdd/memory"),
    task_id="t-7",
    session_id="s-1",
)

# Record a turn
mem.append_turn(
    Turn(role="user", content="design the API", tags=["api"]),
)

# Free-text recall, BM25 ranked, newest-first within rank
for hit in mem.recall("API", k=3):
    print(hit.role, hit.content, hit.ts_ns)
```

### Turn shape

A `Turn` has the following fields. Validation happens on `append_turn`.

| Field | Type | Notes |
|-------|------|-------|
| `role` | `str` | One of `user`, `assistant`, `system`, `tool`. |
| `content` | `str` | Non-empty. Stored verbatim. |
| `tags` | `list[str]` | Free-form. No commas. Used as an optional recall filter. |
| `ts_ns` | `int` | Wall-clock nanoseconds. Defaults to `time.time_ns()`. |

The append also stores `task_id`, `session_id`, and a `sha256:<hex>`
content hash so a downstream auditor can correlate a turn with the
lineage entry of any artefact it produced.

### Recall

```python
mem.recall(query, *, k=5, tag=None, task_id=None)
```

- `query` is tokenised by FTS5 `porter unicode61`. Empty queries return
  an empty list. FTS5 operator characters are stripped so a copy-pasted
  string with colons or parentheses does not break the parser.
- `k` is the maximum number of hits. Must be positive.
- `task_id` scopes the search to one task. By default recall spans
  every task in the shared semantic index, which is what you want when
  an agent is looking for context from sibling tasks.

Recall applies these steps in order:

1. The FTS query is restricted to `task_id` when one is supplied.
2. Matching rows are ranked by BM25 (best first), with ties broken by
   newest `ts_ns` first.
3. The ranked candidate window is selected: `k` candidates without a
   `tag` filter, or `4 * k` candidates when `tag` is supplied.
4. With `tag`, the candidate window is filtered to turns containing that
   exact tag; at most `k` hits are returned.

The tag filter runs after candidate selection, not across every matching
row. As a result, a tagged recall can return fewer than `k` hits even if
older rows matching the tag exist: those rows may fall outside the top
`4 * k` ranked candidates.

### Auto-load at agent spawn

The orchestrator wires `load_recent_turns(root, task_id=..., k=...)`
into the agent-spawn path for tasks with prior sessions. The function
returns the most recent N turns for that `task_id`, newest first, so
they can be joined into the system prompt without a free-text query.

```python
from bernstein.core.memory.session_memory import load_recent_turns

prior = load_recent_turns(
    Path(".sdd/memory"),
    task_id="t-7",
    k=10,
)
```

### Prune

```python
mem.prune(older_than_ns)
```

Prune operates at two different scopes, on purpose:

- Episodic JSONL pruning is scoped to this instance's
  `(task_id, session_id)` and drops every turn whose `ts_ns` is strictly
  less than the cutoff. The JSONL file is rewritten in place via a
  sibling temp file plus atomic rename, so a crash mid-prune leaves
  either the old log or the new log intact.
- Semantic index deletion is scoped to `task_id` only, so a parallel
  task sharing the same `.sdd/memory/` root is never touched and stale
  embeddings for other sessions of the same task are cleared in one
  pass.

## On-disk layout

```
.sdd/memory/
├── episodic/
│   ├── task-a/
│   │   ├── session-1.jsonl
│   │   └── session-2.jsonl
│   └── task-b/
│       └── session-1.jsonl
└── semantic.sqlite
```

The semantic database opens in WAL mode so a concurrent reader does
not block the writer.

## Concurrency

- Episodic appends rely on POSIX append-write semantics. Two processes
  appending to the same `(task_id, session_id)` file at the same time
  may interleave bytes within a single record. Bernstein assigns one
  session per agent process so this collision does not occur in
  practice.
- The semantic SQLite database uses its own file-locking. WAL mode
  means readers and writers do not block each other.
- The episodic and semantic layers are written from the same code
  path; if the process crashes between the two writes the episodic
  layer is the source of truth and the next read on the same instance
  will silently rebuild the missing semantic row on the next
  `append_turn` of the same hash. (Reindex utilities are a v2 task.)

## What v1 does not do

| Capability | Status | Notes |
|------------|--------|-------|
| Vector / embedding recall | Out of scope | FTS5 BM25 is the baseline. |
| Cross-machine sync | Out of scope | Single host only. |
| Memory edit / forget | Out of scope beyond `prune` | Operator-driven forgetting is a v2 follow-up. |
| Git-versioned episodic log | Deferred | The JSONL files are intentionally append-only on disk. |

## Related modules

- `src/bernstein/core/memory/sqlite_store.py` -- tag-indexed knowledge base.
- `src/bernstein/core/memory/jsonl_log.py` -- flat per-key JSONL log.
- `src/bernstein/core/memory/cross_task_kb.py` -- explicit publish/subscribe.
- `src/bernstein/core/knowledge/rag.py` -- codebase FTS5 index. The
  `_sanitize_query` helper here matches the one used there.
