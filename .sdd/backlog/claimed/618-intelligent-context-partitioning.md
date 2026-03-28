# 618 — Intelligent Context Partitioning

**Role:** backend
**Priority:** 2 (high)
**Scope:** medium
**Depends on:** none

## Problem

Currently, agents receive broad codebase context regardless of their specific task. This wastes tokens, increases cost, and degrades quality as irrelevant context dilutes the agent's focus. No intelligent context selection exists.

## Design

Implement intelligent context partitioning that routes only relevant files to each agent. Build a file dependency analyzer that maps imports, function calls, and test-to-source relationships. When a task is assigned, use the task description and file dependency graph to select the minimal relevant file set. Implement tiered context: tier 1 (files to modify — full content), tier 2 (direct dependencies — summaries), tier 3 (transitive dependencies — file names only). Use TF-IDF or embedding similarity between task description and file content for relevance scoring. Cache the dependency graph per project to avoid re-analysis. Expose context partitioning decisions in the audit log for transparency.

## Files to modify

- `src/bernstein/core/context_partitioner.py` (new)
- `src/bernstein/core/dependency_analyzer.py` (new)
- `src/bernstein/core/orchestrator.py`
- `src/bernstein/core/spawner.py`
- `tests/unit/test_context_partitioner.py` (new)

## Completion signal

- Agents receive only task-relevant files, not full codebase
- Context selection logged with reasoning
- Measurable token reduction (target: 60%+ fewer tokens per agent)
