## Workflow resume examples pass the goal

The README (and its translations) and the workflow guide resumed a run
without `-g`. The goal is not saved with the run and `-g` defaults to an
empty string, so those examples ran the unfinished nodes with an empty
`{goal}`. The resume examples now pass the original goal, and the guide
lists what `spec_snapshot.json` actually records.
