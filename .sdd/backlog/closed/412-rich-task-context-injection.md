# 412 -- Rich task context injection to eliminate agent research overhead

**Role:** architect
**Priority:** 1 (critical)
**Scope:** large
**Complexity:** high

## Problem
Every spawned agent starts fresh and spends 30-50% of its turns (and tokens) reading files, running `find`, `rg`, `cat` to understand the codebase. Research across CrewAI, AutoGen, MetaGPT, Aider, OpenHands, Devin, and Google ADK (2025-2026) converges on the same solution: **pre-computed context injection** with **layered architecture** and **explicit token budgets**.

Key finding: Aider's repo-map (tree-sitter AST signatures + dependency graph, token-budgeted) is the single highest-ROI technique. AGENTS.md-style briefing docs correlate with 29% runtime reduction (arXiv 2602.20478).

## Design: Three-Layer Context Architecture

### Layer 1: Hot Context (injected into every agent, ~15% token budget)
- Role system prompt (already exists)
- Project conventions from `.sdd/project.md` (already exists)
- **NEW: Repo map** — compressed codebase summary

### Layer 2: Warm Context (injected per-task, ~40% token budget)
- **NEW: Task context packet** — files to touch, dependency info, predecessor outputs
- **NEW: Relevant knowledge** — decisions/findings from related completed tasks

### Layer 3: Cold Context (agent pulls on-demand via tools, ~35% reserved)
- Full file contents (agent reads via tools as needed)
- Full specs, historical data

## Implementation

### 1. Repo Map Generator (`src/bernstein/core/repomap.py`)
Aider-style codebase digest:
- Parse Python files via `ast` module (no tree-sitter dependency needed)
- Extract: module docstrings, class names + bases, function signatures, top-level constants
- Build import graph: file A imports from file B
- Rank by relevance: files with most importers = most important
- Token-budget the output (configurable, default 1500 tokens)
- Cache to `.sdd/knowledge/repo_map.md`, regenerate on file changes (check mtimes)

### 2. Task Context Packet Builder (`src/bernstein/core/context.py`)
For each task, construct a structured packet:
- **Owned files summary**: For each file in `owned_files`, include function/class signatures (from repo map data)
- **Dependency context**: Which files import owned files, which files owned files import
- **Predecessor outputs**: If task depends on completed tasks, include their `result_summary` (from task server)
- **Recent decisions**: Last 5 entries from `.sdd/knowledge/decisions.jsonl` relevant to the touched subsystem

### 3. Knowledge Capture on Task Completion
When janitor confirms a task is done:
- Parse agent output log for key decisions (heuristic: lines containing "decided", "chose", "because", "approach")
- Append structured entry to `.sdd/knowledge/decisions.jsonl`: `{task_id, title, files_changed, summary, timestamp}`
- Update repo map if files were modified

### 4. Manager Enrichment
Update manager role prompt to require `context_hints` in created tasks:
- List specific files the agent will need
- Note any architectural constraints or gotchas
- Reference related completed tasks

### 5. Prompt Assembly in Spawner
Modify `_render_prompt()` in `spawner.py`:
```
[Role system prompt]              ← hot (existing)
[Repo map]                        ← hot (NEW)
[Task description + context_hints]← warm (existing + enriched)
[File signatures for owned_files] ← warm (NEW)
[Predecessor task summaries]      ← warm (NEW)
[Recent relevant decisions]       ← warm (NEW)
[Completion instructions]         ← hot (existing)
```

## Files
- src/bernstein/core/repomap.py (new) — AST-based repo map generator
- src/bernstein/core/context.py — extend with TaskContextPacket builder
- src/bernstein/core/spawner.py — inject repo map + context packet into prompts
- src/bernstein/core/janitor.py — knowledge capture on task completion
- src/bernstein/core/orchestrator.py — trigger repo map refresh
- templates/roles/manager/system_prompt.md — require context_hints
- tests/unit/test_repomap.py (new)
- tests/unit/test_context_packet.py (new)

## Completion signals
- test_passes: uv run pytest tests/unit/test_repomap.py -x -q
- test_passes: uv run pytest tests/unit/test_context_packet.py -x -q
- file_contains: src/bernstein/core/repomap.py :: RepoMap
- file_contains: src/bernstein/core/spawner.py :: repo_map
- path_exists: .sdd/knowledge/repo_map.md

## Research reference
- .sdd/research/auto/context-sharing-best-practices-2026.md


---
**completed**: 2026-03-28 05:01:51
**task_id**: a572449b736f
**result**: Completed: 412 -- Rich task context injection. Added TaskContextBuilder with AST parsing, dependency graphs, git co-change analysis, subsystem context. Knowledge base at .sdd/knowledge/ with file_index.json, architecture.md, recent_decisions.md. Spawner injects rich context into agent prompts. Orchestrator refreshes KB periodically and captures decisions from done tasks. Manager template updated with context_hints guidance. 29 new tests, 1580 total passing.
