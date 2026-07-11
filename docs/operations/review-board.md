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
| Page | `GET /dashboard/review-board` - served by `bernstein serve` and `bernstein gui serve`; queue + diff viewer + evidence panel + action buttons |
| Columns | `queued`, `running`, `gated`, `needs_review`, `merged` - all folded from journal events |
| Projection API | `GET /review-board/runs`, `GET /review-board/runs/<run_id>`, `.../evidence/<task_id>`, `.../diff/<task_id>` |
| Action API | `POST /dashboard/review-board/runs/<run_id>/tasks/<task_id>/review` - approve / request_changes / merge |
| Determinism | Two operators with the same journal render byte-identical board state (same `projection_hash`) |
| Verifiability | Every projection carries `journal_head` + `journal_verified`; a tampered journal renders with a loud `JOURNAL TAMPERED` receipt |
| Detached runs | The fold and the captured diff need only the run directory - a completed or copied-over run projects and diffs exactly like a live one |
| Live updates | The page re-fetches the projection on `/events` SSE activity; the client never mutates board state |
| Auth | Reads: task-server bearer middleware. Actions: dashboard-auth middleware, operator scope required, principal attributed |
| Non-goals | No editing, no chat, no board-side state. An action is a governance receipt in the journal, never board-side state |

## How the columns are derived

The board is a pure fold over the run journal's event vocabulary:

| Journal event | Board transition |
|---|---|
| `task_claimed` | card enters / returns to **running** |
| `task_verification_failed` | card moves to **gated** (failed signals kept as gate receipts) |
| `task_retried` | card moves back to **queued** |
| `task_completed` | card moves to **needs_review** |
| `task_merged` | card moves to **merged** |
| `task_diff_captured` | attaches the card's diff summary (hash + `+/-` + file count); no column move |
| `task_review_decision` | attaches the operator verdict; a `merge` decision moves the card to **merged** |
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

## Diff viewer

The card drawer folds the task diff, ported from the TUI's diff folding:
each file is a collapsible summary (`+added / -removed`, hunk count) that
expands to its hunks and lines.

The diff is **captured at completion time**, not computed live at review
time. When a task's worktree is reaped, the lifecycle stores its
`git diff HEAD` under `.sdd/runs/<run_id>/review/diffs/<task_id>.diff` and
records a `task_diff_captured` journal row carrying only the diff's
`sha256` and its line/file counts. So the diff a reviewer folds open is
exactly the bytes that executed, it travels with a detached run, and it is
verifiable:

```
GET /review-board/runs/<run_id>/diff/<task_id>
{ "sha256": "sha256:...", "verified": true, "added": 3, "removed": 2,
  "files": ["src/a.py"], "diff_text": "diff --git ..." }
```

`verified` is `true` only when the served bytes re-hash to the
journal-chained capture hash **and** the chain still recomputes. A tampered
diff file renders with a `DIFF UNVERIFIED` badge instead of a silently
wrong diff. Capture is fail-open: a task whose diff could not be captured
(no worktree, git error) simply shows "no diff captured".

## Attested actions (approve / request-changes / merge)

An operator acts on a card without leaving the board:

```
POST /dashboard/review-board/runs/<run_id>/tasks/<task_id>/review
{ "decision": "approve" | "request_changes" | "merge", "note": "optional" }
```

The action is a **governance receipt, not board-side state**:

1. The decision is appended to the run journal as a `task_review_decision`
   row via `EventJournal.resume`, so it chains onto the exact verified
   journal head it was made against (a poisoned chain fails closed with
   `409`). The board's `merged` column and review annotations then project
   from that row - strip the chain and the action loses its meaning, not
   just its log line.
2. It is mirrored as a signed `review_board.action` entry on the HMAC audit
   chain, naming the acting principal and binding the `projection_hash` the
   operator saw, the `journal_head` the decision chained onto, and the
   reviewed `diff_hash`. `bernstein audit verify` recomputes it offline.

The endpoint sits under `/dashboard`, so the dashboard-auth middleware
(issue #2366) attributes a real principal and refuses any non-operator
scope before the handler runs; on an unconfigured loopback bind the action
is attributed to the local `dashboard-operator`. The response returns the
receipt and the freshly re-folded board:

```json
{
  "receipt": {
    "decision": "merge", "principal": "operator-olga", "scope": "operator",
    "projection_hash": "...", "journal_head": "...",
    "journal_entry_hash": "...", "diff_sha256": "sha256:...",
    "audit_event_hash": "..."
  },
  "board": { "board": { "columns": {...} }, "journal_verified": true, ... }
}
```

Because the receipt binds what the operator saw (`projection_hash`,
`diff_sha256`) to what executed (`journal_head`, `journal_entry_hash`), a
verifier can prove from the chain alone that a named principal took the
action against that exact board state without operator override.

## Scope and follow-ups

Deferred to a follow-up slice on the same epic:

* Moving the board page into the `web/` SPA build once the GUI CI build
  pipeline replaces the committed prebuilt bundle. The page ships here as a
  dependency-free HTML route so the review surface does not depend on that
  migration.
