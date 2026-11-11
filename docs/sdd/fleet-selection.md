# Fleet worker selection

`core/fleet/directory_registry.py` validates manifest worker matrices into
`WorkerCapabilitySpec` rows on `InstanceSpec`. Matrix errors retain dashboard
registration and collected errors while removing all routing eligibility.
`core/fleet/selection.py` exposes the pure `select_fleet_instance` function via
`bernstein.core.fleet`: one enabled instance, one fully matching worker, then
lexicographic instance-name ordering. It reuses `TaskType`, `DispatchTier` and
`Task.requires`; it performs no submission, discovery or authority grant.
See [manifest and matching rules](../fleet/directory-registry.md).
