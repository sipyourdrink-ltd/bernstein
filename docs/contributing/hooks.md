# Lifecycle hooks - contract reference

This document is the single source of truth for the Bernstein lifecycle
hook pipeline. It covers:

- The full event vocabulary.
- The JSON payload schema for every event.
- The stdout protocol scripts use to allow/deny/mutate/annotate.
- Hook script discovery (`.bernstein/hooks/<event>.{sh,py}`).
- The `bernstein hooks` CLI.

If you have ported a hook script from another orchestrator and it is
not running unchanged in Bernstein, the contract is the issue - file a
bug.

---

## TL;DR

| Step | Command |
|------|---------|
| Drop a script | `chmod +x .bernstein/hooks/preToolUse.sh` |
| List what fires for an event | `bernstein hooks list` |
| Smoke-test a script | `bernstein hooks dry-run preToolUse` |
| Use a custom payload | `bernstein hooks dry-run preToolUse --payload my.json` |
| Validate all scripts | `bernstein hooks check` |

---

## Event vocabulary

Bernstein recognises two families of events.

### Cross-CLI standardised events

These match the de-facto names used by neighbouring orchestrators so a
script written for another tool drops into `.bernstein/hooks/` without
modification.

| Event | Required payload keys | Optional payload keys |
|-------|-----------------------|------------------------|
| `sessionStart` | `session_id` | `role`, `prompt_template_sha`, `env_snapshot` |
| `userPromptSubmitted` | `session_id`, `prompt` | `attached_files` |
| `preToolUse` | `session_id`, `tool`, `args` | `blast_radius_score` |
| `postToolUse` | `session_id`, `tool`, `args`, `result` | `duration_ms`, `cost`, `success` |
| `errorOccurred` | `session_id`, `error_class`, `message` | `recovery_path` |
| `idle` | `session_id`, `idle_duration_s` | - |
| `sessionEnd` | `session_id`, `status` | `total_cost`, `total_tokens` |

Extra keys are always allowed. Schemas are validated up-front by
`bernstein hooks dry-run` and at dispatch time when a payload is
explicitly supplied; missing required keys raise `PayloadSchemaError`
before any script runs.

### Bernstein-native events

The original snake_case events remain fully supported and are unchanged
by issue #1323. Existing hook scripts continue to work without edits.

| Event | When it fires |
|-------|---------------|
| `pre_task` | Before a task transitions out of `open`. |
| `post_task` | After a task reaches a terminal state. |
| `pre_merge` / `post_merge` | Around integration merges. |
| `pre_spawn` / `post_spawn` | Around agent session spawn. |
| `pre_archive` / `post_archive` | Around task archival. |

These events accept any payload shape - no schema is enforced.

---

## Where hooks live

A hook is any executable file whose path the orchestrator can reach.
There are three registration channels:

1. **Convention-based** - drop an executable at
   `.bernstein/hooks/<event>.{sh,py}`. The filename stem is matched
   against the event vocabulary; `preToolUse.sh`, `preToolUse.py`, and
   `session_start.sh` are all valid examples.
2. **Config-based** - declare scripts in `bernstein.yaml` under the
   top-level `hooks:` key. Use this when you want explicit ordering or
   a non-default timeout.
3. **Plugin-based** - implement `@hookimpl` against
   `LifecycleHookSpec` in a pluggy plugin. Plugin names registered
   under `hooks.<event>` in `bernstein.yaml` are documented references
   only - the plugin must be loaded the usual way.

Example `bernstein.yaml`:

```yaml
hooks:
  preToolUse:
    - script: scripts/policy/preflight.sh
      timeout: 5
  postToolUse:
    - scripts/audit/log_tool_use.sh
    - plugin: bernstein_plugin_jira
  sessionEnd:
    - scripts/cleanup.sh
```

---

## JSON protocol

### Input

Hooks receive a single-line JSON object on stdin. The top-level shape
is the same for every event:

```json
{
  "event": "preToolUse",
  "task": "T-123",
  "session_id": "s-abc",
  "workdir": "/path/to/worktree",
  "env": {"BERNSTEIN_FOO": "bar"},
  "timestamp": 1736251200.0,
  "data": {
    "session_id": "s-abc",
    "tool": "shell.run",
    "args": {"command": "ls"},
    "blast_radius_score": 0
  }
}
```

The per-event payload lives under `data`. Bernstein also sets the
following environment variables on the subprocess:

| Variable | Description |
|----------|-------------|
| `BERNSTEIN_EVENT` | Event value (e.g. `preToolUse`). |
| `BERNSTEIN_TASK_ID` | Task ID (when task-scoped). |
| `BERNSTEIN_SESSION_ID` | Agent session ID. |
| `BERNSTEIN_WORKDIR` | Working directory the hook should treat as CWD. |
| `BERNSTEIN_*` | Any other `BERNSTEIN_*` env variable inherited from the parent. |

Anything else is stripped - secrets and unrelated process state do not
leak into hook subprocesses.

### Output

Hooks may emit a single-line JSON object on stdout to influence the
pipeline. Stdout that is empty, plain text, or non-object JSON is
treated as an implicit `allow`.

| Decision | Effect |
|----------|--------|
| `{"decision": "allow"}` | Default. Continue the chain. |
| `{"decision": "deny", "reason": "<text>"}` | For `preToolUse`, blocks the tool call and surfaces a structured audit-chain event. |
| `{"decision": "mutate", "data": {...}}` | Replaces `LifecycleContext.data` for the rest of the chain. |
| `{"decision": "annotate", "data": {...}}` | Merges keys into `LifecycleContext.data` without replacing it. |

Unknown decision verbs are normalised to `allow`, but the raw record
is preserved for downstream consumers that want to inspect richer
responses.

### Exit codes

| Exit code | Meaning |
|-----------|---------|
| `0` | Success. Decision parsed from stdout (if any). |
| `2` | Blocking error. Pipeline halts and stderr is surfaced. |
| Any other non-zero | Treated as a failure, raises `HookFailure`. |

A `deny` decision is distinct from a non-zero exit: deny is a
*structured* refusal that the orchestrator records as an audit event,
while a non-zero exit is treated as a *hook bug* the operator must
investigate.

---

## Example: deny `shell.run` for a session

`.bernstein/hooks/preToolUse.sh`:

```bash
#!/usr/bin/env bash
set -euo pipefail

payload="$(cat)"
tool="$(echo "$payload" | jq -r '.data.tool')"

if [[ "$tool" == "shell.run" ]]; then
  echo '{"decision": "deny", "reason": "shell.run blocked by site policy"}'
  exit 0
fi

echo '{"decision": "allow"}'
```

Smoke-test:

```bash
bernstein hooks dry-run preToolUse
# DENIED: preToolUse blocked by script:.bernstein/hooks/preToolUse.sh: shell.run blocked by site policy
```

---

## Example: annotate `postToolUse` with billing data

```python
#!/usr/bin/env python3
import json
import sys

payload = json.loads(sys.stdin.read())
duration_ms = payload["data"].get("duration_ms", 0)

print(
    json.dumps(
        {
            "decision": "annotate",
            "data": {"billing_unit_cost": duration_ms * 0.0001},
        }
    )
)
```

Place this at `.bernstein/hooks/postToolUse.py`, mark it executable,
and subsequent hooks in the chain will see `billing_unit_cost` in
their payload.

---

## CLI reference

### `bernstein hooks list`

Print every hook registered for every event, including
convention-based scripts and plugin references.

### `bernstein hooks dry-run <event> [--payload <file>]`

Fire `<event>` with a synthetic payload. The default payload is the
documented sample for the event; pass `--payload <file>` to supply a
JSON object instead. The payload is validated against the schema
before any script runs.

Exit codes:

- `0` - every hook in the chain returned `allow` (or didn't emit a
  decision).
- `1` - a hook raised `HookFailure` or the payload failed validation.
- `2` - a hook explicitly denied the event.

### `bernstein hooks check`

Validate that every script declared in `bernstein.yaml` exists and is
executable. Useful as a pre-merge gate so a broken hook reference
never lands on `main`.

### `bernstein hooks run <event>`

Fire `<event>` with an empty context. Kept for backwards compatibility;
prefer `dry-run` for new workflows since it ships sample payloads.

---

## Schema enforcement and forward compatibility

- Schemas are *additive*. Bernstein never strips unknown keys from
  `LifecycleContext.data`; extra annotations propagate to every hook
  in the chain and to plugin `@hookimpl` consumers.
- Missing required keys fail loudly. `bernstein hooks dry-run` will
  refuse to dispatch and tell you which key is missing.
- The 7 cross-CLI event names are stable. New events may be added in
  the future; existing event names will not change.

---

## Audit-chain integration

Every `deny` decision in the `preToolUse` chain produces a structured
audit event (issue #1316 surface). The event payload includes:

- The event value (`preToolUse`).
- The denying hook's label (e.g. `script:.bernstein/hooks/preToolUse.sh`).
- The reason string returned by the hook.
- The original `LifecycleContext.data` (with `tool`, `args`, etc.).

These events are forwarded through the standard `on_audit_event`
pluggy hook so any SIEM sink (Splunk, Datadog, Elastic, MQTT) sees
denials automatically.

---

## Migration checklist for existing hook scripts

If you are porting a hook script from another CLI:

1. Drop it at `.bernstein/hooks/<event>.{sh,py}` and `chmod +x` it.
2. Run `bernstein hooks dry-run <event>` to confirm it executes.
3. If the script reads its event metadata from environment variables
   (e.g. `CLAUDE_EVENT`, `OPENAI_TOOL_NAME`), adapt it to read from
   `BERNSTEIN_*` variables instead, or parse the JSON payload from
   stdin.
4. If the script writes decisions to stdout in a non-JSON format,
   wrap the output in the JSON envelope documented above.

That's it. The contract is intentionally narrow so the port stays
mechanical.
