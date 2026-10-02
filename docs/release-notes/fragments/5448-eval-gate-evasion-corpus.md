## `gate-evasion-v1`: does a quality gate catch the ways a change has evaded one before?

`bernstein bench run gate-evasion-v1` lays each fixture under `src/bernstein/eval/cases/gate_evasion/` out as a scratch working tree, runs the gate its manifest names through `GateRunner`, and records what that gate returned. Adding a class is a directory with a `manifest.json`, not code.

A catch requires the gate's output to identify a finding — pytest's JUnit failure count, ruff's `Found N errors.` line — not merely a nonzero exit, because pytest exits nonzero when a test module will not import and crediting that counts the benchmark's own breakage as a result. A miss says why (`pass`, `tool_error`, `command_not_found`, `no_gate`), and every result carries the basis its verdict was decided on.

On the eight shipped classes the gates catch two today, and each of the six misses has a follow-up issue against the gate that should have flagged it.
