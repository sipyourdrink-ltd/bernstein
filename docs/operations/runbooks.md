# Runbook Templates

Operational runbooks for automated remediation of common agent failure patterns.

## Overview

Bernstein includes a runbook engine for matching error output against known failure
modes. Callers can pass the error text to `RunbookEngine.match` to get the first
matching rule and its suggested fix. Matching itself does not execute the action.

Runbooks are not LLM-driven. They use compiled regex patterns and deterministic
matching, so they add near-zero latency to the failure path.

Key concepts:

- **RunbookRule** -- a detect pattern (regex) paired with an action (shell command or instruction).
- **RunbookEngine** -- searches its rules in order and returns the first match. Recording an action outcome is a separate operation.
- **RunbookMatch** -- the result of a successful match, with the first captured group available for action-text substitution.

## Default rules

The constructor uses the built-in rules when given no rules. The orchestrator
constructs the engine with those defaults. It does not invoke matching on the run
failure path or discover `.sdd/config/runbooks.json` automatically.
The built-in rules cover the most common agent failure patterns:

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

Rules are evaluated in listed order. Each regex searches anywhere in the error text and
matches case-insensitively; the first matching rule is returned as a `RunbookMatch`, even
if later rules also match. Matching only produces this record: it does not run the action
or record an execution outcome. Call `record_execution(...)` separately when an action
outcome is to be recorded.

For example, both candidate rules below match the same input, but `match` returns the
first one. The example only inspects the suggested action; it performs no action and
records no execution:

```python
from bernstein.core.observability.runbooks import RunbookEngine, RunbookRule

rules = [
    RunbookRule("package", r"Missing package: (\w+)", "install {module}"),
    RunbookRule("fallback", r"missing package", "review the dependency"),
]
engine = RunbookEngine(rules=rules)
error = "MISSING PACKAGE: demo_widget"

assert rules[0].matches(error) is not None
assert rules[1].matches(error) is not None
result = engine.match(error)
assert result is not None and result.rule.name == "package"
assert result.interpolated_action == "install demo_widget"
assert engine.executions == []
```

## Custom rules via JSON config

`RunbookEngine.load_rules(config_path)` explicitly loads a rule list from JSON; it does
not configure the engine by itself. Pass the returned list to `RunbookEngine(rules=...)`
to use it. A nonempty supplied list replaces (rather than extends) the built-in rules.
The constructor treats an empty list the same as no list and installs the built-ins.

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

Save this as `.sdd/config/runbooks.json`, then explicitly load it and construct the engine
with the returned rules if you want to use these custom rules. The file is not picked up
on the next run automatically. A missing or unreadable file, invalid JSON, or a
missing required field selects the built-ins. A valid file with an empty
`runbooks` list returns an empty list,
but passing that list to the engine constructor still selects the built-in defaults.

### Field reference

| Field          | Type   | Required | Default | Description                                    |
|----------------|--------|----------|---------|------------------------------------------------|
| `name`         | string | yes      | --      | Unique identifier for the rule                 |
| `detect`       | string | yes      | --      | Regex pattern matched against agent error text |
| `action`       | string | yes      | --      | Shell command or human-readable instruction    |
| `auto_execute` | bool   | no       | `false` | Flag carried by the rule for callers          |
| `max_retries`  | int    | no       | `2`     | Retry count carried by the rule for callers   |

The matching engine records these two fields as rule metadata. It does not execute an
action or schedule a retry from either field.

If a matching regex has capture groups, `RunbookMatch.interpolated_action` uses only its
first capture group, replacing `{module}`, `{port}`, and `{file}` placeholders in the
action text with that captured value. With no capture group (or an empty first group), the
action text is returned unchanged.

## Operational scenario templates

### Over-budget recovery

An orchestration run is approaching or has exceeded its spending cap.

**Indicators:**
- Token monitor reports cost > 80% of `budget_usd`
- Cost anomaly detector fires alerts

**Runbook steps:**

1. Check current spend: `GET /status` and inspect `cost_usd` in the response.
2. Identify the highest-cost agents from `.sdd/cost/ledger.jsonl`.
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
