# WORKFLOW: CI Failure Auto-Routing to Responsible Agent
**Version**: 0.1
**Date**: 2026-03-28
**Author**: Workflow Architect
**Status**: Approved
**Implements**: 334f — CI Failure Auto-Routing to Responsible Agent

---

## Overview

When GitHub Actions reports a failed workflow run, Bernstein parses the CI log,
identifies which files triggered the failure, traces those files to the most
recent agent task that touched them, and creates a targeted fix task pre-loaded
with the CI log and agent diff as context. The fix task auto-retries up to 3
times with model/effort escalation before entering quarantine.

---

## Actors

| Actor | Role in this workflow |
|---|---|
| GitHub Actions | Executes CI; sends `workflow_run` webhook on failure |
| GitHub Webhook route | Receives, verifies, parses the webhook event |
| CI Failure Mapper | New mapper function — parses CI log, attributes blame, builds fix task |
| GitHub API | Provides CI log download URL; provides commit diff for the run |
| Task store | Persists the newly-created fix task |
| Orchestrator | Picks up the fix task and spawns an agent |
| Agent | Executes the fix task |
| Quarantine store | Records tasks that exhausted all retries |

---

## Prerequisites

- `GITHUB_WEBHOOK_SECRET` env var set (for HMAC verification)
- `GITHUB_TOKEN` env var set (for CI log download via GitHub API)
- Task server running and reachable at `http://127.0.0.1:8052`
- Git history available in the working directory (for `git log --diff-filter` attribution)

---

## Trigger

GitHub Actions sends a `workflow_run` webhook event with:
- Header: `X-GitHub-Event: workflow_run`
- `action = "completed"`
- `payload.workflow_run.conclusion = "failure"`

Endpoint: `POST /webhooks/github`

---

## Workflow Tree

### STEP 1: Receive and Verify Webhook
**Actor**: Webhook route (`src/bernstein/core/routes/webhooks.py`)
**Action**: Receive POST body, verify HMAC-SHA256 signature against `GITHUB_WEBHOOK_SECRET`
**Timeout**: 5s (FastAPI request handling)
**Input**: `{ headers: dict, body: bytes }`
**Output on SUCCESS**: Verified body, parsed `WebhookEvent(event_type="workflow_run", action="completed")` → GO TO STEP 2
**Output on FAILURE**:
  - `FAILURE(missing_secret_header)`: No `X-Hub-Signature-256` header and secret is configured → return 401, no task created
  - `FAILURE(invalid_signature)`: HMAC mismatch → return 401, no task created
  - `FAILURE(invalid_json)`: Body is not valid JSON → return 400, no task created
  - `FAILURE(missing_repo)`: `repository.full_name` absent → return 400, no task created

**Observable states during this step**:
  - Operator sees: nothing (webhook is internal)
  - Database: no change yet
  - Logs: `[webhooks] workflow_run webhook received repo=owner/repo`

---

### STEP 2: Filter — Is This a Failure Worth Routing?
**Actor**: CI Failure Mapper (new function `ci_failure_to_task` in `src/bernstein/github_app/mapper.py`)
**Action**: Check event qualifications:
  1. `event.event_type == "workflow_run"`
  2. `event.action == "completed"`
  3. `payload.workflow_run.conclusion == "failure"`
  4. `payload.workflow_run.name` is not in the skip-list (e.g. `"deploy-prod"` — deployments route separately)
**Timeout**: <1ms (pure logic, no I/O)
**Input**: `WebhookEvent`
**Output on SUCCESS**: Qualified failure event → GO TO STEP 3
**Output on FAILURE**:
  - `FAILURE(not_a_failure)`: `conclusion != "failure"` (e.g. success, cancelled) → return early, no task created, return 200 `{"tasks_created": 0}`
  - `FAILURE(skip_list_match)`: Workflow name is in the configured skip-list → return early, 200 `{"tasks_created": 0}`

**Observable states**:
  - Logs: `[ci_mapper] Skipping workflow_run: conclusion=success` OR `[ci_mapper] Qualifying failure: run_id=... workflow=...`

---

### STEP 3: Fetch CI Log
**Actor**: CI Failure Mapper
**Action**: Call GitHub API to get the log download URL for the failed run, then download the log ZIP and extract the relevant job log text.
  - `GET https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}/logs`
    → redirects to a ZIP download URL (or returns 302)
  - Download and unzip; extract lines from the failing job's log file
  - Truncate to last 4000 characters (tail of log — where failures appear)
**Timeout**: 15s total (3s for URL fetch + 12s for log download)
**Input**: `{ repo_full_name: str, run_id: int, token: str }`
**Output on SUCCESS**: `{ log_text: str, job_name: str, run_url: str }` → GO TO STEP 4
**Output on FAILURE**:
  - `FAILURE(api_403)`: Token lacks `actions:read` scope → ABORT_DEGRADED (create fix task without log context; set flag `ci_log_unavailable=true`)
  - `FAILURE(api_404)`: Run ID not found (race: run deleted before webhook processed) → return 200 `{"tasks_created": 0, "reason": "run_not_found"}`
  - `FAILURE(timeout)`: Log download takes > 15s → ABORT_DEGRADED
  - `FAILURE(zip_parse_error)`: Log ZIP is malformed → ABORT_DEGRADED

**Observable states**:
  - Logs: `[ci_mapper] Fetching CI log run_id=... repo=...`
  - Logs (failure): `[ci_mapper] CI log fetch failed: 403 — creating degraded fix task`

---

### STEP 4: Parse Failing Files from Log
**Actor**: CI Failure Mapper
**Action**: Apply regex patterns to the log text to extract failing file paths:
  - Pytest: `FAILED tests/path/to/test_file.py::TestClass::test_method`
  - Ruff: `src/path/to/file.py:42:1: E501 ...`
  - Pyright: `src/path/to/file.py:42:5 - error: ...`
  - Generic: any line matching `^\s*(FAILED|ERROR|error)\s+(\S+\.py)`
  Deduplicate and normalise to repo-relative paths.
**Timeout**: <100ms (pure string processing)
**Input**: `{ log_text: str }`
**Output on SUCCESS**: `{ failing_files: list[str] }` (may be empty list) → GO TO STEP 5
**Output on FAILURE**:
  - `FAILURE(no_files_parsed)`: No file paths extracted from log → GO TO STEP 5 with `failing_files=[]` (workflow continues with blame-free fix task)

**Observable states**:
  - Logs: `[ci_mapper] Parsed N failing files from CI log: [file1, file2, ...]`

---

### STEP 5: Attribute Blame via Git History
**Actor**: CI Failure Mapper
**Action**: For each failing file, run `git log --oneline -10 -- {file}` against the repo to find recent commits. Cross-reference commit SHAs against the `workflow_run.head_sha` and recent task store entries (tasks with `status=done` and `assigned_agent` set) to find the most recently responsible agent/task. Attribution rules (in order):
  1. **Exact SHA match**: task whose `result_summary` or description mentions the commit SHA that introduced the failing file change
  2. **File ownership match**: task whose `owned_files` list contains the failing file
  3. **Recency fallback**: most recent `done` task whose `assigned_agent` is not null, created within the last 2 hours
  4. **No attribution**: if none of the above match → `responsible_agent=None`, `responsible_task_id=None`
**Timeout**: 5s (git log is local; task store query is in-memory)
**Input**: `{ failing_files: list[str], repo_path: Path, task_store: TaskStore }`
**Output on SUCCESS**: `{ responsible_agent: str | None, responsible_task_id: str | None, blamed_files: list[str] }` → GO TO STEP 6
**Output on FAILURE**:
  - `FAILURE(git_not_available)`: git command fails → continue with `responsible_agent=None`

**Observable states**:
  - Logs: `[ci_mapper] Blamed N files → agent={agent_id} task={task_id}` OR `[ci_mapper] No blame attribution found`

---

### STEP 6: Check Retry Count
**Actor**: CI Failure Mapper
**Action**: Check if a CI fix task already exists for this `run_id` or the same `head_sha` + `workflow_name`. Query task store for tasks with `[CI-FIX]` prefix in title matching this run. Count existing retries.
**Timeout**: <50ms (in-memory task store query)
**Input**: `{ run_id: int, head_sha: str, workflow_name: str, task_store: TaskStore }`
**Output on SUCCESS**: `{ existing_retries: int }` → GO TO STEP 7
**Output on FAILURE**:
  - Any error → assume `existing_retries=0` (conservative: allow the fix task creation)

---

### STEP 7: Check Retry Cap
**Actor**: CI Failure Mapper
**Action**: Compare `existing_retries` against `MAX_CI_RETRIES = 3`.
**Timeout**: <1ms
**Input**: `{ existing_retries: int }`
**Output on SUCCESS** (`existing_retries < 3`): → GO TO STEP 8
**Output on FAILURE** (`existing_retries >= 3`):
  - Record in quarantine: `quarantine.record_failure(f"CI:{workflow_name}:{head_sha}", "Max CI retries exhausted")`
  - Log: `[ci_mapper] CI failure max retries (3) exhausted for run_id=... — quarantined`
  - Return 200 `{"tasks_created": 0, "reason": "max_ci_retries_exhausted"}`
  - **No fix task created.** Operator must intervene manually.

---

### STEP 8: Build and Create Fix Task
**Actor**: CI Failure Mapper → Task store
**Action**: Assemble the fix task payload and POST to task server.

Task payload:
```python
{
    "title": f"[CI-FIX][RETRY {existing_retries}] {workflow_name} failure on {head_sha[:8]}"[:120],
    "description": (
        f"CI workflow '{workflow_name}' failed on commit {head_sha}.\n"
        f"Run: {run_url}\n\n"
        f"Failing files:\n{failing_files_list}\n\n"
        f"CI log (tail):\n```\n{log_text[-4000:]}\n```\n\n"
        f"Agent diff context:\n```diff\n{agent_diff[:3000]}\n```\n\n"
        f"Fix the CI failures. Run the full test suite before completing."
    ),
    "role": _role_from_failing_files(failing_files),  # qa if tests/, backend otherwise
    "priority": 1,  # CI failures are always critical
    "scope": "small",
    "task_type": "fix",
    "model": _escalate_model(existing_retries),  # sonnet→opus on retry 2+
    "effort": _escalate_effort(existing_retries),  # high→max on retry 2+
    "owned_files": failing_files,
}
```

Model/effort escalation:
- `existing_retries == 0`: model=`sonnet`, effort=`high`
- `existing_retries == 1`: model=`sonnet`, effort=`max`
- `existing_retries == 2`: model=`opus`, effort=`max`

**Timeout**: 5s (HTTP POST to task server)
**Input**: Full task payload dict
**Output on SUCCESS**: `{ task_id: str }` → GO TO STEP 9
**Output on FAILURE**:
  - `FAILURE(server_unavailable)`: Task server not reachable → retry 3× with 2s backoff → if still failing, log error and return 500

**Observable states**:
  - Operator dashboard: new fix task appears in `open` state, priority=1 (critical)
  - Database: task record created with `status=open`
  - Logs: `[ci_mapper] Created CI fix task {task_id} for run {run_id}`

---

### STEP 9: Return Webhook Response
**Actor**: Webhook route
**Action**: Return 200 with summary of tasks created.
**Timeout**: <10ms
**Output**: `{ "event_type": "workflow_run", "action": "completed", "tasks_created": 1, "task_ids": [task_id] }`

---

### ABORT_DEGRADED: Create Fix Task Without Log Context
**Triggered by**: STEP 3 failure modes (log fetch fails)
**Actions**:
  1. Set `log_text = "(CI log unavailable — fetch failed)"`
  2. Set `failing_files = []`
  3. Continue from STEP 5 (skip to STEP 6 with empty failing_files)
  4. Create fix task with degraded description noting log unavailability
  5. Tag task description with `[LOG_UNAVAILABLE]` so agent knows to fetch manually
**What operator sees**: Fix task created, dashboard shows `[LOG_UNAVAILABLE]` marker in title

---

## State Transitions

```
[ci_failure_detected]
  → (conclusion != failure OR skip_list)          → [ignored, no task]
  → (run not found)                               → [ignored, no task]
  → (max_ci_retries >= 3)                         → [quarantined]
  → (task created)                                → [fix_task:open]

[fix_task:open]
  → (agent claims)                                → [fix_task:claimed]

[fix_task:claimed]
  → (agent starts working)                        → [fix_task:in_progress]
  → (claim timeout)                               → [fix_task:open]  # orchestrator reclaims

[fix_task:in_progress]
  → (agent fixes CI, marks complete)              → [fix_task:done]
  → (agent fails)                                 → [fix_task:failed]

[fix_task:failed]
  → (existing_retries < 3, next CI run fails)     → [fix_task:open] (new task, RETRY N+1)
  → (existing_retries >= 3)                       → [quarantined]
```

---

## Handoff Contracts

### GitHub Actions → Webhook Route

**Endpoint**: `POST /webhooks/github`
**Headers**:
```
X-GitHub-Event: workflow_run
X-Hub-Signature-256: sha256={hmac_digest}
Content-Type: application/json
```
**Payload** (relevant fields):
```json
{
  "action": "completed",
  "workflow_run": {
    "id": 12345678,
    "name": "CI",
    "head_sha": "abc1234...",
    "conclusion": "failure",
    "html_url": "https://github.com/owner/repo/actions/runs/12345678",
    "logs_url": "https://api.github.com/repos/owner/repo/actions/runs/12345678/logs"
  },
  "repository": {
    "full_name": "owner/repo"
  },
  "sender": {
    "login": "github-actions[bot]"
  }
}
```
**Success response**: `200 OK { "tasks_created": 1, "task_ids": ["abc123"] }`
**Failure response**: `401 { "detail": "Invalid webhook signature" }` | `400 { "detail": "Bad webhook payload: ..." }`
**Timeout**: FastAPI request timeout (30s default)

---

### Webhook Route → GitHub API (CI Logs)

**Endpoint**: `GET https://api.github.com/repos/{owner}/{repo}/actions/runs/{run_id}/logs`
**Headers**:
```
Authorization: Bearer {GITHUB_TOKEN}
Accept: application/vnd.github+json
X-GitHub-Api-Version: 2022-11-28
```
**Success response**: `302 Location: {zip_download_url}` → follow redirect, download ZIP
**Failure response**:
```json
{ "message": "Resource not accessible by integration", "status": "403" }
```
**Timeout**: 15s total
**On failure**: ABORT_DEGRADED

---

### CI Failure Mapper → Task Server

**Endpoint**: `POST http://127.0.0.1:8052/tasks`
**Payload**: (see STEP 8 task payload above)
**Success response**:
```json
{ "id": "abc123def456", "status": "open", ... }
```
**Failure response**:
```json
{ "detail": "Validation error: ..." }
```
**Timeout**: 5s
**On failure**: Retry 3× with 2s backoff. If still failing, log error, return 500 to GitHub webhook.

---

## Cleanup Inventory

This workflow creates tasks but does not create resources that require cleanup on failure. The task server handles its own persistence.

| Resource | Created at step | Destroyed by | Destroy method |
|---|---|---|---|
| Fix task (open) | Step 8 | If agent fixes CI → `done`; if max retries → `failed` + quarantine | Task status update |

No external resources (cloud, DNS, cache) are created by this workflow.

---

## New Components Required

### 1. `ci_failure_to_task()` in `src/bernstein/github_app/mapper.py`

New mapper function handling `event_type == "workflow_run"`. Steps 2–8 above.

**Signature**:
```python
async def ci_failure_to_task(
    event: WebhookEvent,
    *,
    github_token: str,
    task_store: TaskStore,
    repo_path: Path,
) -> dict[str, Any] | None:
    ...
```

Returns task payload dict or `None` (no task to create).

### 2. `_fetch_ci_log()` helper in `src/bernstein/github_app/mapper.py`

```python
async def _fetch_ci_log(
    repo_full_name: str,
    run_id: int,
    token: str,
    *,
    timeout_s: float = 15.0,
) -> tuple[str, str]:  # (log_text, job_name)
    ...
```

Uses `httpx.AsyncClient` to:
1. GET the logs URL (follow redirect)
2. Download and unzip the response
3. Return the concatenated log text of all failing jobs

### 3. `_parse_failing_files()` helper

```python
def _parse_failing_files(log_text: str) -> list[str]:
    ...
```

Regex patterns for pytest, ruff, pyright, and generic `FAILED`/`ERROR` lines.

### 4. `_attribute_blame()` helper

```python
def _attribute_blame(
    failing_files: list[str],
    task_store: TaskStore,
    repo_path: Path,
) -> tuple[str | None, str | None]:  # (agent_id, task_id)
    ...
```

### 5. Wire into webhook route

In `src/bernstein/core/routes/webhooks.py`, add `workflow_run` branch:

```python
elif event.event_type == "workflow_run" and event.action == "completed":
    ci_task = await ci_failure_to_task(
        event,
        github_token=os.environ.get("GITHUB_TOKEN", ""),
        task_store=store,
        repo_path=Path(settings.workdir),
    )
    if ci_task is not None:
        task_payloads.append(ci_task)
```

---

## Reality Checker Findings

| # | Finding | Severity | Spec section affected | Resolution |
|---|---|---|---|---|
| RC-1 | Webhook route uses sync `TaskCreate` creation; CI mapper needs async for log fetch | High | STEP 3, New Components | `ci_failure_to_task` must be `async`; webhook route already uses `await store.create(...)` so this fits |
| RC-2 | Task model has no `ci_run_id` field — retry deduplication in STEP 6 requires searching task titles | Medium | STEP 6 | Use `[CI-FIX]` prefix + SHA substring in title as dedup key. No model change needed for v1 |
| RC-3 | `push_to_tasks()` in mapper creates QA verify task on every push — a CI fix task and a push-verify task may duplicate effort | Low | STEP 2 | Acceptable overlap in v1; push-verify and CI-fix serve different purposes |
| RC-4 | GitHub API `logs` endpoint returns a ZIP; Python stdlib `zipfile` can parse in-memory. httpx async client must follow redirect. | Medium | STEP 3 | `_fetch_ci_log()` must use `follow_redirects=True` |
| RC-5 | `GITHUB_TOKEN` is not currently defined or validated at startup — missing env var causes silent 403s | High | Prerequisites | Add startup validation: log warning if `GITHUB_TOKEN` is absent |
| RC-6 | No existing test for `workflow_run` mapper path | High | Test Cases | Add `tests/unit/test_ci_failure_routing.py` (see Test Cases below) |

---

## Test Cases

| Test | Trigger | Expected behavior |
|---|---|---|
| TC-01: Happy path, pytest failure | Valid webhook, log contains `FAILED tests/unit/test_foo.py::test_bar`, file owned by recent task | Fix task created, `role=qa`, `priority=1`, log embedded in description |
| TC-02: Not a failure | `conclusion=success` | No task created, 200 returned |
| TC-03: Invalid HMAC | Signature mismatch | 401 returned, no task created |
| TC-04: Log fetch 403 | GitHub token lacks `actions:read` | Fix task created with `[LOG_UNAVAILABLE]` marker, degraded description |
| TC-05: Log fetch timeout | GitHub API takes > 15s | ABORT_DEGRADED — fix task created with degraded description |
| TC-06: No file attribution | Log has no parseable failing files | Fix task created with `owned_files=[]`, role=`backend` |
| TC-07: Max retries exceeded | 3 CI fix tasks already exist for this SHA + workflow | No new task, quarantine entry recorded, 200 returned |
| TC-08: Ruff linting failure | Log contains `src/foo.py:10:1: E501` | Fix task created, `owned_files=["src/foo.py"]`, `role=backend` |
| TC-09: Model escalation on retry 2 | `existing_retries=2` | Fix task created with `model=opus`, `effort=max` |
| TC-10: Skip-list workflow | `workflow_name="deploy-prod"` in skip-list | No task created, 200 returned |
| TC-11: Run not found (404) | `run_id` returns 404 from GitHub API | No task created, 200 with `reason=run_not_found` |
| TC-12: Concurrent duplicate webhooks | Same `run_id` delivered twice simultaneously | Only one fix task created (idempotency via title dedup check) |

---

## Assumptions

| # | Assumption | Where verified | Risk if wrong |
|---|---|---|---|
| A1 | GitHub token has `actions:read` scope for log download | Not verified — must be documented in setup | Silent 403, ABORT_DEGRADED path triggered |
| A2 | Task store is queryable by title prefix within the mapper (passed as argument) | Verified: `TaskStore` accessible from request state in webhook route | If not passed, STEP 6 cannot deduplicate |
| A3 | `zipfile` can parse GitHub Actions log ZIP in memory without writing to disk | Not verified in tests | MemoryError on very large logs; mitigation: 4000-char truncation applied before storage |
| A4 | `workflow_run.logs_url` points to the same GitHub API endpoint regardless of repo visibility | Assumed from GitHub docs | Private repos may require different auth scope |
| A5 | The working directory used for `git log` is the same repo the webhook references | Verified: `settings.workdir` is the project root | Wrong if multi-repo setup — addressed in future `repo` field on Task |
| A6 | CI fix tasks are never spawned from a `workflow_run` event that is itself triggered by a previous CI fix commit — preventing infinite CI fix loops | Not enforced | Infinite loop. Mitigation: the 3-retry cap (STEP 7) is the hard stop |

## Open Questions

- Should the fix task be assigned directly to the responsible agent (if still alive), or queued open for any available agent of the right role?
  - **Current decision**: Queue open. Agent reuse would require the orchestrator to interrupt an agent's current work — too complex for v1.
- Should the CI log be stored separately in `.sdd/ci_logs/{run_id}.txt` to avoid bloating task descriptions?
  - **Current decision**: Embed in description (truncated to 4000 chars). Revisit if task descriptions become unwieldy.
- What is the configured skip-list for workflow names? Who maintains it?
  - **Current decision**: Hard-coded list `["deploy-prod", "release"]` in mapper. Make configurable in v2.

## Spec vs Reality Audit Log

| Date | Finding | Action taken |
|---|---|---|
| 2026-03-28 | Initial spec created | — |
