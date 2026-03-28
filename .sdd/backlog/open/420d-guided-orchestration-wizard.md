# 420d — Guided Orchestration Wizard with Templates
**Role:** frontend  **Priority:** 3 (medium)  **Scope:** medium

## Problem
"Only senior+ engineers use parallel agents successfully." Multi-agent orchestration needs templates for common patterns.

## Design
Pre-built workflow templates: "refactor + test + review", "feature + docs + security audit", "bug fix + regression test". `bernstein wizard` walks through template selection. Generates bernstein.yaml with tasks pre-defined.
