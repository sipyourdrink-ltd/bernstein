# 335b — Cross-Model Verification Pipeline
**Role:** backend  **Priority:** 1 (critical)  **Scope:** medium

## Problem
Multiple sources: "Have Codex review Claude's code." Different models catch different issues.

## Design
After task completion, route diff to a DIFFERENT model for review. Writer != reviewer. Review agent uses cheap model with focused prompt. Configurable per-task.
