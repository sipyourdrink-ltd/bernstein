# Task Lifecycle CLI

Driving Bernstein from a script means knowing which command moves a task between which states, what the JSON outputs look like, and where the durable state lives. This page is the contract.

> Source of truth for transitions: [`architecture/LIFECYCLE.md`](../../architecture/LIFECYCLE.md). All flag claims below are cited as `cli/<file>:<line>`.

## The 12 task states

| State | One-liner |
|---|---|
| `PLANNED` | Awaiting human approval before execution (plan mode). |
| `OPEN` | Ready for an agent to claim. Default starting state. |
| `CLAIMED` | An agent has claimed the task but has not started work. |
| `IN_PROGRESS` | Agent is actively working on the task. |
| `DONE` | Agent reported completion. Pending janitor verification + merge. |
| `CLOSED` | Verified and merged. Terminal. |
| `FAILED` | Agent failed or verification rejected. Can retry within `max_retries` (default 3). |
| `BLOCKED` | Waiting on another task, resource, or approval. |
| `WAITING_FOR_SUBTASKS` | Parent task waiting for child subtasks to complete. |
| `CANCELLED` | Manually or programmatically cancelled. Terminal. |
| `ORPHANED` | Agent crashed mid-task; awaits crash recovery. |
| `PENDING_APPROVAL` | Task completed but requires human approval before merge. Set directly by the approval subsystem (no FSM-managed exit). |

The lifecycle kernel (`core/tasks/lifecycle.py`) rejects any transition not in the table at `architecture/LIFECYCLE.md` with `IllegalTransitionError`. Approvals, cancels, and verification all flow through the same kernel; the CLI is just a thin layer over the HTTP routes documented in [`reference/openapi-reference.md`](../openapi-reference.md).

The flow you usually drive from a script is:

```
PLANNED ──approve──▶ OPEN ──claim──▶ CLAIMED ──start──▶ IN_PROGRESS
                                                          │
                                            agent reports │ success
                                                          ▼
                                                        DONE ──verify+merge──▶ CLOSED
```

---

## `bernstein add-task`

Create a single task on the running task server (`POST /tasks`).

**Synopsis:** `bernstein add-task TITLE [flags]`

**Flags:** *(source: `cli/commands/task_cmd.py:37-66`)*

| Flag | Default | Meaning |
|---|---|---|
| `TITLE` | required | Short task name (positional). |
| `--role` | `backend` | Agent role for this task. |
| `-d / --description` | `""` | Long description (free-form text). |
| `--priority` | `2` | `1`=critical, `2`=normal, `3`=nice-to-have (range 1-3). |
| `--scope` | `medium` | `small` / `medium` / `large`. |
| `--complexity` | `medium` | `low` / `medium` / `high`. |
| `--depends-on TASK_ID` | - | Task IDs this task depends on. Repeatable. |
| `--dry-run` | off | Print the JSON payload without calling the API. |

The command is registered as `task compose` internally and exposed as the visible `bernstein add-task` (see `cli/main.py:696`).

**Example - create a task and read its ID:**

```bash
bernstein add-task "Add JWT middleware" \
  --role backend \
  --description "Express middleware that validates HS256 tokens" \
  --priority 1 \
  --scope medium \
  --depends-on T-deadbeef
```

The server responds with the created task as JSON; in `--json` mode the CLI re-emits it on stdout. Pipe to `jq -r .id` to capture the ID.

---

## `bernstein list-tasks`

List tasks visible to the running task server, with optional filters.

**Synopsis:** `bernstein list-tasks [flags]`

**Flags:** *(source: `cli/commands/task_cmd.py:637-647`)*

| Flag | Default | Meaning |
|---|---|---|
| `--status-filter` | none | One of `open / claimed / in_progress / done / failed / blocked`. |
| `--role ROLE` | none | Filter by role (e.g. `backend`, `qa`, `security`). |
| `--json` | off | Emit raw JSON list instead of the Rich table. |

The data source is `GET /status`; only tasks the server currently knows about are returned (archived tasks are not in this view - use `bernstein recap` or `bernstein replay` for archive views).

```bash
# Only the in-flight backend tasks, JSON
bernstein list-tasks --status-filter in_progress --role backend --json
```

For a holistic view including dependency edges and the critical path, use `bernstein plan --graph`. (`cli/commands/task_cmd.py:454-486`.)

---

## `bernstein pending`

List tasks waiting for **human approval** in the `--approval review` flow.

**Synopsis:** `bernstein pending [flags]`

**Flags:** *(source: `cli/commands/task_cmd.py:291-303`)*

| Flag | Default | Meaning |
|---|---|---|
| `--workdir` | `.` | Project root (parent of `.sdd/`). |

A task is "pending" when its task store has dropped a JSON file under `.sdd/runtime/pending_approvals/`. This happens automatically when `bernstein run --approval review` verifies a task and pauses awaiting your decision. The state is **not** `PENDING_APPROVAL` (which is the FSM enum used by the security/approval subsystem); the file-on-disk semaphore is what `bernstein pending` reads.

JSON output mode (`--json` on the root) emits the raw array of pending records - useful in scripts:

```bash
bernstein --json pending | jq -r '.[].task_id' | while read tid; do
  bernstein approve "$tid"
done
```

---

## `bernstein approve` / `bernstein reject`

Resolve a pending review.

**Synopsis:**
```
bernstein approve TASK_ID [--workdir .]
bernstein reject  TASK_ID [--workdir .]
```

*(source: `cli/commands/task_cmd.py:249-288`)*

| Flag | Default | Meaning |
|---|---|---|
| `TASK_ID` | required | Task ID, positional. |
| `--workdir` | `.` | Project root. |

Both commands are file-only: they write `.sdd/runtime/approvals/<id>.approved` or `.rejected`. The orchestrator's next tick picks the file up, transitions the task (merge on approve, cleanup on reject), and removes the file. **Approval and rejection are both idempotent** - the orchestrator scrubs duplicates.

> **Not the same as `bernstein approve-tool` / `bernstein reject-tool`.** Those resolve **tool-call** approvals (a single tool invocation by a running agent). The lifecycle approve/reject above resolves a **whole task's verification** review. See `cli/commands/approval_cmd.py` for the tool-call variants.

---

## `bernstein cancel`

Cancel a running, claimed, or queued task.

**Synopsis:** `bernstein cancel TASK_ID [-r REASON]`

**Flags:** *(source: `cli/commands/task_cmd.py:160-172`)*

| Flag | Default | Meaning |
|---|---|---|
| `TASK_ID` | required | Task to cancel. |
| `-r / --reason` | `Cancelled by user` | Reason recorded in the audit log. |

The CLI calls `POST /tasks/{id}/cancel` on the running server. Cancellation is **graceful by default**: an in-flight agent receives a soft-stop signal and is allowed to write its current artefact and emit a `task_cancelled` event before the worktree is torn down. The terminal state is `CANCELLED`.

There is no `--force` on `cancel` itself. To kill an agent process **without** unwinding state, use `POST /agents/{session_id}/kill` (auth required) directly, or `bernstein stop --force` (`cli/commands/stop_cmd.py:717`) for a global hard-stop.

---

## `bernstein review` / `bernstein verify`

Two related but distinct gates.

**`bernstein review`** *(source: `cli/commands/task_cmd.py:175-246`)* triggers a manager-agent review of the entire task queue. With `--pipeline` it runs a YAML review pipeline against a specific PR; without it, it drops a flag file (`.sdd/runtime/review_requested`) that the orchestrator picks up next tick. Useful when you suspect the planner has wandered off-course and want a structured re-evaluation.

| Flag | Default | Meaning |
|---|---|---|
| `--workdir` | `.` | Project root. |
| `--pipeline FILE` | none | Path to a `review.yaml` pipeline. |
| `--pr N` | none | GitHub PR number to review (requires `--pipeline`). |
| `--validate-only` | off | Validate `--pipeline` schema and exit. No agents run. |
| `--dry-run` | off | Print resolved pipeline; spawn nothing. |

**`bernstein verify`** *(source: `cli/commands/verify_cmd.py`)* runs the **quality pipeline** (lint / tests / type-check / custom gates) on a specific task's artefact. It's the same gate the janitor runs automatically; calling it manually is useful when you want to re-run gates after fixing something out-of-band. See [`architecture/quality-pipeline.md`](../../architecture/quality-pipeline.md) for what the gates do.

---

## `bernstein merge`

Merge a completed task's worktree into the project's main branch.

**Synopsis:** `bernstein merge [flags]`

*(source: `cli/commands/merge_cmd.py:64+`)*

The merge **strategy** is governed by the root-level `--merge` flag (`cli/main.py:520-527`):

| Strategy | Behaviour |
|---|---|
| `pr` (default) | Open a GitHub PR. The PR is the merge gate. |
| `direct` | Push directly to the main branch. Skip PR review. |

Use `--merge direct` only on solo / experimental projects where review overhead is unjustified. Most runs leave it on `pr`, which composes nicely with the GitHub Action and `bernstein review-responder`.

Internally, both strategies converge on the same task FSM transition `DONE → CLOSED` after the merge succeeds. A failed merge keeps the task at `DONE` so it can be re-attempted.

---

## End-to-end script example

A single bash flow that creates a task, watches it complete, and merges. Assumes the Bernstein server is already running (`bernstein start` or a previous `bernstein run`).

```bash
#!/usr/bin/env bash
set -euo pipefail

# 1. Create a task and capture the ID.
TASK_JSON=$(bernstein --json add-task "Add SBOM endpoint" \
  --role backend \
  --description "Expose POST /sbom that returns CycloneDX." \
  --priority 1 \
  --scope small)
TASK_ID=$(echo "$TASK_JSON" | jq -r .id)
echo "Created $TASK_ID"

# 2. Poll until the task reaches a terminal-ish state.
while :; do
  STATUS=$(bernstein --json list-tasks \
    | jq -r --arg id "$TASK_ID" '.[] | select(.id == $id) | .status')
  case "$STATUS" in
    done|failed|cancelled|closed) break ;;
    "")  echo "task $TASK_ID disappeared from server"; exit 1 ;;
    *)   sleep 5 ;;
  esac
done

# 3. If the run paused for review, resolve it.
if bernstein --json pending | jq -e --arg id "$TASK_ID" '.[] | select(.task_id == $id)' >/dev/null; then
  bernstein approve "$TASK_ID"
fi

# 4. Trigger the merge (if the run wasn't already auto-merging).
bernstein merge

# 5. Final state - anything other than `closed` means the merge failed.
bernstein --json list-tasks \
  | jq --arg id "$TASK_ID" '.[] | select(.id == $id)'
```

Notes:

- All four state-mutating commands (`add-task`, `approve`, `reject`, `cancel`) are safe to retry. The server / orchestrator dedupes on `task_id`.
- Treat `cancelled` / `failed` / `closed` as terminal in scripts. `done` is **not** terminal - it precedes verification + merge.
- For long-running orchestrations, prefer `bernstein watch` (streams events) over a polling loop. (`cli/watch_cmd.py:252`.)
