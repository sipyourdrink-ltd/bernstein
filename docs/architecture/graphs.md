# Graphs in `core/knowledge/`

Bernstein has three core graph modules under `src/bernstein/core/knowledge/`
(`code_graph.py` and `graphify_code_graph.py` also live there and are not
covered here). Despite the shared word "graph", they model three different domains and rarely overlap
at runtime. This page records what each one is for, where it lives, and who
reads from it so future readers don't confuse them.

## The three domains

| Module | Domain | Nodes / edges | Storage |
| --- | --- | --- | --- |
| `task_graph.py` | Task-dependency DAG (`core/knowledge/task_graph.py`) | Tasks; `BLOCKS` / `INFORMS` / `VALIDATES` / `TRANSFORMS` edges | In-memory, rebuilt each tick from the task store |
| `knowledge_graph.py` | Codebase symbol graph (`core/knowledge/knowledge_graph.py`) | Files, classes, functions, methods; `defines` / `imports` / `calls` / `inherits` edges | SQLite at `.sdd/index/knowledge_graph.db` |
| `ast_symbol_graph.py` | AST-level symbol graph (`core/knowledge/ast_symbol_graph.py`) | Function/class/method symbols; `calls` / `inherits` edges | In-memory per build; seeds `knowledge_graph` |

Terminology note: a **workflow node** is a declaration in a YAML workflow
manifest ([workflow manifest guide](../operations/workflow-manifests.md)); it is
not a task record. A **task graph node** represents an executable task record,
while a **symbol graph node** represents a code symbol or file. The shared word
"node" refers to different entities in these domains.

## When to use which

- **Scheduling / critical-path analysis / detecting bottlenecks** - `task_graph`.
  Used by the orchestrator, spawner, workflow DSL, and dep-validator to decide
  what to run next and which tasks block the most downstream work.
- **"What code does a file change impact?"** - `knowledge_graph`. Backs the
  `GET /graph/impact` route and the `bernstein graph impact` CLI. Persists
  across runs, refreshed opportunistically.

- **"Which symbols and neighbourhoods live in these files?"** - `ast_symbol_graph`.
  A pure-Python AST walk used as the ingestion stage for `knowledge_graph` and
  for token-efficient context extraction. Not intended as a standalone API.

### File impact queries

Pass a repository-relative path to select one file exactly:

```console
bernstein graph impact src/alpha/shared.py
```

An exact path match takes precedence. If there is no exact path match, the
query falls back to the basename and can select every matching file in the
graph. For example, when both `src/alpha/shared.py` and
`src/beta/shared.py` are present, `bernstein graph impact shared.py`
can match both; use either full path to select just that file.

The reported downstream set follows dependencies transitively, is sorted by
path, and excludes the matched starting file or files. If no file matches, the
CLI reports that and exits with status 1. A successful match with no downstream
files is different: the CLI reports “No downstream files found” and exits
successfully.

## History

`task_graph.py` was named `graph.py` and `ast_symbol_graph.py` was named
`semantic_graph.py` until April 2026. The old names are still resolved
through `_REDIRECT_MAP` in `bernstein/core/__init__.py` for back-compat, but new
code should import the self-descriptive names directly.
