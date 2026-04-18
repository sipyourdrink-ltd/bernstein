# Graphs in `core/knowledge/`

Bernstein has three graph modules under `src/bernstein/core/knowledge/`. Despite
the shared word "graph", they model three different domains and rarely overlap
at runtime. This page records what each one is for, where it lives, and who
reads from it so future readers don't confuse them.

## The three domains

| Module | Domain | Nodes / edges | Storage |
| --- | --- | --- | --- |
| `task_graph.py` | Task-dependency DAG (`core/knowledge/task_graph.py`) | Tasks; `BLOCKS` / `INFORMS` / `VALIDATES` / `TRANSFORMS` edges | In-memory, rebuilt each tick from the task store |
| `knowledge_graph.py` | Codebase symbol graph (`core/knowledge/knowledge_graph.py`) | Files, classes, functions, methods; `defines` / `imports` / `calls` / `inherits` edges | SQLite at `.sdd/index/knowledge_graph.db` |
| `ast_symbol_graph.py` | AST-level symbol graph (`core/knowledge/ast_symbol_graph.py`) | Function/class/method symbols; `calls` / `imports` / `inherits` edges | In-memory per build; seeds `knowledge_graph` |

## When to use which

- **Scheduling / critical-path analysis / detecting bottlenecks** — `task_graph`.
  Used by the orchestrator, spawner, workflow DSL, and dep-validator to decide
  what to run next and which tasks block the most downstream work.
- **"What code does a file change impact?"** — `knowledge_graph`. Backs the
  `GET /graph/impact` route and the `bernstein graph impact` CLI. Persists
  across runs, refreshed opportunistically.
- **"Which symbols and neighbourhoods live in these files?"** — `ast_symbol_graph`.
  A pure-Python AST walk used as the ingestion stage for `knowledge_graph` and
  for token-efficient context extraction. Not intended as a standalone API.

## History

`task_graph.py` was named `graph.py` and `ast_symbol_graph.py` was named
`semantic_graph.py` until audit-177 (2026-04). The old names are still resolved
through `_REDIRECT_MAP` in `bernstein/core/__init__.py` for back-compat, but new
code should import the self-descriptive names directly.
