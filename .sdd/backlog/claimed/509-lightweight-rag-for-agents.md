# 509 — Lightweight project RAG for agent codebase queries

**Role:** architect
**Priority:** 3
**Scope:** large
**Complexity:** high

## Problem
Agents spend 30-50% of turns navigating code with `grep`, `find`, `cat`. A pre-built RAG index would let them ask "where is auth handled?" and get file + line references instantly, instead of trial-and-error searching. This complements #412 (static context injection) with dynamic querying.

## Design considerations
- **NOT a full vector DB** — overkill for a CLI tool. Use SQLite + BM25 (no embeddings needed).
- **Build on startup, incrementally update** — index `.py`, `.md`, `.yaml` files
- **Query interface** — MCP tool exposed to agents: `search_codebase(query) -> [{file, line, snippet}]`
- **Storage** — `.sdd/index/codebase.db` (SQLite FTS5)

## Implementation

### 1. Indexer (`src/bernstein/core/rag.py`)
- On bootstrap: scan project files, chunk by function/class (AST-aware for Python)
- Store: file_path, line_start, line_end, content, symbols (function/class names)
- Use SQLite FTS5 for full-text search (BM25 ranking, no external deps)
- Incremental: only re-index files modified since last index (check mtime)
- Exclude: `.sdd/runtime/`, `__pycache__/`, `.git/`, `node_modules/`

### 2. Query MCP tool
Expose as MCP tool available to all agents:
```json
{"name": "search_codebase", "description": "Search project codebase by keyword or question",
 "parameters": {"query": "string", "limit": "int (default 5)"}}
```
Returns: top-N matches with file, line range, snippet, and relevance score.

### 3. Agent integration
- Add MCP server to agent spawn config (pass via `--mcp-config`)
- Agents use `search_codebase` instead of `grep` for high-level questions
- Fall back to `grep`/`find` for exact matches

### Risk assessment
- **Risk**: Agents over-rely on RAG and miss recent changes not yet indexed
- **Mitigation**: Index includes mtime; show staleness warning if file changed after last index
- **Risk**: Index size for large projects
- **Mitigation**: FTS5 is efficient; 100K LOC project = ~10MB index

## Files
- src/bernstein/core/rag.py (new) — CodebaseIndexer, search()
- src/bernstein/core/bootstrap.py — build/update index on startup
- tests/unit/test_rag.py (new)

## Completion signals
- test_passes: uv run pytest tests/unit/test_rag.py -x -q
- file_contains: src/bernstein/core/rag.py :: CodebaseIndexer
