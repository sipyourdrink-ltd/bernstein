# Task Lifecycle CLI

Driving Bernstein from a script means knowing which command moves a task between which states, what the JSON outputs look like, and where the durable state lives. This page is the contract.

> Source of truth for transitions: [`architecture/LIFECYCLE.md`](../../architecture/LIFECYCLE.md). All flag claims below are cited as `cli/<file>:<line>`.

## Task states

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
| `STOP_REQ` | Cancellation requested. The reaper waits for the worker group to exit. |
| `SUSPENDED` | Parked mid-session. Resumable from an attested receipt. |
| `ABANDONED` | Agent voluntarily ended the task with a structured reason. |
| `BLOCKED_BY_ABANDON` | An abandoned dependency blocks the task. |
| `BLOCKED_BY_FAILED_DEP` | An unsuccessful dependency blocks the task. If that dependency recovers while another prerequisite still has an unresolved failure, the task stays blocked and names that prerequisite. |
| `REFUSED` | Worker reported a typed refusal. |
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

**Flags:** See `bernstein add-task --help`.

| Flag | Default | Meaning |
|---|---|---|
| `TITLE` | required | Short task name (positional). |
| `--role` | `backend` | Agent role for this task. |
| `-d / --description` | `""` | Long description (free-form text). |
| `--priority` | `2` | `1`=critical, `2`=normal, `3`=nice-to-have (range 1-3). |
| `--scope` | `medium` | `small` / `medium` / `large`. |
| `--complexity` | `medium` | `low` / `medium` / `high`. |
| `--depends-on TASK_ID` | - | Task IDs this task depends on. Repeatable. |
| `--criterion-profile PRESET` | none | Per-task preset such as `safety-first`, `speed-first`, `balanced`, or `cost-first`. |
| `--mode` | none | Worker response style (`verbose`, `balanced`, `terse`) or mode profile (`fast`, `smart`, `deep`). |
| `--artifact-kind` | none | Artifact type (`report`, `dataset`, `action_log`, `ops_result`, `finding`, or `blob`). Requires `--artifact-output`. |
| `--artifact-output PATH` | none | Workdir-relative artifact path. |
| `--artifact-criterion TYPE:VALUE` | none | Repeatable check of type `schema_valid`, `criteria_match`, or `hash_stable`. Requires `--artifact-kind`. |
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

### If creation returns no result

Do not automatically retry `add-task` after a timeout or lost response. Its
create payload has no caller-chosen task ID; the task store generates a new
ID for each creation. The first request may already have created a task even
when the caller did not receive its ID, and a second request can create a
duplicate.

Read the current backlog before deciding whether to submit again:

```bash
bernstein plan --export task-backlog.json
jq '.[] | select(.title == "Add SBOM endpoint" and .role == "backend")' task-backlog.json
```

Compare the candidate's description, dependencies, and other submitted
fields as well as its title and role. If it is the task you created, reuse
its ID. Titles are not unique, so a title match alone does not establish
identity. This export makes one unpaginated `GET /tasks` request, which
returns at most 500 tasks. No match in that export is not proof that creation
failed: the new task may be outside the returned page. A larger backlog
requires complete, paginated task readback before deciding it is absent.
If readback fails or leaves the result ambiguous, resolve that uncertainty
before issuing another create. Retry safety depends on the individual
command; a successful read does not make task creation idempotent.

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

List tasks waiting for **human approval** before starting or after verification.

**Synopsis:** `bernstein pending [flags]`

**Flags:** *(source: `cli/commands/pending_cmd.py`)*

| Flag | Default | Meaning |
|---|---|---|
| `--workdir` | `.` | Project root (parent of `.sdd/`). |
| `--kind` | `all` | Use `approval` for pre-spawn gates or `spawn` for post-completion review. |

The pre-spawn gate writes `.sdd/runtime/approvals/<task_id>.pending`.
When the orchestrator is configured for post-completion review, it parks
verified tasks under `.sdd/runtime/pending_approvals/<task_id>.json`.

JSON output mode (`--json` on the root) returns an object with
`approval_pending` and `spawn_pending` arrays:

```bash
bernstein --json pending | jq -r '.spawn_pending[].task_id' | while read tid; do
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

Both commands are file-only: they write `.sdd/runtime/approvals/<id>.approved` or `.rejected`. The orchestrator's next poll picks the file up and transitions the task (merge or spawn on approve, cleanup on reject). **Approval and rejection are both idempotent**: a second call for the same open request reports `Already approved` / `Already resolved` and changes nothing.

### Decision records

The file each command writes is a signed **decision record**, not a marker. The same applies to the task server's `POST /approvals/{id}/approve` and `/reject` routes and to `/approve` / `/reject` in `bernstein chat`.

| Field | Meaning |
|---|---|
| `task_id` | The task the decision applies to. |
| `outcome` | `approved` or `rejected`; must match the file suffix. |
| `source` | Decision path: `cli`, `web`, `chat`, or `timeout-default` (written by the gate itself when nobody decided). |
| `principal` | Who decided: `os-user:<name>` (`local-shell`) for the CLI, the authenticated principal for the web routes (`dashboard-operator` / `loopback` when no scoped tokens are configured), `chat:<platform>:<user>` for chat. |
| `nonce` | The nonce of the approval request being answered. Each gate writes a fresh one into its pending file (`approvals/<id>.pending` for the pre-spawn gate, `pending_approvals/<id>.json` for the review gate) when it opens a request. |
| `decided_at`, `reason` | Decision time and the optional reason. |
| `mac` | HMAC-SHA256 over the other fields, keyed from the install's audit key. |

The MAC key is derived from the audit key at `$BERNSTEIN_AUDIT_KEY_PATH` (default `$XDG_STATE_HOME/bernstein/audit.key`, mode `0600`), outside the project directory. `bernstein approve` / `reject`, the task server, and the orchestrator must resolve the **same** audit key. If they do not, the gate cannot verify the record and rejects the task.

**Fail-closed rule.** Both gates (pre-spawn `ApprovalSpec` gate and post-completion review gate) honour a decision file only if all of these hold:

- the MAC verifies;
- `task_id` is the gated task;
- `outcome` matches the file name;
- `nonce` equals the nonce of the request the gate has open.

Anything else in a decision slot resolves the gate to **rejected**: an empty or plain-text file, a record copied from another task, or a record answering an earlier request. The audit event `approval_resolved` then carries `decision_source: "unverified-file"`, `verified: false`, and a `verification_failure` reason (`not-a-record`, `bad-signature`, `task-mismatch`, `outcome-mismatch`, `nonce-mismatch`, ...). The rejected file is moved aside to `<id>.<outcome>.unverified` and kept as evidence.

With no decision file, nothing changes: the gate waits and then applies its timeout policy (`decision_source: "timeout-default"`).

A verified decision produces an `approval_resolved` event naming the real decision path (`decision_source`) and the principal (`principal`, `principal_auth_method`, `principal_kind`, `principal_grant`). The `gate` field says which gate resolved it: `pre-spawn` or `post-completion-review`.

**Leftover decisions.** When a gate opens a new request, an authentic record left by an earlier request for the same task is removed and logged as `approval_decision_superseded`. Example: the pre-spawn approval of a task that has now reached its review gate. Such a record never answers the new request; the operator decides again.

**No open request.** If no request is open for the task when you run `bernstein approve`, the command says so. The record it writes binds no request nonce, so a gate opened later will not accept it. The exception is a task parked with `--until approval`, which checks the signature and task binding only (see [durable suspend/resume](../../operations/durable-suspend-resume.md#wake-on-approval)).

**What this protects against.** Anything that can only create, copy, or overwrite files under `.sdd/` cannot produce a record a gate accepts. That covers an agent's ordinary file-writing tools. A process running as the same OS user can still read the audit key and sign a record on purpose. Run agents under a separate OS user or in a sandbox if that boundary matters for your deployment.

> **Not the same as `bernstein approve-tool` / `bernstein reject-tool`.** Those resolve **tool-call** approvals (a single tool invocation by a running agent) and are also reachable as `bernstein approve --tool <id>` / `bernstein reject --tool <id>`. The lifecycle approve/reject above resolves a **whole task's verification** review. See `cli/commands/approval_cmd.py` for the tool-call variants.

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

**[`bernstein verify`](verify.md)** builds and verifies signed run receipts and checks evidence such as WAL integrity and execution fingerprints. The janitor runs the [task quality pipeline](../../architecture/quality-pipeline.md) during task completion.

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

A single bash flow that creates a task and waits for verified closure, with a
bounded polling period. Run it from the same project directory as the server
so `pending` reads that project's approval files. Set `SERVER_URL` to the
same task server used by the CLI (the example uses the local default).
The normal verification-and-merge path must be enabled.

The script exits with `2` when an operator decision is needed; inspect
`bernstein pending` and review the task before choosing `approve` or
`reject`, then resume monitoring that task ID. `done` means agent work has
finished, not that verification and merge succeeded. Only `closed` is success.

```bash
#!/usr/bin/env bash
set -euo pipefail

SERVER_URL=http://127.0.0.1:8052
POLL_TIMEOUT_S=${POLL_TIMEOUT_S:-600}

# 1. Create once and capture the ID; do not blindly retry a lost response.
TASK_JSON=$(bernstein --json add-task "Add SBOM endpoint" \
  --role backend \
  --description "Expose POST /sbom that returns CycloneDX." \
  --priority 1 \
  --scope small)
TASK_ID=$(echo "$TASK_JSON" | jq -er .id)
echo "Created $TASK_ID"

# 2. Check approvals before waiting, including pre-spawn approval requests.
DEADLINE=$((SECONDS + POLL_TIMEOUT_S))
while (( SECONDS < DEADLINE )); do
  PENDING=$(bernstein --json pending)
  if jq -e --arg id "$TASK_ID" \
    '[.approval_pending[], .spawn_pending[]] | any(.task_id == $id)' \
    <<<"$PENDING" >/dev/null; then
    echo "task $TASK_ID needs operator review; inspect bernstein pending"
    exit 2
  fi

  # Read one task directly, rather than depending on the task-list envelope.
  STATUS=$(curl --fail --silent --show-error --max-time 10 \
    "$SERVER_URL/tasks/$TASK_ID" | jq -er .status)
  case "$STATUS" in
    closed) echo "Task $TASK_ID is closed"; exit 0 ;;
    planned|pending_approval)
      echo "task $TASK_ID needs operator review"; exit 2 ;;
    failed|cancelled|abandoned|refused|blocked_by_abandon|blocked_by_failed_dep)
      echo "task $TASK_ID stopped in state $STATUS, not closed"; exit 1 ;;
    done) : ;; # Continue waiting for verification and merge.
    *) : ;;    # Other transient states remain bounded by the deadline.
  esac
  sleep 5
done
echo "timed out waiting for task $TASK_ID to close"
exit 1
```

If automatic merging is not enabled and a completed task needs a manual merge, select it explicitly (the task must still be `done` and its agent branch must exist):

```bash
bernstein merge pick --pick "$TASK_ID"
bernstein --json list-tasks \
  | jq --arg id "$TASK_ID" '.[] | select(.id == $id)'
```

The second command checks the task's actual state; do not assume the merge invocation itself moved it to `closed`. The bare `bernstein merge` group prints help when no selection is supplied.
