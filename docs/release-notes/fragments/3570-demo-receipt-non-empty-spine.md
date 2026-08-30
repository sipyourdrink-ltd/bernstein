## The front-page demo receipt carries a non-empty lineage spine

The shipped `docs/assets/demo-run/run-receipt.json` now has `spine.entry_count > 0`. The demo recording script (`scripts/record_demo.sh`) waits for the orchestrator to finalize gracefully, so the spine is written by the run itself rather than rebuilt post-hoc. The README now states the full journal-head + spine-head bind unconditionally (#3570).
