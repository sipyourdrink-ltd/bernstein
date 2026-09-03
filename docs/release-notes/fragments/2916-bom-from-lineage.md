## `bernstein bom emit --from-lineage` (Issue #2916)

`bernstein bom emit --from-lineage` derives the AI-BOM snapshot from the run's
lineage spine instead of requiring a hand-assembled `bom_snapshot.json`. The
spine is HMAC-tagged, so the command uses the run's own audit key and never
mints a fresh one. Each model's `sha256` is the lineage entry hash of its first
invocation, so a reviewer can resolve any line item with `bernstein lineage
replay <run>` without trusting the document on its own. Two derivations of the
same run are byte-identical. (#2916)
