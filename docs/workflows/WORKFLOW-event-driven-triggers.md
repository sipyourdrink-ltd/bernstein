# WORKFLOW: Event-Driven Agent Triggers
**Version**: 0.1
**Date**: 2026-03-29
**Author**: Workflow Architect
**Status**: Review
**Implements**: 510b — Event-Driven Agent Triggers (Cursor Automations-style)

---

## Overview

Bernstein spawns agents in response to external events — git pushes, CI failure webhooks, Slack messages, cron schedules, and file-watch patterns — without requiring manual `bernstein run` invocation. A unified **TriggerManager** evaluates incoming events against user-defined trigger rules, applies filters and conditions, deduplicates, and creates tasks on the task server. The orchestrator picks up these tasks in its normal tick loop. This is the single highest-impact UX improvement: orchestration "just works" when things happen.

---

## Actors

| Actor | Role in this workflow |
|---|---|
| External event source | Produces raw events (GitHub, filesystem, cron scheduler, Slack, generic webhook) |
| TriggerManager | Central coordinator — receives events, evaluates trigger rules, creates tasks |
| TriggerSource (per-type) | Adapter that converts a raw event into a normalized `TriggerEvent` |
| Trigger rule engine | Evaluates conditions, filters, and deduplication against the normalized event |
| Task server | Persists created tasks at `http://127.0.0.1:8052` |
| Orchestrator | Picks up open tasks in its tick loop and spawns agents |
| Agent | Executes the triggered task |
| Notification manager | Sends outbound notifications on trigger fire (optional) |

---

## Prerequisites

- Bernstein server running (`http://127.0.0.1:8052/health` returns 200)
- Trigger configuration file exists at `.sdd/config/triggers.yaml`
- For **GitHub triggers**: `GITHUB_WEBHOOK_SECRET` env var set; webhook endpoint exposed (via ngrok, Cloudflare Tunnel, or public server)
- For **Slack triggers**: `SLACK_BOT_TOKEN` and `SLACK_SIGNING_SECRET` env vars set
- For **cron triggers**: Orchestrator running (cron is evaluated within the orchestrator tick loop)
- For **file-watch triggers**: Orchestrator running on a machine with filesystem access to the watched paths
- For **generic webhook triggers**: Webhook endpoint exposed at `/webhooks/trigger`

---

## Trigger Configuration Schema

All triggers are defined in `.sdd/config/triggers.yaml`. This is the single source of truth for what events create tasks.

```yaml
# .sdd/config/triggers.yaml
version: 1

triggers:
  # --- Git Push Trigger ---
  - name: "qa-on-push"
    source: github_push
    enabled: true
    filters:
      branches: ["main", "develop"]          # Only these branches
      paths: ["src/**", "tests/**"]          # Only when these paths change
      exclude_paths: [".sdd/**", "docs/**"]  # Never trigger for these
      exclude_senders: ["bernstein[bot]"]    # Prevent infinite loops
    conditions:
      min_commits: 1                         # At least 1 commit in the push
      cooldown_s: 60                         # Don't re-trigger within 60s of last fire
    task:
      title: "QA verify push to {branch} ({sha_short})"
      role: qa
      priority: 2
      scope: small
      task_type: standard
      description_template: |
        Commits pushed to {branch}:
        {commit_messages}

        Changed files: {changed_files}
        Run the test suite and verify nothing is broken.

  # --- CI Failure Trigger ---
  - name: "ci-fix"
    source: github_workflow_run
    enabled: true
    filters:
      conclusion: failure
      workflow_names: ["CI", "Tests", "Lint"]
      exclude_workflow_names: ["deploy-prod", "release"]
    conditions:
      max_retries: 3                         # Stop after 3 fix attempts
      cooldown_s: 30
    task:
      title: "[CI-FIX] {workflow_name} failure on {sha_short}"
      role: auto                             # Inferred from failing files
      priority: 1
      scope: small
      task_type: fix
      model_escalation:                      # Model escalation on retries
        0: { model: sonnet, effort: high }
        1: { model: sonnet, effort: max }
        2: { model: opus, effort: max }

  # --- Cron Schedule Trigger ---
  - name: "nightly-evolve"
    source: cron
    enabled: true
    schedule: "0 2 * * *"                    # 2 AM daily
    conditions:
      skip_if_active: true                   # Don't fire if a task from this trigger is still active
    task:
      title: "Nightly evolution pass ({date})"
      role: manager
      priority: 3
      scope: medium
      task_type: research
      description_template: |
        Run a self-evolution cycle: review recent changes, identify improvements,
        and create follow-up tasks.

  # --- File Watch Trigger ---
  - name: "test-on-src-change"
    source: file_watch
    enabled: true
    filters:
      patterns: ["src/**/*.py"]
      exclude_patterns: ["src/**/__pycache__/**", "src/**/*.pyc"]
      events: [modified, created]            # Not deleted
    conditions:
      debounce_s: 10                         # Coalesce rapid changes
      cooldown_s: 120                        # Don't re-trigger within 2 min
    task:
      title: "Test affected modules ({changed_count} files changed)"
      role: qa
      priority: 2
      scope: small
      task_type: standard

  # --- Slack Message Trigger ---
  - name: "slack-task"
    source: slack
    enabled: true
    filters:
      channels: ["#bernstein-tasks"]
      mention_required: true                 # Must @bernstein
      message_pattern: "^@bernstein\\s+"     # Must start with @bernstein
    conditions:
      cooldown_s: 30
    task:
      title: "Slack request: {message_preview}"
      role: auto                             # Inferred from message content
      priority: 2
      scope: medium
      task_type: standard
      description_template: |
        From {sender} in {channel}:
        > {message_text}

  # --- Generic Webhook Trigger ---
  - name: "deploy-hook"
    source: webhook
    enabled: true
    filters:
      path: "/webhooks/trigger/deploy"       # Custom path suffix
      method: POST
      headers:
        X-Trigger-Secret: "{TRIGGER_SECRET}" # Env var interpolation
    conditions:
      cooldown_s: 300
    task:
      title: "Post-deploy verification ({environment})"
      role: qa
      priority: 1
      scope: medium
      task_type: standard
```

---

## Trigger

This workflow is itself triggered by **any** of the following external events:

| Source | Entry point | Protocol |
|---|---|---|
| GitHub push | `POST /webhooks/github` | GitHub webhook (existing endpoint) |
| GitHub workflow_run | `POST /webhooks/github` | GitHub webhook (existing endpoint) |
| GitHub issues | `POST /webhooks/github` | GitHub webhook (existing endpoint) |
| Slack message | `POST /webhooks/slack` | Slack Events API |
| Cron schedule | Orchestrator tick loop | Internal scheduler (no HTTP) |
| File-watch | Orchestrator tick loop | Filesystem observer (watchdog) |
| Generic webhook | `POST /webhooks/trigger/{path}` | HTTP POST with secret header |

---

## Workflow Tree

### STEP 1: Event Ingestion
**Actor**: TriggerSource (varies by source type)
**Action**: Receive raw event from external source and normalize it into a `TriggerEvent`.
**Timeout**: 5s (HTTP handlers); N/A for cron/file-watch (internal)
**Input**: Raw event (HTTP body, filesystem event, cron tick)
**Output on SUCCESS**: Normalized `TriggerEvent` → GO TO STEP 2

```python
@dataclass(frozen=True)
class TriggerEvent:
    source: str               # "github_push", "github_workflow_run", "slack", "cron", "file_watch", "webhook"
    timestamp: float          # Unix epoch
    raw_payload: dict[str, Any]
    # Normalized fields (source-specific, populated by TriggerSource adapter):
    repo: str | None          # "owner/repo" for GitHub sources
    branch: str | None        # For push/workflow_run
    sha: str | None           # Head SHA for GitHub sources
    sender: str | None        # Human or bot that caused the event
    changed_files: list[str]  # Files affected (push, file_watch)
    message: str | None       # Commit message, Slack message, webhook body summary
    metadata: dict[str, Any]  # Source-specific extras (workflow_name, channel, cron_name, etc.)
```

**Output on FAILURE**:
  - `FAILURE(invalid_signature)`: Webhook HMAC mismatch → return 401, no trigger evaluation
  - `FAILURE(invalid_payload)`: Missing required fields → return 400, log warning
  - `FAILURE(unknown_source)`: Event source not recognized → return 400, log warning
  - `FAILURE(source_disabled)`: Source type disabled in config → return 200 `{"triggers_fired": 0, "reason": "source_disabled"}`

**Observable states**:
  - Operator sees: nothing (internal)
  - Database: no change
  - Logs: `[trigger] Ingested {source} event from={sender} repo={repo}`

---

### STEP 2: Match Triggers
**Actor**: TriggerManager
**Action**: Load all trigger rules from `.sdd/config/triggers.yaml`. For each enabled trigger whose `source` matches the event source, evaluate the trigger's `filters` against the normalized `TriggerEvent`.

Filter evaluation per source type:

**github_push**:
  - `branches`: event.branch in trigger.filters.branches (glob match)
  - `paths`: any file in event.changed_files matches trigger.filters.paths (glob match)
  - `exclude_paths`: no file in event.changed_files matches trigger.filters.exclude_paths
  - `exclude_senders`: event.sender not in trigger.filters.exclude_senders

**github_workflow_run**:
  - `conclusion`: event.metadata.conclusion matches trigger.filters.conclusion
  - `workflow_names`: event.metadata.workflow_name in trigger.filters.workflow_names
  - `exclude_workflow_names`: event.metadata.workflow_name not in trigger.filters.exclude_workflow_names

**cron**:
  - No filters — cron triggers match by schedule, evaluated in STEP 2b

**file_watch**:
  - `patterns`: any changed file matches trigger.filters.patterns (glob match)
  - `exclude_patterns`: no changed file matches trigger.filters.exclude_patterns
  - `events`: file event type (created/modified/deleted) in trigger.filters.events

**slack**:
  - `channels`: event.metadata.channel in trigger.filters.channels
  - `mention_required`: if true, event.message contains `@bernstein`
  - `message_pattern`: event.message matches regex pattern

**webhook**:
  - `path`: event.metadata.request_path matches trigger.filters.path
  - `method`: event.metadata.request_method matches trigger.filters.method
  - `headers`: all specified headers present and match (with env var interpolation)

**Timeout**: <50ms (in-memory config, string matching)
**Input**: `TriggerEvent`, loaded trigger rules
**Output on SUCCESS**: `list[MatchedTrigger]` (0 or more matches) → GO TO STEP 3
  - If empty list: return early, 200 `{"triggers_fired": 0}`
**Output on FAILURE**:
  - `FAILURE(config_parse_error)`: triggers.yaml is malformed → log error, return 500, no triggers fire
  - `FAILURE(config_not_found)`: triggers.yaml missing → log warning, return 200 `{"triggers_fired": 0, "reason": "no_config"}`

**Observable states**:
  - Logs: `[trigger] Event matched {N} trigger(s): [{trigger_names}]`
  - Logs (no match): `[trigger] Event matched 0 triggers — no action`

---

### STEP 2b: Cron Schedule Evaluation (cron source only)
**Actor**: TriggerManager (within orchestrator tick)
**Action**: On each orchestrator tick, evaluate all enabled cron triggers. For each, check if the cron expression matches the current time (minute-level granularity). If a match is found and the trigger has not fired within this minute, synthesize a `TriggerEvent` with `source="cron"` and proceed to STEP 3.

**Implementation constraint**: Cron evaluation MUST be deterministic — no external scheduler process. The orchestrator tick loop (3s default) checks cron expressions against `datetime.now()`. A cron trigger fires at most once per minute-boundary that matches its expression.

**State tracking**: Last fire time per cron trigger stored in `.sdd/runtime/triggers/cron_state.json`:
```json
{
  "nightly-evolve": {
    "last_fired": 1711670400.0,
    "last_fire_minute": "2026-03-29T02:00"
  }
}
```

**Timeout**: <10ms per cron trigger evaluation
**Output on SUCCESS**: Synthesized `TriggerEvent` → GO TO STEP 3
**Output on FAILURE**:
  - `FAILURE(invalid_cron_expr)`: Cron expression unparseable → log error once, skip trigger, do not crash orchestrator

---

### STEP 2c: File Watch Event Collection (file_watch source only)
**Actor**: FileWatchSource (watchdog observer thread)
**Action**: A background `watchdog` observer watches configured paths. When filesystem events occur, they are buffered and debounced per trigger's `debounce_s`. After the debounce window closes with no new events, the coalesced batch is emitted as a single `TriggerEvent` with all affected files.

**Implementation constraint**: The watchdog observer runs as a daemon thread within the orchestrator process. It does NOT spawn a separate process. Events are queued to a `queue.SimpleQueue` and drained by the orchestrator tick.

**State tracking**: Debounce timers in memory (not persisted — lost on restart, acceptable).
**Timeout**: N/A (async, event-driven within the process)
**Output on SUCCESS**: Debounced `TriggerEvent` with coalesced `changed_files` → GO TO STEP 3
**Output on FAILURE**:
  - `FAILURE(watchdog_unavailable)`: `watchdog` package not installed → log warning at startup, disable all file_watch triggers
  - `FAILURE(permission_denied)`: Cannot watch target directory → log error, disable specific trigger
  - `FAILURE(too_many_watches)`: OS inotify/FSEvents limit hit → log error, disable specific trigger

**Observable states**:
  - Logs: `[trigger:file_watch] Observer started, watching {N} patterns`
  - Logs: `[trigger:file_watch] Debounce window closed: {N} files changed in {trigger_name}`

---

### STEP 3: Evaluate Conditions
**Actor**: TriggerManager
**Action**: For each matched trigger from STEP 2, evaluate its `conditions`:

1. **cooldown_s**: Check `.sdd/runtime/triggers/fire_log.jsonl` for the last fire time of this trigger name. If `now - last_fire < cooldown_s`, skip this trigger.
2. **max_retries** (CI triggers): Query task server for existing tasks with matching title prefix. If retry count >= max_retries, skip and log quarantine entry.
3. **skip_if_active** (cron triggers): Query task server for tasks created by this trigger name that are in `open`, `claimed`, or `in_progress` status. If any exist, skip.
4. **min_commits** (push triggers): Check `len(event.raw_payload.get("commits", []))` >= min_commits.
5. **debounce_s** (file_watch triggers): Already handled in STEP 2c — if we reach STEP 3, debounce has passed.

**Timeout**: <100ms (in-memory log check + task server query)
**Input**: `list[MatchedTrigger]`, trigger conditions, fire log, task server state
**Output on SUCCESS**: `list[QualifiedTrigger]` (triggers that pass all conditions) → GO TO STEP 4
  - If empty list: return early, log `[trigger] All matched triggers suppressed by conditions`
**Output on FAILURE**:
  - `FAILURE(task_server_unreachable)`: Cannot query task server for skip_if_active/max_retries check → **conservative: skip the trigger** (do not create tasks when state is unknown)
  - `FAILURE(fire_log_corrupt)`: Cannot read fire_log.jsonl → treat as no history (allow trigger to fire, log warning)

**Observable states**:
  - Logs: `[trigger] {trigger_name} suppressed by cooldown (last fired {N}s ago, cooldown={cooldown_s}s)`
  - Logs: `[trigger] {trigger_name} suppressed by max_retries ({existing_retries}/{max_retries})`
  - Logs: `[trigger] {trigger_name} suppressed by skip_if_active (task {task_id} still {status})`
  - Logs: `[trigger] {N} trigger(s) qualified after condition evaluation`

---

### STEP 4: Deduplicate
**Actor**: TriggerManager
**Action**: For each qualified trigger, compute a deduplication key:

```
dedup_key = hash(trigger_name + source + branch/channel/path + sha/timestamp_bucket)
```

Check `.sdd/runtime/triggers/dedup_cache.json` for this key. If present and not expired (TTL = max(cooldown_s, 300)), skip the trigger.

This handles:
- **Duplicate webhook deliveries**: GitHub retries webhooks on timeout; same push event arrives 2×
- **Rapid file saves**: Editor auto-save fires multiple modify events for the same file
- **Cron tick overlap**: Orchestrator tick may evaluate the same minute boundary twice

**Timeout**: <10ms
**Input**: `list[QualifiedTrigger]`, dedup cache
**Output on SUCCESS**: `list[DedupedTrigger]` → GO TO STEP 5
**Output on FAILURE**:
  - `FAILURE(dedup_cache_corrupt)`: Cannot read dedup cache → treat as empty (allow all triggers, log warning)

**Observable states**:
  - Logs: `[trigger] {trigger_name} deduplicated (key={dedup_key} seen {N}s ago)`

---

### STEP 5: Render Task Payloads
**Actor**: TriggerManager
**Action**: For each deduped trigger, render the task payload by interpolating the `task` template fields with values from the `TriggerEvent`:

Template variables available:
```
{branch}         — event.branch
{sha}            — event.sha (full)
{sha_short}      — event.sha[:8]
{sender}         — event.sender
{repo}           — event.repo
{changed_files}  — newline-joined list of changed files
{changed_count}  — len(event.changed_files)
{commit_messages} — newline-joined commit messages (push)
{workflow_name}  — event.metadata.workflow_name (CI)
{message_text}   — event.message (Slack)
{message_preview} — first 60 chars of event.message
{channel}        — event.metadata.channel (Slack)
{environment}    — event.metadata.environment (webhook)
{date}           — ISO date (cron)
{trigger_name}   — the trigger rule name
```

For `role: auto`:
  - Infer from changed files: `tests/` → `qa`, `docs/` → `docs`, `src/` → `backend`
  - Fallback: `backend`

For `model_escalation` (CI triggers):
  - Look up retry count, select model/effort from escalation table

Attach trigger metadata to task:
```python
task_payload["description"] += f"\n\n<!-- trigger: {trigger_name} source: {source} dedup: {dedup_key} -->"
```

**Timeout**: <10ms (string interpolation)
**Input**: `list[DedupedTrigger]`, `TriggerEvent`
**Output on SUCCESS**: `list[TaskCreate]` → GO TO STEP 6
**Output on FAILURE**:
  - `FAILURE(template_render_error)`: Missing variable in template → log error, skip this trigger, do not crash

**Observable states**:
  - Logs: `[trigger] Rendered {N} task payload(s) from trigger(s)`

---

### STEP 6: Create Tasks
**Actor**: TriggerManager → Task server
**Action**: POST each rendered task payload to `http://127.0.0.1:8052/tasks`. On success, record the fire event in `.sdd/runtime/triggers/fire_log.jsonl` and update the dedup cache.

**Fire log entry**:
```json
{
  "trigger_name": "qa-on-push",
  "source": "github_push",
  "fired_at": 1711670400.0,
  "task_id": "abc123def456",
  "dedup_key": "sha256:...",
  "event_summary": "push to main, 3 files changed"
}
```

**Timeout**: 5s per task creation (with 3× retry, 2s backoff)
**Input**: `list[TaskCreate]`
**Output on SUCCESS**: `list[str]` (task IDs) → GO TO STEP 7
**Output on FAILURE**:
  - `FAILURE(server_unavailable)`: Task server not reachable after 3 retries → log error, return 500 (for webhooks) or log and continue (for cron/file_watch)
  - `FAILURE(validation_error)`: Task payload rejected (400) → log error with payload details, skip this task, continue with others
  - `FAILURE(partial_create)`: Some tasks created, some failed → log partial results, return created task IDs + errors

**Observable states**:
  - Operator dashboard: new task(s) appear in `open` state
  - Database: task records created
  - Logs: `[trigger] Created task {task_id} from trigger {trigger_name}`
  - Fire log: new entry appended
  - Dedup cache: key inserted with TTL

---

### STEP 7: Return Response
**Actor**: TriggerManager → HTTP response (for webhook sources) / log entry (for cron/file_watch)
**Action**: Return summary of triggers evaluated and tasks created.

**For webhook sources** (HTTP response):
```json
{
  "source": "github_push",
  "triggers_evaluated": 3,
  "triggers_fired": 1,
  "triggers_suppressed": 2,
  "suppression_reasons": {
    "ci-fix": "cooldown",
    "lint-check": "no_filter_match"
  },
  "tasks_created": 1,
  "task_ids": ["abc123def456"]
}
```

**For cron/file_watch** (log only):
  - Logs: `[trigger] Cron trigger {trigger_name} fired → task {task_id}`
  - Logs: `[trigger] File watch trigger {trigger_name} fired ({N} files) → task {task_id}`

**Timeout**: <10ms
**Output**: HTTP 200 response / log entry

---

### ABORT_CLEANUP: Trigger System Failure
**Triggered by**: Config parse failure, TriggerManager initialization failure
**Actions**:
  1. Log error with full context
  2. Disable trigger system (set `.sdd/runtime/triggers/disabled` marker file)
  3. Continue orchestrator operation — triggers are disabled but manual task creation still works
  4. Notify operator if notification targets configured
**What operator sees**: Warning in `bernstein status` output: "Trigger system disabled: {reason}"
**What customer sees**: N/A (operator-facing system)

---

## State Transitions

```
[event_received]
  → (invalid signature/payload)                    → [rejected, no action]
  → (no matching triggers)                         → [ignored, no action]
  → (all triggers suppressed by conditions)        → [suppressed, no action]
  → (all triggers deduplicated)                    → [deduplicated, no action]
  → (task(s) created)                              → [task:open, awaiting orchestrator]

[task:open]
  → (orchestrator claims)                          → [task:claimed]

[task:claimed]
  → (agent spawned)                                → [task:in_progress]

[task:in_progress]
  → (agent completes)                              → [task:done]
  → (agent fails)                                  → [task:failed]

[task:failed]
  → (retryable + retries remain)                   → [task:open] (via retry logic)
  → (max retries exhausted)                        → [task:failed] (terminal, quarantine)

[trigger_system_error]
  → (config parse failure)                         → [trigger_system_disabled]
  → (operator fixes config)                        → [trigger_system_enabled]
```

---

## Handoff Contracts

### GitHub → Webhook Route (existing)

**Endpoint**: `POST /webhooks/github`
**Headers**: `X-GitHub-Event`, `X-Hub-Signature-256`, `Content-Type: application/json`
**Verification**: HMAC-SHA256 against `GITHUB_WEBHOOK_SECRET`
**Success response**: `200 OK { "triggers_evaluated": N, "triggers_fired": N, "task_ids": [...] }`
**Failure response**: `401 { "detail": "Invalid webhook signature" }` | `400 { "detail": "..." }`
**Timeout**: 30s (FastAPI default)

### Slack Events API → Slack Webhook Route (new)

**Endpoint**: `POST /webhooks/slack`
**Headers**: `X-Slack-Signature`, `X-Slack-Request-Timestamp`, `Content-Type: application/json`
**Verification**: HMAC-SHA256 using `SLACK_SIGNING_SECRET` with `v0:{timestamp}:{body}` as message
**Payload** (message event):
```json
{
  "type": "event_callback",
  "event": {
    "type": "message",
    "channel": "C1234567890",
    "user": "U1234567890",
    "text": "@bernstein fix the login bug",
    "ts": "1711670400.000100"
  }
}
```
**Special case — URL verification challenge**:
```json
{ "type": "url_verification", "challenge": "abc123" }
```
→ Return `200 { "challenge": "abc123" }` immediately. No trigger evaluation.

**Success response**: `200 OK { "triggers_fired": N, "task_ids": [...] }`
**Failure response**: `401 { "detail": "Invalid Slack signature" }` | `200 {}` (Slack requires 200 even on error to prevent retries)
**Timeout**: 3s (Slack requires response within 3 seconds; defer processing if needed)
**On timeout risk**: If trigger evaluation + task creation > 3s, respond 200 immediately and process asynchronously via background task queue.

### Generic Webhook → Trigger Route (new)

**Endpoint**: `POST /webhooks/trigger/{path}`
**Headers**: Configurable per trigger (e.g., `X-Trigger-Secret`)
**Verification**: Header value match against trigger config (with env var interpolation)
**Payload**: Arbitrary JSON — trigger config defines which fields to extract
**Success response**: `200 OK { "triggers_fired": N, "task_ids": [...] }`
**Failure response**: `401 { "detail": "Invalid trigger secret" }` | `400`
**Timeout**: 30s

### TriggerManager → Task Server

**Endpoint**: `POST http://127.0.0.1:8052/tasks`
**Payload**: Standard `TaskCreate` JSON (see task server API)
**Success response**: `201 { "id": "...", "status": "open", ... }`
**Failure response**: `400 { "detail": "..." }` | `500 { "detail": "..." }`
**Timeout**: 5s
**On failure**: Retry 3× with 2s exponential backoff → if still failing, log and skip

### FileWatchSource → TriggerManager (internal)

**Protocol**: `queue.SimpleQueue` within the orchestrator process
**Payload**: `TriggerEvent` with `source="file_watch"`, `changed_files` populated
**Timeout**: Non-blocking `.get_nowait()` — drained on each orchestrator tick
**On failure**: Queue overflow (>10000 events) → drain and discard oldest, log warning

### CronEvaluator → TriggerManager (internal)

**Protocol**: Direct function call within orchestrator tick
**Input**: Current `datetime`, list of cron triggers, cron state file
**Output**: `list[TriggerEvent]` (0 or more cron triggers that fire this minute)
**Timeout**: <10ms
**On failure**: Invalid cron expression → skip trigger, log error

---

## Cleanup Inventory

| Resource | Created at step | Destroyed by | Destroy method |
|---|---|---|---|
| Task record (open) | STEP 6 | Agent completion or manual cancel | Task status → done/cancelled |
| Fire log entry | STEP 6 | Log rotation (configurable retention) | Truncate fire_log.jsonl to last 10000 entries |
| Dedup cache entry | STEP 6 | TTL expiry | Periodic cleanup in orchestrator tick |
| Cron state file | STEP 2b | Trigger removal from config | Delete key from cron_state.json |
| Watchdog observer thread | STEP 2c startup | Orchestrator shutdown | `observer.stop()` + `observer.join()` |
| Disabled marker file | ABORT_CLEANUP | Operator intervention / config fix | Delete `.sdd/runtime/triggers/disabled` |

---

## New Components Required

### 1. `TriggerEvent` dataclass in `src/bernstein/core/models.py`

```python
@dataclass(frozen=True)
class TriggerEvent:
    source: str                          # "github_push", "github_workflow_run", "slack", "cron", "file_watch", "webhook"
    timestamp: float
    raw_payload: dict[str, Any]
    repo: str | None = None
    branch: str | None = None
    sha: str | None = None
    sender: str | None = None
    changed_files: tuple[str, ...] = ()  # Frozen tuple for hashability
    message: str | None = None
    metadata: FrozenDict[str, Any] = field(default_factory=FrozenDict)
```

### 2. `TriggerConfig` dataclass in `src/bernstein/core/models.py`

```python
@dataclass(frozen=True)
class TriggerConfig:
    name: str
    source: str
    enabled: bool = True
    filters: dict[str, Any] = field(default_factory=dict)
    conditions: dict[str, Any] = field(default_factory=dict)
    task: dict[str, Any] = field(default_factory=dict)
    schedule: str | None = None            # Cron expression (cron source only)
```

### 3. `TriggerManager` class in `src/bernstein/core/trigger_manager.py` (new file)

Central coordinator. Methods:
- `load_config(path: Path) -> list[TriggerConfig]`
- `evaluate(event: TriggerEvent) -> list[TaskCreate]`
- `check_cooldown(trigger_name: str, cooldown_s: int) -> bool`
- `check_dedup(dedup_key: str) -> bool`
- `record_fire(trigger_name: str, task_id: str, dedup_key: str) -> None`
- `evaluate_cron_triggers(now: datetime) -> list[TriggerEvent]`

### 4. `TriggerSource` protocol + adapters in `src/bernstein/core/trigger_sources/` (new directory)

```python
class TriggerSource(Protocol):
    def normalize(self, raw_event: dict[str, Any]) -> TriggerEvent: ...
    def matches_filter(self, event: TriggerEvent, filters: dict[str, Any]) -> bool: ...
```

Concrete implementations:
- `github_push.py` — adapts existing `push_to_tasks()` logic
- `github_workflow_run.py` — adapts existing `workflow_run_to_task()` logic
- `github_issues.py` — adapts existing `issue_to_tasks()` logic
- `slack.py` — new Slack Events API handler
- `cron.py` — cron expression evaluator (use `croniter` library)
- `file_watch.py` — watchdog observer wrapper
- `webhook.py` — generic webhook handler

### 5. New routes in `src/bernstein/core/routes/` (modify existing or new file)

- `POST /webhooks/slack` — Slack event ingestion + signature verification
- `POST /webhooks/trigger/{path:path}` — Generic webhook ingestion
- Modify existing `POST /webhooks/github` to route through TriggerManager instead of direct mapper calls

### 6. Orchestrator integration in `src/bernstein/core/orchestrator.py`

Add to tick loop:
```python
# In orchestrator tick, after task processing:
cron_events = self.trigger_manager.evaluate_cron_triggers(datetime.now())
for event in cron_events:
    tasks = self.trigger_manager.evaluate(event)
    for task in tasks:
        await self.task_client.create(task)

file_events = self.file_watch_source.drain_queue()
for event in file_events:
    tasks = self.trigger_manager.evaluate(event)
    for task in tasks:
        await self.task_client.create(task)
```

### 7. CLI commands in `src/bernstein/cli/`

- `bernstein triggers list` — Show all configured triggers and their status (enabled/disabled, last fired)
- `bernstein triggers fire <name>` — Manually fire a trigger (for testing)
- `bernstein triggers test <name> --event <json>` — Dry-run a trigger against a sample event
- `bernstein triggers history` — Show recent fire log

### 8. Runtime state files in `.sdd/runtime/triggers/`

- `fire_log.jsonl` — Append-only log of trigger fires
- `dedup_cache.json` — Dedup keys with TTLs
- `cron_state.json` — Last fire time per cron trigger
- `disabled` — Marker file indicating trigger system is disabled (presence = disabled)

---

## Infinite Loop Prevention

**Critical safety mechanism**: Agents create commits → commits trigger push events → push events trigger tasks → tasks spawn agents → agents create commits → ...

Prevention layers:

1. **exclude_senders filter**: Trigger config should exclude `bernstein[bot]` and agent commit authors from push triggers
2. **cooldown_s**: Even if sender filter fails, cooldown prevents rapid re-triggering
3. **skip_if_active**: Cron triggers won't fire if a previous task is still running
4. **max_retries**: CI fix triggers hard-cap at N retries
5. **Global rate limit**: TriggerManager enforces a global max of 20 tasks created per minute across all triggers. Exceeding this disables the trigger system and alerts the operator.
6. **Agent commit tagging**: All agent commits should include `[bernstein]` in the commit message; push triggers should have `exclude_commit_patterns: ["\\[bernstein\\]"]` in their filters

```yaml
# Default safety filters applied to ALL triggers
defaults:
  max_tasks_per_minute: 20        # Global rate limit
  max_tasks_per_trigger_per_hour: 50
  exclude_senders:
    - "bernstein[bot]"
    - "github-actions[bot]"
```

---

## Migration Path from Existing Webhook Handlers

The existing `mapper.py` functions (`issue_to_tasks`, `push_to_tasks`, `workflow_run_to_task`, etc.) continue to work as-is. The TriggerManager is an **additive layer**:

1. **Phase 1**: TriggerManager wraps existing mappers. GitHub webhooks still hit the same endpoint. TriggerManager checks for matching trigger rules first; if none configured, falls back to existing mapper behavior. Zero breaking changes.
2. **Phase 2**: Users define custom triggers in `triggers.yaml`. These run alongside the default mappers.
3. **Phase 3**: Default mappers migrated to trigger rules. Old mapper code deprecated but kept as TriggerSource adapters.

---

## Reality Checker Findings

| # | Finding | Severity | Spec section affected | Resolution |
|---|---|---|---|---|
| RC-1 | Existing `push_to_tasks()` in mapper.py has no branch filter or path filter — creates QA task on every push | Medium | STEP 2 filters | TriggerManager adds filters that existing mapper lacks; migration Phase 1 preserves old behavior |
| RC-2 | Existing webhook route is sync; Slack 3s response constraint may require async processing | High | Slack handoff contract | Slack route must respond 200 immediately, then process trigger asynchronously via `asyncio.create_task()` |
| RC-3 | No `watchdog` dependency in project currently | Medium | STEP 2c | Must add `watchdog` as optional dependency: `pip install bernstein[file-watch]` |
| RC-4 | Orchestrator tick is 3s default; cron evaluation needs minute-level granularity | Low | STEP 2b | 3s tick means cron evaluation runs ~20× per minute; cron_state.json prevents duplicate fires within same minute |
| RC-5 | `.sdd/runtime/` directory structure has no `triggers/` subdirectory yet | Low | STEP 6, state files | TriggerManager.init() must create `.sdd/runtime/triggers/` on first run |
| RC-6 | Existing `exclude_senders` is not implemented anywhere — push events from agents will trigger loops without this | Critical | Infinite loop prevention | Must be implemented in Phase 1 before any file_watch or push triggers are enabled |
| RC-7 | Task server has no field for tracking which trigger created a task | Medium | STEP 5, STEP 6 | Embed trigger metadata in task description as HTML comment: `<!-- trigger: name -->`. Future: add `trigger_name` field to Task model |
| RC-8 | No existing Slack integration (SLACK_BOT_TOKEN, SLACK_SIGNING_SECRET not referenced anywhere) | Low | Prerequisites | Slack triggers are Phase 2; document as optional feature |
| RC-9 | `croniter` is not in dependencies | Low | STEP 2b | Must add as dependency for cron evaluation |
| RC-10 | Global rate limit (max 20 tasks/min) has no current enforcement mechanism | High | Infinite loop prevention | Must implement rate counter in TriggerManager before any triggers are enabled |
| RC-11 | Orchestrator has `_should_trigger_manager_review()` method — naming collision with TriggerManager concept. That method escalates to a manager agent after N failures; unrelated to event triggers | Low | New components | Use distinct naming: `TriggerManager` for events, keep existing method name but add docstring clarification |
| RC-12 | `NotificationManager` already exists (`src/bernstein/core/notifications.py`) with Slack Block Kit, Discord, Telegram, and generic webhook support — but no `trigger.fired` event type | Medium | Actors, STEP 7 | Add `trigger.fired` to `NotificationEvent` literal type; wire into TriggerManager post-fire hook |
| RC-13 | Existing `NotificationManager` uses `httpx` for outbound HTTP — can be reused for Slack reply-in-channel (Open Question #4) without adding new HTTP client | Low | Slack handoff contract | Reuse `NotificationManager.send()` with Slack target for channel replies |

---

## Test Cases

| Test | Trigger | Expected behavior |
|---|---|---|
| TC-01: Push happy path | Valid push webhook, matching branch + paths | Task created with QA role, commit context in description |
| TC-02: Push filtered by branch | Push to `feature/x`, trigger only matches `main` | No task created, 200 returned |
| TC-03: Push filtered by path | Push changes only `docs/README.md`, trigger excludes `docs/**` | No task created |
| TC-04: Push filtered by sender | Push from `bernstein[bot]` | No task created (infinite loop prevention) |
| TC-05: Push cooldown | Two pushes within 60s, cooldown_s=60 | First creates task, second suppressed |
| TC-06: CI failure happy path | workflow_run failure, workflow_name in filter list | Fix task created with model escalation |
| TC-07: CI failure max retries | 3 existing fix tasks for same SHA | No task created, quarantine logged |
| TC-08: CI success ignored | workflow_run conclusion=success | No task created |
| TC-09: Cron fires at schedule | Current time matches "0 2 * * *" | Task created once per matching minute |
| TC-10: Cron skip_if_active | Cron fires but previous task still in_progress | No new task created |
| TC-11: Cron no double-fire | Orchestrator evaluates same minute twice (3s tick) | Task created only once (cron_state dedup) |
| TC-12: File watch single file | Single .py file modified | Task created after debounce window |
| TC-13: File watch debounce | 10 files modified in 5s, debounce_s=10 | Single task created with all 10 files listed |
| TC-14: File watch exclude pattern | `__pycache__` file modified | No task created |
| TC-15: Slack happy path | Message with @bernstein in configured channel | Task created with message content |
| TC-16: Slack wrong channel | Message in non-configured channel | No task created |
| TC-17: Slack no mention | Message without @bernstein, mention_required=true | No task created |
| TC-18: Slack URL verification | Slack sends challenge request | 200 + challenge response, no task |
| TC-19: Generic webhook happy path | POST to /webhooks/trigger/deploy with correct secret | Task created |
| TC-20: Generic webhook bad secret | POST with wrong X-Trigger-Secret header | 401 returned |
| TC-21: Duplicate webhook delivery | Same GitHub push delivered 2× within 5s | Only 1 task created (dedup) |
| TC-22: Config parse error | Malformed triggers.yaml | Trigger system disabled, log error, orchestrator continues |
| TC-23: Config missing | No triggers.yaml file | Trigger system inactive, no errors (graceful degradation) |
| TC-24: Task server down | Trigger qualifies but POST /tasks fails | Retry 3×, log error, no task created |
| TC-25: Global rate limit | 21 tasks created within 1 minute | 20th task created, 21st blocked, operator alerted |
| TC-26: Template render error | Trigger template references {nonexistent_var} | Task not created, error logged, other triggers unaffected |
| TC-27: Multiple triggers match | One push event matches 2 triggers (qa + lint) | 2 tasks created, each with distinct trigger metadata |
| TC-28: Disabled trigger | trigger.enabled=false | Trigger skipped in evaluation, no task |
| TC-29: File watch watchdog missing | `watchdog` not installed | File_watch triggers disabled at startup with warning |
| TC-30: Cron invalid expression | schedule: "invalid" | Trigger skipped with error log, other cron triggers unaffected |

---

## Assumptions

| # | Assumption | Where verified | Risk if wrong |
|---|---|---|---|
| A1 | Agent commits can be identified by commit author or message pattern | Not enforced — must add `[bernstein]` tagging convention | Infinite trigger loops on push events |
| A2 | `watchdog` library works on macOS (FSEvents), Linux (inotify), and within Docker containers | Verified: watchdog docs confirm cross-platform support | File-watch triggers broken on unsupported platforms |
| A3 | `croniter` library evaluates cron expressions deterministically within the orchestrator tick | Not verified in integration | Cron triggers may fire at wrong times or miss fires |
| A4 | Slack Events API delivers each event exactly once (no retries on 200 response) | Slack docs confirm: 200 = acknowledged | Duplicate task creation if Slack retries |
| A5 | `.sdd/runtime/triggers/` directory is writable by the orchestrator process | Assumed from `.sdd/runtime/` being writable | State files fail to write, triggers malfunction |
| A6 | Task server is on localhost (127.0.0.1:8052) — no network partition between TriggerManager and task server | Verified: current architecture runs both in same process or on same host | Task creation fails on network split in distributed mode |
| A7 | Global rate limit of 20 tasks/minute is sufficient for normal operation | Not validated against real-world usage | Legitimate high-activity periods may be throttled |
| A8 | Debounce window of 10s is sufficient for file-watch to coalesce editor save events | Based on typical editor autosave behavior | Rapid CI/CD pipelines may need shorter debounce |
| A9 | Dedup cache fits in memory (<10000 entries at any time) | Based on expected trigger frequency | Memory pressure if triggers fire at unexpected rate |
| A10 | Slack 3s response deadline can be met by deferring processing to background task | Standard Slack integration pattern | If asyncio task queue is full, trigger processing may be delayed |

---

## Open Questions

1. **Should triggers be hot-reloadable?** If `triggers.yaml` changes, should the TriggerManager pick up changes on the next tick, or require a restart?
   - **Proposed**: Hot-reload on each tick (re-read YAML if mtime changed). Low overhead, high convenience.

2. **Should trigger fire history be queryable via the task server API?** This would enable a "trigger dashboard" showing fire rates, suppressions, and errors.
   - **Proposed**: Phase 2. v1 uses fire_log.jsonl with CLI query (`bernstein triggers history`).

3. **Should file-watch triggers support remote filesystems (NFS, FUSE)?** Watchdog performance degrades on network filesystems.
   - **Proposed**: Document as unsupported. Recommend local filesystem or git-push triggers for remote repos.

4. **Should Slack trigger responses include a confirmation message in the channel?** (e.g., "Task created: QA verify push to main")
   - **Proposed**: Yes, via existing NotificationManager with a `slack` target. Add `reply_in_channel: true` option to Slack trigger config.

5. **Should generic webhooks support request body validation (JSON Schema)?** This would prevent malformed payloads from reaching the trigger engine.
   - **Proposed**: Phase 2. v1 trusts authenticated webhooks.

6. **What happens to in-flight file-watch events when the orchestrator restarts?** They are lost (in-memory queue).
   - **Proposed**: Acceptable for v1. File changes will still exist on disk; next git commit will trigger push event.

7. **Should there be a UI for managing triggers?** (VS Code extension panel, web dashboard)
   - **Proposed**: Phase 3. v1 is YAML-only configuration.

---

## Implementation Phases

### Phase 1: Core Framework + GitHub Triggers (MVP)
- `TriggerEvent` and `TriggerConfig` models
- `TriggerManager` with config loading, filter evaluation, condition checking, dedup
- GitHub TriggerSource adapters (wrap existing mapper functions)
- Fire log, dedup cache, cron state persistence
- Infinite loop prevention (exclude_senders, global rate limit)
- `bernstein triggers list/history` CLI commands
- Existing webhook route routes through TriggerManager
- All safety mechanisms active before any trigger is enabled

### Phase 2: Cron + File Watch + Slack
- Cron evaluation in orchestrator tick (add `croniter` dependency)
- File-watch source (add `watchdog` optional dependency)
- Slack Events API route + signature verification
- `bernstein triggers fire/test` CLI commands
- Hot-reload of triggers.yaml

### Phase 3: Generic Webhooks + Dashboard
- Generic webhook route with configurable auth
- Trigger fire rate dashboard (web UI extension)
- VS Code extension panel for trigger management
- JSON Schema validation for webhook payloads

---

## Spec vs Reality Audit Log

| Date | Finding | Action taken |
|---|---|---|
| 2026-03-29 | Initial spec created | — |
| 2026-03-29 | Reality check pass #2: verified RC-6 (Critical) and RC-10 (High) still unimplemented; discovered RC-11 (naming collision), RC-12 (NotificationManager exists but lacks trigger.fired event), RC-13 (httpx reuse for Slack replies) | Added RC-11–RC-13; promoted status Draft → Review |
