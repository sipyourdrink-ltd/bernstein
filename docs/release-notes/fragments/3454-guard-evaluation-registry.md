## Guard evaluation reachability tracking

Added `bernstein.core.observability.guard_registry.GuardRegistry`, a per-process registry that records every guard evaluation — not just violations — so "never fired" is distinguishable from "never evaluated". The `scope_violation` check is now instrumented; its evaluation count appears in `default_report()`.

(#3454)
