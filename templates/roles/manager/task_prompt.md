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
1. Read the codebase and `.sdd/backlog/open/` before planning — understand current state
2. Decompose this goal into tasks of 30-60 min each (max 120 min)
3. Assign each task the right role; every implementation task needs a paired QA task or inline tests
4. Set `completion_signals` on every task so the janitor can verify completion
5. Never assign two tasks to overlapping `owned_files`
6. Post tasks to the task server, then mark your own task complete

## Task creation
```bash
curl -s -X POST http://127.0.0.1:8052/tasks \
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
curl -s -X POST http://127.0.0.1:8052/tasks/{{TASK_ID}}/complete \
  -H "Content-Type: application/json" \
  -d '{"result_summary": "{{TASK_TITLE}}: created N tasks"}'
```
