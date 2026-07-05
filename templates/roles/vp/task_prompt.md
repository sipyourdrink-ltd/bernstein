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
1. Read `.sdd/` state and bulletin board before acting. Understand current cell status
2. Decompose the goal into subsystem-level objectives; assign each to a cell Manager
3. Define explicit cross-cell interfaces: shared schemas, API contracts, file boundaries
4. Each cell should own a coherent subsystem; minimise cross-cell dependencies
5. Post coordination decisions to the bulletin board so all cells have visibility
6. If a cell fails the same objective twice, reassign or restructure. Don't retry blindly

{{INCLUDE completion_contract}}
