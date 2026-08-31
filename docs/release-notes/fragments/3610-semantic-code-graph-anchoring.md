## Deterministic code graph anchoring for safe parallel execution

Added semantic code graph indexing and anchoring to enable content-addressed code representation for parallel task scheduling.

- Implemented GraphifyCodeGraph protocol using graphifyy CLI for deterministic, content-addressed code graphs
- Added unparsed_files and edge origin tracking to ast_symbol_graph.py for explicit coverage reporting  
- Modified orchestrator to build and anchor code graph digest in audit chain before agent spawning
- Added EVENT_CODE_GRAPH_ANCHORED audit event for verifiable graph state recording

Closes #3610
