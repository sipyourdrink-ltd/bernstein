# Run review board (web merge queue)

Reviewing a fleet's output is the narrowest funnel in the workflow: diffs,
gate results, and merge decisions are spread across TUI panes and terminal
logs. The review board puts the queue on one web surface - and it is built
as a **projection, not an application state**. The server folds the per-run
event journal (`.sdd/runs/<run_id>/journal.jsonl`) on every request; the
board holds zero state of its own.

## TL;DR

| Item | Behaviour |
|---|---|
| Page | `GET /dashboard/review-board` - served by `bernstein serve` and `bernstein gui serve` |
| Columns | `queued`, `running`, `gated`, `needs_review`, `merged` - all folded from journal events |
| Projection API | `GET /review-board/runs`, `GET /review-board/runs/<run_id>`, `GET /review-board/runs/<run_id>/evidence/<task_id>` |
| Determinism | Two operators with the same journal render byte-identical board state (same `projection_hash`) |
| Verifiability | Every projection carries `journal_head` + `journal_verified`; a tampered journal renders with a loud `JOURNAL TAMPERED` receipt |
| Detached runs | The fold needs only the journal file - a completed or copied-over run projects exactly like a live one |
| Live updates | The page re-fetches the projection on `/events` SSE activity; the client never mutates board state |
| Auth | Covered by the task server's bearer-token middleware like every other API route |
| Non-goals | No editing, no chat, no board-side state, no write endpoints |

## How the columns are derived

The board is a pure fold over the run journal's event vocabulary:

| Journal event | Board transition |
|---|---|
| `task_claimed` | card enters / returns to **running** |
| `task_verification_failed` | card moves to **gated** (failed signals kept as gate receipts) |
| `task_retried` | card moves back to **queued** |
| `task_completed` | card moves to **needs_review** |
| `task_merged` | card moves to **merged** |
| `run_started` / `run_completed` | populate the board's run envelope |

`task_merged` is recorded by the task lifecycle at the moment a verified
task's work lands, so the merged column is a journal fact rather than a
side inference. Journals recorded before this event existed simply project
an empty merged column. Unknown event types are ignored, so a newer journal
never breaks an older board renderer.

Card ordering inside a column is the journal index of the card's last
transition (ties broken by task id) - ordering is a property of the
journal, not of render time.

## The projection is a receipt

`GET /review-board/runs/<run_id>` returns:

```json
{
  "run_id": "run-...",
  "board": { "schema_version": 1, "run": {...}, "event_count": 12, "columns": {...} },
  "projection_hash": "<sha256 of the canonical board bytes>",
  "journal_head": "<the journal's Merkle head>",
  "journal_verified": true,
  "event_count": 12
}
```

* `projection_hash` is the SHA-256 of the canonical (sorted-key, compact)
  board JSON. The fold never reads the wall-clock envelope on journal rows,
  so replaying the same journal anywhere reproduces the same hash.
* `journal_head` binds the board to the run's hash chain - the same head
  that `bernstein replay`-surface verification pins.
* `journal_verified` is the result of re-walking the whole chain at
  projection time. `false` means a row no longer recomputes (edited,
  truncated-then-extended, reordered); the page renders the board but
  brands it `JOURNAL TAMPERED`.

To cross-check an API response against a local journal copy:

```python
from bernstein.core.replay.journal import load_events
from bernstein.core.replay.review_board import board_hash, project_board

board = project_board(load_events(path_to_journal))
assert board_hash(board) == response["projection_hash"]
```

## Evidence on the card

Opening a card fetches
`GET /review-board/runs/<run_id>/evidence/<task_id>` - the task's sealed
verification-evidence bundle (content-addressed producer outputs, gate
verdict, signature, audit-chain entry hash) plus a recomputed
`bundle_hash`. Tasks that declared no evidence producers return 404 and
the drawer says so.

## Scope and follow-ups

Shipped in the core slice: the projection API, the journal-backed merge
receipt, and the board page.

Deferred to follow-up slices on the same epic:

* Approve / request-changes / merge actions from the board, going through
  the existing attested-approval path with principal attribution on every
  action (arrives together with the scoped dashboard-auth surface).
* The TUI diff viewer port (folding) into the card drawer.
* Moving the board page into the `web/` SPA build once the GUI CI build
  pipeline replaces the committed prebuilt bundle.
