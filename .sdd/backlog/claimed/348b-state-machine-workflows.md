# 348b — Explicit State Machine Workflows
**Role:** backend  **Priority:** 2 (high)  **Scope:** medium

## Problem
"Driving a deterministic state machine for each ticket — final code quality is very good."

## Design
Define workflows as states (plan→implement→test→review→merge) with deterministic transitions. Human checkpoints configurable. Each state is a task with specific agent role.
