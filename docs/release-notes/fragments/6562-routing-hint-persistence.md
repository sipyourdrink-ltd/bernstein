## Preserve task routing hints across restart

Task REST responses and the JSONL TaskStore now retain exact `model`, `effort`,
`cli` and `requires` declarations through single/batch creation, later records
and restart. Invalid capability declarations are rejected before REST
persistence; historical tasks keep their existing defaults. This preserves
routing data without adding fleet selection, dispatch or result acceptance.
Refs #6562.
