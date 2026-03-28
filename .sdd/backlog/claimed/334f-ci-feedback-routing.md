# 334f — CI Failure Auto-Routing to Responsible Agent
**Role:** backend  **Priority:** 1 (critical)  **Scope:** medium

## Problem
4 sources: CI failures should route back to the agent that caused them.

## Design
Parse CI log → match failed files to agent's merge → create fix task with CI log + agent's own diff as context. Auto-retry up to 3x. GitHub Actions webhook integration.
