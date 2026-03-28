# 647 — WASM Isolation Spike

**Role:** architect
**Priority:** 5 (low)
**Scope:** small
**Depends on:** none

## Problem

Current isolation options (worktrees, containers, microVMs) each have trade-offs in startup time, overhead, and security. WebAssembly Components offer microsecond-level isolation with capability-based security, but it is unclear whether this is practical for Bernstein's use case. Microsoft's Wassette project runs WASM via MCP, suggesting the approach has legs.

## Design

Conduct a research spike evaluating WASM Components for Bernstein tool isolation. Investigate: can Bernstein tools (file operations, git commands, HTTP requests) be compiled to or wrapped in WASM components? What is the startup overhead compared to containers and microVMs? Does the WASI (WebAssembly System Interface) provide sufficient filesystem and network capabilities? Prototype a single tool (e.g., file read/write) running inside a WASM sandbox. Evaluate the Wasmtime and WasmEdge runtimes for Python integration. Document findings: feasibility, performance characteristics, capability limitations, and a recommendation on whether to invest further. Time-box to 2 days of effort.

## Files to modify

- `.sdd/decisions/wasm-isolation-spike.md` (new)
- `spikes/wasm-isolation/` (new — prototype code)

## Completion signal

- Decision document with feasibility assessment and performance data
- Prototype demonstrating one tool running in WASM sandbox
- Clear recommendation: invest further or abandon
