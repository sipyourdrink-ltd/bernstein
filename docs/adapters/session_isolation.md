# Deterministic session-id binding

Bernstein derives a deterministic session id for each adapter conversation
so that replays reach the same conversation slot on rerun and concurrent
sessions of the same adapter in one worktree do not collide.

## The problem

Several CLIs let the underlying agent assign its own session id. Two
failure modes follow:

- A rerun of the orchestrator cannot resume the same conversation, because
  the id the CLI minted last time is non-deterministic.
- Two parallel sessions of the same adapter in the same workspace can
  clobber each other's on-disk history files.

## The binding

The derived id comes from the orchestrator's conversation id, namespaced
per adapter:

```text
digest      = HMAC-SHA256(key=conversation_id, msg="bernstein.adapter:" + adapter_name)
session_id  = UUID built from the first 16 digest bytes, with the RFC 4122
              version (5) and variant bits stamped in.
```

HMAC is used here purely as a keyed, well-defined mixing function with a
fixed namespace string; it is not a security boundary. The recipe lives in
[`src/bernstein/adapters/session_id.py`](https://github.com/sipyourdrink-ltd/bernstein/blob/main/src/bernstein/adapters/session_id.py)
and is versioned through `DERIVE_RECIPE_VERSION` so any future change to the
recipe is explicit.

Key properties:

- Stable across processes and runs: identical inputs always produce the
  same id.
- Distinct per conversation: a different `conversation_id` yields a
  different id.
- Distinct per adapter: the adapter name is mixed into the namespace, so
  two adapters sharing a conversation id never collide.

## Spawn-time wiring

Each adapter declares an optional `session_id_flag` in its capability
contract under `tests/contract/contracts/<adapter>.yaml`:

- When the upstream CLI accepts a caller-supplied session id, the contract
  names the flag (for example `--session-id`). At spawn time the adapter
  pins the derived id via `CLIAdapter.session_id_args(conversation_id)`.
- When the CLI exposes no such flag, `session_id_flag` is omitted (or
  blank). The derived id is still recorded in orchestrator state for
  cross-reference, but no flag is passed.

## Replay lookup

The replay subsystem records each run's derived id in a small index at
`<.sdd>/session_index.json`, mapping `(conversation_id, adapter_name)` to a
run. Replay then resolves a prior run directly, with no scan over
`events.jsonl` logs:

```python
from bernstein.core.replay import locate_run, record_run

record_run(sdd_dir, conversation_id, adapter_name, run_id)
record = locate_run(sdd_dir, conversation_id, adapter_name)
```

A rerun overwrites the slot for a key rather than appending a duplicate, so
the lookup always points at the most recent run for that conversation.

## Out of scope

- Rewriting session ids for already-stored history.
- Sharing one session across different adapters.
