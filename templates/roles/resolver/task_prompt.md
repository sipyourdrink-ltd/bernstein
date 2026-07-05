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
1. For each conflicting file, read both sides of the conflict markers and surrounding context
2. Understand what each side was trying to accomplish. Read git log for both branches
3. Combine both intents where possible; pick one side only when truly incompatible
4. After resolving all conflicts, run tests: `uv run python scripts/run_tests.py -x`
5. Stage resolved files and commit with a message explaining resolution choices

## If stuck or blocked
- If a conflict is ambiguous, mark the task as failed with a clear explanation rather than guessing
- If tests fail after resolution, the resolution is wrong. Revisit the conflicting sections

{{INCLUDE completion_contract}}
