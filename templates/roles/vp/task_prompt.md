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
1. Read `.sdd/` state and bulletin board before acting — understand current cell status
2. Decompose the goal into subsystem-level objectives; assign each to a cell Manager
3. Define explicit cross-cell interfaces: shared schemas, API contracts, file boundaries
4. Each cell should own a coherent subsystem; minimise cross-cell dependencies
5. Post coordination decisions to the bulletin board so all cells have visibility
6. If a cell fails the same objective twice, reassign or restructure — don't retry blindly

## Done signal
```bash
curl -s -X POST http://127.0.0.1:8052/tasks/{{TASK_ID}}/complete \
  -H "Content-Type: application/json" \
  -d '{"result_summary": "{{TASK_TITLE}}: <N cells assigned, subsystem breakdown>"}'
```
