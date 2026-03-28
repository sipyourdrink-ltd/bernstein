# 420c — AGENTS.md-Aware Task Routing
**Role:** backend  **Priority:** 3 (medium)  **Scope:** small

## Problem
Nx/Datadog: "Nested AGENTS.md are the default for monorepos but quite limited." Agents don't read project-level agent configs.

## Design
Read AGENTS.md / .cursorrules / CLAUDE.md per subdirectory. Auto-inject relevant rules into agent prompt based on which files the task touches. Monorepo-aware routing.
