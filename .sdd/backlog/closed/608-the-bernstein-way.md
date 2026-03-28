# 608 — The Bernstein Way

**Role:** architect
**Priority:** 1 (critical)
**Scope:** small
**Depends on:** none

## Problem

Bernstein lacks a clearly articulated opinionated default workflow. Users don't know what "the right way" to use Bernstein looks like. Without strong defaults, every user reinvents the wheel and the tool feels like a framework instead of a product.

## Design

Define and document "The Bernstein Way" — the opinionated default workflow that works out of the box. Core tenets: supervisor/worker topology (one manager agent decomposes work, N worker agents execute), git worktree isolation (each agent gets its own worktree), CI feedback loop (every agent push triggers CI, failures route back), cost budgeting (every run has a dollar cap), and file-based state (`.sdd/` is the source of truth). Document escape hatches for advanced users who need custom topologies. Write this as a concise architecture doc, not a tutorial. Include a diagram showing the default flow. This document becomes the foundation for README, docs, and talks.

## Files to modify

- `docs/the-bernstein-way.md` (new)
- `README.md` (reference to the doc)

## Completion signal

- "The Bernstein Way" document exists with clear tenets and flow diagram
- Default workflow is implementable from the doc alone
- README references the document
