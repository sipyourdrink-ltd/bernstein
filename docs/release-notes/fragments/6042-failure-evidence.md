## Preserve failed test subprocess evidence

The isolated test runner reports the native exit status for every failed
subprocess, recognizes signal termination even after a nested pytest summary,
and retains both captured streams when no failure sections were printed. This
improves diagnostics without claiming to resolve the intermittent orchestrator
exit (#6042).
