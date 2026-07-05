## Done signal (completion contract worker-completion/v1)

Report your terminal outcome as a structured JSON payload. The task server validates it against the completion contract; a payload that fails validation fails the task as `contract_violation`, so fix the payload instead of retrying blindly.

When the work is complete and verified:
```bash
curl -s -w '\n%{http_code}' -X POST http://127.0.0.1:8052/tasks/{{TASK_ID}}/complete \
  -H "Content-Type: application/json" \
  -d '{"payload": {"contract": "worker-completion/v1", "summary": "<what was done>", "files_changed": ["<repo-relative path>"], "verification": {"command": "<command you ran>", "exit_code": 0}}}'
```

Payload fields:
- `summary` (required): what was done, max 2000 characters.
- `files_changed`: repo-relative paths you modified.
- `verification`: the command you ran to verify the work and its exit code; use `null` only when nothing was runnable.
- `receipt_ref` (optional): hash or reference to a receipt artefact.

If you cannot proceed as specified, do NOT improvise and do NOT mark the task complete or failed. Report a typed refusal instead; the task ends in the terminal `refused` state, distinct from failure, and the orchestrator routes it deterministically:
```bash
curl -s -w '\n%{http_code}' -X POST http://127.0.0.1:8052/tasks/{{TASK_ID}}/complete \
  -H "Content-Type: application/json" \
  -d '{"payload": {"contract": "worker-completion/v1", "kind": "<kind>", "detail": "<why you cannot proceed>", "<kind-specific field>": "<value>"}}'
```

Refusal kinds (closed set; anything else is rejected):
- `scope_exceeded` with `"proposed_split": ["<subtask 1>", "<subtask 2>"]` - the task is too large for one session; the orchestrator creates the split tasks from your list.
- `underspecified` with `"question": "<what is missing>"` - the spec does not determine the work.
- `awaiting_operator` with `"question": "<what needs sign-off>"` - a human must decide before you can proceed.
- `blocked_on_dependency` with `"blocking_dep": "<task id or resource>"` - an upstream output does not exist yet.

Check the trailing HTTP status code. Only retry on connection refused / network errors; a 409 or any other 4xx means the task state changed and retrying will not help. If the task server requires auth, add the `Authorization: Bearer` header described in the auth section.
