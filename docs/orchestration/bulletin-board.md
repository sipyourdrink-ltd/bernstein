# Bulletin board (cross-agent messaging)

An append-only, in-memory message log shared by every agent in a run.
Agents post typed messages (alerts, blockers, findings, status updates,
dependency notes); other agents and the orchestrator read messages posted
since their last check. It is also the trigger surface for [BLOCKER
clearance gates](worker-coordination.md#blocker-clearance-gates).

Source: `src/bernstein/core/communication/bulletin.py` (imported elsewhere
as `bernstein.core.bulletin`, a back-compat alias to the same module).

## Message shape

```python
@dataclass(frozen=True)
class BulletinMessage:
    agent_id: str
    type: Literal["alert", "blocker", "finding", "status", "dependency"]
    content: str
    timestamp: float = 0.0   # auto-filled on post() if left as 0
    cell_id: str | None = None
```

## REST API

The multi-cell task server exposes the board at:

| Method | Path | Notes |
|---|---|---|
| `POST` | `/bulletin` | Body: `{agent_id, type, content, cell_id}`. Returns `201` on stored + acted, `202` if a registered signal action (e.g. a blocker's clearance gate) failed and was queued for retry. |
| `GET` | `/bulletin?since=<ts>` | Messages with `timestamp` strictly greater than `since`. |

```bash
curl -s -X POST http://127.0.0.1:8052/bulletin \
  -H "Content-Type: application/json" \
  -d '{"agent_id": "backend-1", "type": "finding", "content": "shared auth helper lives in core/auth.py"}'

curl -s "http://127.0.0.1:8052/bulletin?since=0"
```

Posts are also broadcast on the server's SSE bus under the `bulletin`
channel for live UI updates.

## In-process API

```python
from bernstein.core.communication.bulletin import BulletinBoard, BulletinMessage

board = BulletinBoard()
board.post(BulletinMessage(agent_id="qa-1", type="status", content="ran full suite, 2 failures"))

recent = board.read_since(0.0)          # everything
blockers = board.read_by_type("blocker")
cell_msgs = board.read_by_cell("cell-2")
```

Helper posters exist for common message shapes:
`post_file_created(agent_id, file_path, classes=...)` and
`post_api_endpoint(agent_id, method, route, response=...)`, both of which
post `finding`/`status` messages with a formatted `content` string.

`board.summary(limit=10)` renders the most recent messages as
`- agent_id: content` lines; this is what gets injected into a newly
spawned agent's prompt as recent team activity (`multi_cell.py` passes
`bulletin.summary()` into `spawner_core`'s `bulletin_summary` parameter).

## Storage: in-memory, per server process

`BulletinBoard` holds messages in a plain Python list guarded by a
`threading.Lock`. The class exposes `flush_to_disk(path)` and
`load_from_disk(path)` for appending to / hydrating from a JSONL file, but
nothing in the shipped server or CLI calls either method — the running
task server creates one `BulletinBoard()` at startup
(`core/server/server_app.py`) and nothing persists it across a restart.
Treat the board as scoped to a single run's server process: history is
lost on `bernstein stop` / server restart unless something outside the
base orchestrator calls `flush_to_disk()` itself.

## Related primitives in the same module

`bulletin.py` also defines two other cross-agent communication types that
are separate from the message board itself:

- **`MessageBoard`** — agent-to-agent work delegation: `post_delegation()`
  creates a request targeted at a role, another agent `claim()`s it and
  `post_result()`s. Tracks `PENDING` / `CLAIMED` / `COMPLETED` / `EXPIRED`
  status per delegation.
- **`DirectChannel`** — lightweight query/response for information
  exchange (`post_query()` / `post_response()`), addressed at a specific
  agent, a role, or broadcast, with a TTL per query.

Both have their own `flush_to_disk()` / `load_from_disk()` pairs with the
same in-memory-by-default caveat as the board above.

## Typed signals and clearance gates

A posted `blocker` can drive real scheduler state, not just visibility:
see [Cross-worker coordination — BLOCKER clearance
gates](worker-coordination.md#blocker-clearance-gates) for how
`ClearanceGateCoordinator` turns a blocker post into a chain-anchored
clearance task with injected `depends_on` edges, and how `post()` raises
`SignalActionFailure` (rather than silently succeeding) when the gate hook
fails to materialize.
