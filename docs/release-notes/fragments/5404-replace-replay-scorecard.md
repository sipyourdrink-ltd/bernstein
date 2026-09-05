## Scorecard implementation migrated from `replay/` to `persistence/`

The `bernstein runs scorecard` subcommand now builds the per-run scorecard
artifact through `bernstein.core.persistence.run_scorecard.build_run_scorecard`
and `bernstein.core.persistence.run_scorecard.verify_scorecard` (working
over the run's `WorkLedger`) instead of the previous
`bernstein.core.replay.scorecard.derive_scorecard` projection of a sealed
journal. The signed content-addressed envelope is produced by
`bernstein.core.persistence.run_scorecard.write_scorecard_artifact`, which
supersedes `bernstein.core.replay.scorecard_artifact.write_scorecard_if_configured`.

This re-anchors the scorecard surface on the work-ledger persistence boundary
that already backs `bernstein runs inspect` and `bernstein runs verify`, so a
`runs scorecard --verify` pass and a `runs verify` pass share the same input
artefact and the same content-addressed store. The previous sealed-journal
projection is removed: the journal-derived counts it exposed are not part of
this release.

Migrators with existing `replay/scorecard.py`-shaped tooling should re-target
`build_run_scorecard(ledger)`; the new module returns the same document shape
when read with `--json`, and `verify_scorecard` raises on drift against the
on-disk envelope the way the previous verifier did (#5404).
