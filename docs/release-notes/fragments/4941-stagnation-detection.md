## Repository flow stagnation detection

Added ``detect_stagnation()`` — a pure decision function in ``core.observability.stagnation`` that identifies when merge-ready work has sat untouched for a configurable time window. The function examines a time-series of ``RepositoryFlowSample`` snapshots and returns a ``StagnationFinding`` when every sample in the window shows work ready (open PRs with mergeable state) and no merge occurred. Designed for deterministic offline auditing: no I/O, all evidence preserved verbatim. (#4941)
