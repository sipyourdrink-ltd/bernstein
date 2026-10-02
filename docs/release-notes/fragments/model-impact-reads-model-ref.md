## `bernstein model impact` can now return a result

`bernstein model impact <model>` joins the model registry to the model
reference a lineage entry carries, but it could never return an artefact. The
lineage store wrote `model_ref` into the log and then dropped it when reading
the log back, so every entry looked like it named no model and the rebuilt entry
no longer hashed to the one that was written. The same read-back also dropped
`activity_source` and `external_attestation`. A match, had there been one,
would have raised `AttributeError` on fields `LineageEntry` does not have.

The store now returns all three fields, so a re-read entry hashes to the entry
that was written. `impact` prints the entry hash, artefact path and agent, and
accepts the key `bernstein model registry` prints, `provider/model@version`
(`@*` or no version matches any snapshot), including model names that contain
`/`.

No write path in the orchestrator records a model reference yet, so on ordinary
runs the ledger holds none. The command now says that, rather than reporting
"no artefacts found" as if the model had produced nothing, and the feature
matrix lists `model impact` as Partial.

## The run-archive page no longer names a command that does not exist

`docs/observability/run-archive-retention.md` said `bernstein run archive` is
what survives a run. There is no such command: `run` takes a plan file, so
`archive` was read as a path. Creating and verifying an archive is available as
`create_archive` and `verify_archive` in `bernstein.cli.run_archive`, and the
page now says so and shows the call. A command for either is not provided.
