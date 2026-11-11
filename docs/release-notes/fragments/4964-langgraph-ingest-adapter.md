## LangGraph ingest adapter for graph-shaped agent runtimes

Ingests foreign execution traces from graph-shaped agent runtimes (LangGraph) through the ingest adapter plugin boundary without modifying core scheduling logic (#4964).

The adapter retains foreign nodes, edges, input/output state and tool calls in its ingested representation, and maps node identities and edges to Bernstein's lineage task graph vocabulary (`RunPlan` / `PlanNode`). Every tool call carries a deterministic digest of its recorded arguments. Graph text renderings remain byte-identical across repeated ingests.

This is the entry-point-registered ingest-boundary slice of #4964. String inputs
are JSON; local files require an explicit `Path`. Malformed tool-call entries are
rejected instead of silently discarded. The fixture is synthetic, and the
dependency-free callback recorder is tested with hand-driven events. Real-runtime
callback conformance and recorded-fixture provenance, production declaration
collection, CLI/replay wiring, and offline receipts remain follow-up work.
