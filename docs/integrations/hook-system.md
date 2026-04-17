# Hook System Developer Guide

Bernstein's hook system lets you run custom code in response to orchestration events. Hooks are synchronous or asynchronous callables registered for specific event types.

## Event taxonomy

All events are defined in `bernstein.core.config.hook_events.HookEvent` (also accessible via `bernstein.core.hook_events` through the lazy redirect in `core/__init__.py`):

### Task lifecycle

| Event | Fired when |
|-------|-----------|
| `task.created` | A new task is added to the backlog |
| `task.claimed` | An agent claims a task |
| `task.completed` | A task finishes successfully |
| `task.failed` | A task fails |
| `task.retried` | A failed task is retried |

### Agent lifecycle

| Event | Fired when |
|-------|-----------|
| `agent.spawned` | A new agent process starts |
| `agent.heartbeat` | An agent sends a heartbeat |
| `agent.completed` | An agent finishes its work |
| `agent.killed` | An agent is forcefully terminated |
| `agent.stalled` | An agent stops responding |

### Merge / git

| Event | Fired when |
|-------|-----------|
| `merge.started` | A merge operation begins |
| `merge.completed` | A merge finishes successfully |
| `merge.conflict` | A merge conflict is detected |

### Quality gates

| Event | Fired when |
|-------|-----------|
| `quality_gate.passed` | All quality checks pass |
| `quality_gate.failed` | A quality check fails |

### Budget

| Event | Fired when |
|-------|-----------|
| `budget.threshold` | Spending reaches a warning threshold |
| `budget.exceeded` | Budget limit is exceeded |

## Registering hooks

### In code

Hooks are dispatched via webhook or script execution, not via a Python event emitter. For blocking hooks that run inline, see `bernstein.core.security.blocking_hooks`. For webhook dispatch, see `bernstein.core.server.webhook_handler`.

Event types are defined in `bernstein.core.config.hook_events`:

```python
from bernstein.core.config.hook_events import HookEvent

# Available events:
HookEvent.TASK_COMPLETED   # "task.completed"
HookEvent.AGENT_KILLED     # "agent.killed"
# ... see full list in hook_events.py
```

### In configuration

```yaml
# bernstein.yaml
hooks:
  task.completed:
    - type: webhook
      url: "https://your-app.example.com/hooks/task-completed"
      secret: "hmac-secret"
    - type: script
      command: "python scripts/on_task_complete.py"
  agent.killed:
    - type: webhook
      url: "https://your-app.example.com/hooks/alert"
```

## Hook types

### Webhook hooks

Send an HTTP POST to a URL with the event payload as JSON body.

```yaml
hooks:
  task.completed:
    - type: webhook
      url: "https://example.com/hooks"
      secret: "your-hmac-secret"
      timeout_s: 10
      retry: 3
```

The request includes:
- `X-Bernstein-Event`: Event name
- `X-Bernstein-Signature`: HMAC-SHA256 of the body using the secret
- `X-Bernstein-Timestamp`: Unix timestamp

### Script hooks

Run a local script with the event payload as JSON on stdin.

```yaml
hooks:
  task.failed:
    - type: script
      command: "python scripts/notify_slack.py"
      timeout_s: 30
```

The script receives JSON on stdin:

```json
{
  "event": "task.failed",
  "timestamp": 1712345678.0,
  "data": {
    "task_id": "task-abc123",
    "error": "Test suite failed",
    "agent_id": "agent-xyz"
  }
}
```

### Blocking hooks

Blocking hooks run synchronously and can prevent an action from proceeding. Return a non-zero exit code to block.

```yaml
hooks:
  task.created:
    - type: blocking_script
      command: "python scripts/validate_task.py"
      timeout_s: 5
```

Use cases:
- Validate task descriptions before they enter the backlog
- Enforce naming conventions
- Check resource availability before spawning agents

## Event payloads

### task.created

```json
{
  "task_id": "task-abc123",
  "goal": "Implement feature X",
  "role": "backend",
  "priority": 2,
  "scope": ["src/feature_x/"],
  "complexity": "medium"
}
```

### task.completed

```json
{
  "task_id": "task-abc123",
  "agent_id": "agent-xyz",
  "summary": "Added feature X with tests",
  "files_changed": ["src/feature_x/main.py", "tests/test_feature_x.py"],
  "duration_s": 120.5,
  "tokens_used": 45000
}
```

### agent.spawned

```json
{
  "agent_id": "agent-xyz",
  "task_id": "task-abc123",
  "model": "sonnet",
  "role": "backend",
  "worktree": "/path/to/worktree"
}
```

### merge.conflict

```json
{
  "agent_id": "agent-xyz",
  "branch": "agent/backend-abc123",
  "conflicting_files": ["src/shared/config.py"],
  "base_branch": "main"
}
```

## Writing custom hook handlers

### Example: Slack notification on failure

```python
#!/usr/bin/env python3
"""scripts/notify_slack.py - Send Slack notifications on task failure."""

import json
import sys
import urllib.request

SLACK_WEBHOOK = "https://hooks.slack.com/services/T.../B.../..."

def main() -> None:
    event = json.load(sys.stdin)
    data = event["data"]

    message = {
        "text": f":x: Task `{data['task_id']}` failed: {data.get('error', 'unknown')}",
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"*Task Failed*\n"
                        f"Task: `{data['task_id']}`\n"
                        f"Agent: `{data.get('agent_id', 'N/A')}`\n"
                        f"Error: {data.get('error', 'unknown')}"
                    ),
                },
            }
        ],
    }

    req = urllib.request.Request(
        SLACK_WEBHOOK,
        data=json.dumps(message).encode(),
        headers={"Content-Type": "application/json"},
    )
    urllib.request.urlopen(req)

if __name__ == "__main__":
    main()
```

### Example: Custom quality gate

```python
#!/usr/bin/env python3
"""scripts/validate_task.py - Blocking hook to validate new tasks."""

import json
import sys

def main() -> int:
    event = json.load(sys.stdin)
    data = event["data"]

    # Require a scope for all tasks
    if not data.get("scope"):
        print("ERROR: Tasks must have a scope defined", file=sys.stderr)
        return 1

    # Require goal to be at least 10 characters
    goal = data.get("goal", "")
    if len(goal) < 10:
        print("ERROR: Task goal too short", file=sys.stderr)
        return 1

    return 0

if __name__ == "__main__":
    sys.exit(main())
```

## Testing hooks

Test webhook and script hooks by sending a manual HTTP POST to your hook endpoint, or by invoking the script directly with a JSON payload on stdin:

```bash
echo '{"event": "task.completed", "timestamp": 1712345678.0, "data": {"task_id": "t1"}}' \
  | python scripts/notify_slack.py
```

For blocking hooks, verify they return the correct exit code:

```python
# tests/test_my_hook.py
import json
import subprocess

def test_validate_task_blocks_missing_scope() -> None:
    payload = json.dumps({
        "event": "task.created",
        "timestamp": 1712345678.0,
        "data": {"task_id": "t1", "goal": "Short"}
    })
    result = subprocess.run(
        ["python", "scripts/validate_task.py"],
        input=payload, capture_output=True, text=True
    )
    assert result.returncode == 1  # blocked
```

Hook payload validation is available via `bernstein.core.config.hook_protocol.validate_hook_payload()`. Blocking hook enforcement is in `bernstein.core.security.blocking_hooks`.

## Debugging hooks

Enable hook debug logging:

```yaml
logging:
  hooks: DEBUG
```

Or set the environment variable:

```bash
BERNSTEIN_LOG_HOOKS=DEBUG bernstein run
```

This logs every hook invocation, payload, and result.
