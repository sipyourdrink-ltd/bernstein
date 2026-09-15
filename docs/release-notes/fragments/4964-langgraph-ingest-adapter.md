## LangGraph ingest adapter for graph-shaped agent runtimes

Ingests foreign execution traces from graph-shaped agent runtimes (LangGraph) through the ingest adapter plugin boundary without modifying core scheduling logic (#4964).

The adapter maps foreign nodes, edges, state transitions, and tool calls into Bernstein's lineage task graph representation (`RunPlan` / `PlanNode`). Every tool call is anchored with a deterministic digest of its arguments, node identities and edges are preserved faithfully without silent shrinkage, and graph text renderings remain byte-identical across runs.
