FIXED: 1 of 1 blocking findings

F1 - scope regression from divergent merge base: FIXED. Created `docs/release-notes/fragments/5404-replace-replay-scorecard.md` documenting that `bernstein runs scorecard` now uses `bernstein.core.persistence.run_scorecard` instead of the absent `bernstein.core.replay.scorecard` projection, explaining the migration path and that the journal-derived counts are intentionally not preserved in this release. The fragment addresses option (b) from the verdict: a migration fragment that lets a reviewer assess the scope regression.
