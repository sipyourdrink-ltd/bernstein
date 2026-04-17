# Runbook Templates

Operational runbooks for automated remediation of common agent failure patterns.

## Overview

Bernstein includes a runbook engine that pattern-matches agent error output against
known failure modes and suggests (or automatically executes) remediation actions. When
an agent fails with a recognized error, the `RunbookEngine` finds the first matching
rule and returns the suggested fix before the task is retried.

Runbooks are not LLM-driven. They use compiled regex patterns and deterministic
matching, so they add near-zero latency to the failure path.

Key concepts:

- **RunbookRule** -- a detect pattern (regex) paired with an action (shell command or instruction).
- **RunbookEngine** -- iterates rules in order, returns the first match, and tracks execution history.
- **RunbookMatch** -- the result of a successful match, with captured groups interpolated into the action string.

## Default rules

The built-in rules are loaded automatically when no custom configuration is provided.
They cover the most common agent failure patterns:

| Rule name           | Detect pattern                                                    | Action                                            | Auto-execute | Max retries |
|---------------------|-------------------------------------------------------------------|---------------------------------------------------|:------------:|:-----------:|
| `import_error`      | `ModuleNotFoundError: No module named '(\S+)'`                   | `pip install {module}`                            | No           | 1           |
| `lint_failure`      | `ruff check failed\|Ruff.*error\|ruff.*Found \d+ error`          | `ruff check --fix .`                              | Yes          | 2           |
| `port_conflict`     | `Address already in use\|EADDRINUSE.*:(\d+)\|port (\d+).*in use` | `lsof -ti:{port} \| xargs kill -9`               | No           | 1           |
| `type_error`        | `TypeError: .+ got an unexpected keyword argument '(\S+)'`       | Check function signature for argument `{module}`  | No           | 1           |
| `permission_denied` | `PermissionError\|Permission denied`                              | Check file permissions on affected paths          | No           | 1           |
| `git_conflict`      | `CONFLICT \(content\)\|merge conflict\|Merge conflict`           | Resolve merge conflicts in affected files         | No           | 1           |
| `rate_limit`        | `rate.?limit\|429\|Too Many Requests\|throttl`                   | Wait and retry with exponential backoff           | No           | 3           |
| `disk_space`        | `No space left on device\|ENOSPC\|disk full`                     | Free disk space: clean build artifacts, tmp files | No           | 1           |
| `timeout`           | `TimeoutError\|timed? ?out\|deadline exceeded`                   | Retry with increased timeout or reduced scope     | No           | 2           |
| `test_failure`      | `FAILED tests/\|pytest.*failed\|AssertionError`                  | Review test output and fix failing assertions     | No           | 2           |

Rules are evaluated in the order listed. The first match wins.

## Custom rules via JSON config

Override or extend the default rules by placing a JSON config file and loading it with
`RunbookEngine.load_rules()`:

```json
{
  "runbooks": [
    {
      "name": "oom_kill",
      "detect": "Killed|Out of memory|MemoryError",
      "action": "Reduce batch size or increase memory limit",
      "auto_execute": false,
      "max_retries": 1
    },
    {
      "name": "docker_build_fail",
      "detect": "docker build.*failed|Error response from daemon",
      "action": "docker system prune -f && docker build --no-cache .",
      "auto_execute": false,
      "max_retries": 2
    }
  ]
}
```

Save this as `.sdd/config/runbooks.json` and the engine picks it up on next run.
If the file is absent or malformed, the engine falls back to the built-in defaults.

### Field reference

| Field          | Type   | Required | Default | Description                                    |
|----------------|--------|----------|---------|------------------------------------------------|
| `name`         | string | yes      | --      | Unique identifier for the rule                 |
| `detect`       | string | yes      | --      | Regex pattern matched against agent error text |
| `action`       | string | yes      | --      | Shell command or human-readable instruction    |
| `auto_execute` | bool   | no       | `false` | If true, the action runs without confirmation  |
| `max_retries`  | int    | no       | `2`     | Maximum automatic retries for this failure     |

Capture groups in the `detect` regex are interpolated into the `action` string using
`{module}`, `{port}`, or `{file}` placeholders.

## Operational scenario templates

### Over-budget recovery

An orchestration run is approaching or has exceeded its spending cap.

**Indicators:**
- Token monitor reports cost > 80% of `budget_usd`
- Cost anomaly detector fires alerts

**Runbook steps:**

1. Check current spend: `GET /status` and inspect `cost_usd` in the response.
2. Identify the highest-cost agents from `.sdd/metrics/cost_ledger.jsonl`.
3. Reduce parallelism: lower `max_agents` in `bernstein.yaml` to slow burn rate.
4. Switch remaining tasks to cheaper models via `model_policy` overrides.
5. If budget is already exhausted, stop the run: `bernstein stop`.

**Custom rule example:**

```json
{
  "name": "budget_warning",
  "detect": "budget.*exceeded|cost.*limit|spending.*cap",
  "action": "Pause non-critical tasks and reduce max_agents to 2",
  "auto_execute": false,
  "max_retries": 1
}
```

### Agent crash loop

An agent repeatedly fails on the same task, burning retries without progress.

**Indicators:**
- Task retry count reaches `max_retries` (default 3)
- Same error pattern appears in consecutive attempts
- Runbook execution stats show repeated matches for the same rule

**Runbook steps:**

1. Check the task trace: `bernstein trace <task-id>`.
2. Look for repeated runbook matches in `.sdd/metrics/runbook_log.jsonl`.
3. If the error is environmental (disk, network, permissions), fix the root cause and retry.
4. If the error is in agent-generated code, mark the task as failed and create a simpler subtask.
5. Consider escalating to a different model or role.

**Custom rule example:**

```json
{
  "name": "crash_loop_breaker",
  "detect": "retry_count.*exceeded|max retries reached",
  "action": "Fail task permanently and notify operator",
  "auto_execute": false,
  "max_retries": 0
}
```

### Merge conflict storm

Multiple agents modify overlapping files, producing a burst of merge conflicts.

**Indicators:**
- `git_conflict` runbook rule fires for 3+ tasks within a short window
- Bulletin board shows multiple "blocker" entries referencing merge conflicts
- Janitor merge queue backs up

**Runbook steps:**

1. Pause new task assignments: reduce `max_agents` to 1 temporarily.
2. Drain the merge queue: let the janitor process pending merges one at a time.
3. Identify overlapping file scopes by checking `owned_files` on active tasks.
4. Reassign conflicting tasks to run sequentially (add `depends_on` relationships).
5. Resume normal parallelism after the conflict burst clears.

**Custom rule example:**

```json
{
  "name": "conflict_storm",
  "detect": "CONFLICT.*CONFLICT.*CONFLICT|multiple merge conflicts",
  "action": "Reduce max_agents to 1 and drain merge queue before resuming",
  "auto_execute": false,
  "max_retries": 1
}
```

### High failure rate

The overall task failure rate climbs above an acceptable threshold.

**Indicators:**
- Dashboard (`GET /status`) shows failure rate > 30%
- Multiple different runbook rules are firing across tasks
- Agent utilization drops as tasks pile up in failed state

**Runbook steps:**

1. Pull failure summary: `bernstein status` or `GET /status`.
2. Group failures by error pattern using `.sdd/metrics/runbook_log.jsonl`.
3. If failures cluster around a single cause (e.g., a broken dependency), fix that cause first.
4. If failures are diverse, check for environmental issues: disk space, network, API rate limits.
5. Consider stopping the run, fixing the environment, and restarting: `bernstein stop && bernstein run`.

**Custom rule example:**

```json
{
  "name": "high_failure_rate",
  "detect": "failure rate.*above threshold|too many failures",
  "action": "Stop run, audit .sdd/metrics, fix root cause, then restart",
  "auto_execute": false,
  "max_retries": 1
}
```

## Execution log

All runbook executions are persisted to `.sdd/metrics/runbook_log.jsonl` as newline-delimited
JSON. Each entry contains:

```json
{
  "rule_name": "lint_failure",
  "task_id": "task-abc123",
  "action": "ruff check --fix .",
  "timestamp": 1712345678.9,
  "success": true,
  "output": "Fixed 3 errors."
}
```

Use `RunbookEngine.get_stats()` to retrieve aggregated execution statistics
(total executions, successes, and failures grouped by rule name).
