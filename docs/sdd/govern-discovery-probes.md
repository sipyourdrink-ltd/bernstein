# Govern discovery probe modules

Issue #5081 keeps probe declarations separate from their execution machinery so
neither declaration loading nor agent discovery becomes a monolith.

- `src/bernstein/core/govern/probe.py` owns the declared `Probe`/`ProbeSet`
  schema, directory loading, validation, unknown-field round-tripping, and
  compatibility re-exports for the runtime API.
- `src/bernstein/core/govern/probe_runtime.py` owns refresh-aware fact caching,
  ordered fallback/retry execution, per-target start jitter, hard probe
  deadlines, explicit unknown outcomes, normalized failure records, and the
  single canonical run journal entry.
- `src/bernstein/core/agents/detector_runtime.py` owns the small deadline/jitter
  boundary around registered deep agent detectors. `agent_discovery.py` remains
  responsible only for registry resolution and aggregation.

The current `bernstein govern discover` inventory collector is intentionally
separate. Observation-envelope collection and remote/network probe clients are
outside #5081.
