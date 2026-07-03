# Task: {{TASK_TITLE}}

## Description
{{TASK_DESCRIPTION}}

{{#IF FILES}}
## Files to work with
{{FILES}}
{{/IF}}

{{#IF CONTEXT}}
## Context
{{CONTEXT}}
{{/IF}}

## Instructions
1. Read the codebase and `.sdd/backlog/open/` before planning to understand current state
2. Decompose this goal into tasks of 30-60 min each (max 120 min)
3. Assign each task the right role; every implementation task needs a paired QA task or inline tests
4. Set `completion_signals` on every task so the janitor can verify completion
5. Never assign two tasks to overlapping `owned_files`
6. Post tasks to the task server, then mark your own task complete

## Task creation

The task server requires bearer-token auth - see the `## Task Server Authentication`
section appended below for the absolute token file path. Every POST needs the
`Authorization: Bearer $(cat …)` header (or `Authorization: Bearer $BERNSTEIN_AUTH_TOKEN`
as a fallback).

**Call form matters.** These commands rely on `$(...)`/`$VAR` shell expansion, which
only happens when `run_command` is invoked with a single command **STRING**. If
`run_command` is invoked with an argv **LIST** instead, no shell runs, `$(...)`/`$VAR`
are sent as literal text, curl still exits 0, and the server returns 401 with no other
visible error. Always pass these commands to `run_command` as one string. Do not read
the token via the `read_file` tool - the token file lives outside your worktree and
`read_file` cannot reach it; use `run_command` string-form `cat <token-path>` instead.
Always inspect the trailing HTTP status code (`-w '\n%{http_code}'` below) and treat
any non-2xx response as a failure to fix and retry, not as a reason to give up.

```bash
TOKEN=$(cat <absolute-token-path-from-auth-section>)
curl -sS -w '\n%{http_code}' -X POST http://127.0.0.1:8052/tasks \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "...",
    "role": "backend",
    "description": "...",
    "priority": 2,
    "scope": "medium",
    "complexity": "medium",
    "owned_files": [...],
    "completion_signals": [...]
  }'
```

## Done signal
```bash
TOKEN=$(cat <absolute-token-path-from-auth-section>)
curl -sS -w '\n%{http_code}' -X POST http://127.0.0.1:8052/tasks/{{TASK_ID}}/complete \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"result_summary": "{{TASK_TITLE}}: created N tasks"}'
```

Pass the whole command above to `run_command` as ONE string - never as an argv list -
and confirm the trailing status code is 2xx before considering the task done.
