# 334f — CI Failure Auto-Routing to Responsible Agent
**Role:** backend  **Priority:** 1 (critical)  **Scope:** medium

## Problem
4 sources: CI failures should route back to the agent that caused them.

## Design
Parse CI log → match failed files to agent's merge → create fix task with CI log + agent's own diff as context. Auto-retry up to 3x. GitHub Actions webhook integration.


---
**completed**: 2026-03-28 23:21:54
**task_id**: de9c9fb811dc
**result**: Completed: 334f — CI Failure Auto-Routing to Responsible Agent. Delivered full workflow spec at docs/workflows/WORKFLOW-ci-failure-routing.md covering 12 test cases, 6 RC findings, all failure branches (log unavailable, attribution failure, max retries, duplicate events), handoff contracts for GitHub API and task server, and implementation blueprint for ci_failure_to_task() mapper function.
