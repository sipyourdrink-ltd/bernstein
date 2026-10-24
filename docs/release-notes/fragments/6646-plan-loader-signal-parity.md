## Plan loader now rejects unknown signal types and preserves all supported ones

`_parse_completion_signals` previously had a hardcoded set of six signal types.
Artifact-mode signals (`schema_valid`, `criteria_match`, `hash_stable`) and newer
types (`figures_grounded`, `absence_verified`) were **silently dropped** — the task
reached the server with no gate, and the janitor could never verify it.

**Behaviour change (fail-closed):** three cases that previously warned and continued
now raise `PlanLoadError` at load time:

- A `completion_signals` entry that is not a mapping (e.g. a bare string).
- An entry whose `type` is not in the supported set.
- An entry with no value field (`value`, `path`, or `command`).

This is a breaking change for plan files that relied on the silent-skip behaviour.
The error message names the step, the index, and the unsupported type so the fix
is unambiguous.

The supported set is now derived from a single constant (`plan_schema.SUPPORTED_SIGNAL_TYPES`)
shared by the loader, the validator, and the parity test, so adding a new signal
type is a one-place change (#6646).
