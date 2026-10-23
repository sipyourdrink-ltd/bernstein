# Append-only JSONL memory log

A flat, append-only event log keyed by a short identifier, stored as
one JSONL file per key under `.bernstein/memory/`. Consumers read by
tail. The primitive complements the SQLite knowledge store: SQLite
handles tag-indexed knowledge (conventions, decisions, learnings),
this log handles per-key event streams.

## Why it exists

Two requirements drove the split. First, a per-run event tail must
be tail-friendly so a debugger or a sidecar tool can `tail -f` it
during a live run. Second, the log must survive partial writes and
manual edits without taking the whole record set down.

A JSONL file with one event per line covers both: append-write is
atomic at the line level, malformed lines are skipped on read, and
any `tail`/`grep`/`jq` pipeline in the operator's toolkit works on
day one.

## How to use it

The primitive is a single dataclass with `write` / `read` / `list_keys`
(and `clear`) methods. No third-party dependencies, no schema migrations, no
locking primitives beyond what the POSIX append gives.

```python
from pathlib import Path
from bernstein.core.memory.jsonl_log import JSONLMemoryLog

log = JSONLMemoryLog(root=Path(".bernstein/memory"))

# Append one event under a key
log.write("manager.lessons", {"task": "T-1", "lesson": "guard imports"})

# Read everything previously recorded under a key, oldest first
entries = log.read("manager.lessons")

# Enumerate every key currently tracked under root
keys = log.list_keys()
```

Each key maps to exactly one file: `<root>/<key>.jsonl`. Keys are
restricted to a conservative POSIX-safe character set so a poisoned
key cannot escape the root or collide with OS-reserved names.

## Format

One JSON object per line, UTF-8 encoded, compact separators:

```jsonl
{"task":"T-1","lesson":"guard imports"}
{"task":"T-2","lesson":"reject empty diffs"}
```

`ensure_ascii=False` keeps unicode readable; compact separators mean
a tail-corruption only loses the last record.

## Limitations

- No retrieval, scoring, or decay. Use the SQLite store for that.
- No cross-process locking beyond what append-write to a POSIX file
  gives. Callers needing strict serialisation should use SQLite.
- Off-by-default in the orchestrator. The only consumer is the
  spawner's lessons injection, which reads the `lessons` key when
  `BERNSTEIN_MEMORY_AUTO_INJECT=1`; nothing writes to it automatically.
- Key length is capped at 128 characters; key vocabulary is
  alphanumerics plus `.`, `_`, `-`.

## Related

- Source: `src/bernstein/core/memory/jsonl_log.py`
- Companion store: `src/bernstein/core/memory/sqlite_store.py`
- Spawner consumer: `src/bernstein/core/agents/spawn_prompt.py`
