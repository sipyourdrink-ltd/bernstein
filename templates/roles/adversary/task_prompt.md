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
1. Run `git diff` against the base branch to see the change you are reviewing.
   Do NOT skim; read every hunk.
2. For each touched file, read the surrounding context (full function,
   caller, and matching test file).
3. Build a list of suspicions. For each one, construct a falsification
   test: a concrete test that, if it passes, closes the finding.
4. Drop any suspicion you cannot turn into a falsification test, or
   downgrade it to `info`.
5. Classify each finding's severity:
   - `critical`: blocks the merge (data loss, security, work loss,
     billing error, or a regression of documented behaviour).
   - `warning`: flaky test, missed edge case, subtle correctness bug.
   - `info`: design concern, style nit, future-work hint.
6. Output a single JSON object on stdout with the schema documented in
   your system prompt.
7. Do NOT modify source files. You produce findings only.

## If stuck or blocked
- If a curl to the task server fails, retry up to 3 times with 2-second delays
- If you cannot determine the failure mode but suspect a problem, use
  `info` severity, not `critical`.
- If the diff is unreviewable (binary, generated, > 2000 LOC), follow
  the rules in your system prompt.
- If you cannot complete the review, mark the task as failed:
  ```bash
  curl -s -X POST http://127.0.0.1:8052/tasks/{{TASK_ID}}/fail \
    -H "Content-Type: application/json" \
    -d '{"reason": "<describe what went wrong and what you tried>"}'
  ```

## Bulletin board
Post critical findings immediately so the Steward can react before
the timeout:
```bash
curl -s -X POST http://127.0.0.1:8052/bulletin \
  -H "Content-Type: application/json" \
  -d '{"agent_id": "{{AGENT_ID}}", "type": "blocker", "content": "<critical finding summary + evidence>"}'
```

{{INCLUDE completion_contract}}
