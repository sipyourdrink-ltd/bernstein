## Resolve imported modules in the unreachable-control checker

The security and identity reachability checker now distinguishes calls through
imported modules and aliases from unrelated object methods with the same name.
Synthetic-tree regressions cover module aliases and same-name SDK methods, and
the allowlist records the controls newly exposed as unreachable by the corrected
analysis. This is split out from the Sandbox0 integration review (#6173).
