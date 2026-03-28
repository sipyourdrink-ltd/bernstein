# 349b — Structured Context Handoff Protocol
**Role:** backend  **Priority:** 2 (high)  **Scope:** medium

## Problem
"Subagents seem good on greenfield but on complex projects that handoff is the kiss of death."

## Design
Auto-generate focused context briefs when delegating subtasks. Strip unnecessary context. Include: relevant files (top 5), recent decisions, known constraints. Not full conversation history.


---
**completed**: 2026-03-29 00:21:36
**task_id**: a6c5a81a9cbe
**result**: Completed: 382 — Modern git integration: branches, PRs, smart commits, context from history. All completion signals verified: git_ops.py has conventional_commit, safe_push, bisect_regression; git_context.py has blame_summary, hot_files; orchestrator.py has no direct subprocess.run git calls; 79+28=107 tests pass.
