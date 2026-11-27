## OSSF Scorecard skips scanner conformance fixture targets

Added `tests/fixtures/scanners/.gitignore` ignoring `*/target/` so repo-root
scans that honour `.gitignore` (including OSSF Scorecard's `osv-scanner`
Vulnerabilities check) skip deliberately vulnerable scanner fixture targets
(`tests/fixtures/scanners/<tool>/target/`) without modifying files inside any
fixture directory or changing shipped lockfile scanning (#6068).
