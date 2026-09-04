# bench: CI surface — SARIF, a check run, and a scorecard delta against the verified baseline bundle (#5458)

Added CI integration to `bernstein bench run`:
- `--ci` flag generating SARIF 2.1.0 output mapping failed test cases to compliance control IDs.
- Baseline verification and scorecard delta comparison (`--baseline`, `--threshold`).
- GitHub Check Run integration rendering the scorecard table.
- Enforced verification invariants: missing or unverifiable baseline yields a neutral conclusion (never green), and regressions above threshold fail the build.
