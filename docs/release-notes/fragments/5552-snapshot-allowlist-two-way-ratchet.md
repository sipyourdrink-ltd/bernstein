## Snapshot allowlists report two-way drift in one run and attribute decay to main

Guards holding frozen snapshot allowlists (`KNOWN_ORPHANS`, `KNOWN_UNCALLED`,
`KNOWN_UNREACHABLE`) previously stopped at the first assertion failure,
reporting only appeared entries or only removed entries. During review cycles
when `main` advanced, these guards failed in both directions and accused the PR
branch of adding caller-less code even when the code had been merged to `main`
in an unrelated commit.

A centralized two-way ratchet helper (`tests/unit/_ratchet.py`) now powers
`test_token_orphans`, `test_compliance_module_reachability`,
`test_security_controls_are_wired`, and `test_orchestration_reachability`:

1. **Unified Diagnostics:** Reports both newly appeared entries and stale
   exemptions simultaneously in a single test run, outputting a copy-pasteable
   snippet to update the constant.
2. **Git Attribution:** Inspects git merge metadata when running against PR
   merge refs or tracking branches, clearly distinguishing between entries
   introduced by the current branch versus baseline staleness caused by `main`
   advancing while the branch was in review (#5552, #5503).
