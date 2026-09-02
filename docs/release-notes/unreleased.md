# Unreleased

Changes merged to `main` that are not yet part of a tagged release. Each
tagged release has its own page in this directory; this page carries what has
landed since the newest one.

Cutting a version empties this page: every entry the tag ships moves onto that
version's page in the release PR itself. `tests/unit/test_unreleased_notes_rotation.py`
holds the page to that — an entry naming an issue or PR a tagged release page
already documents fails the build. An entry that cites released work as context
rather than as its own attribution is exempted by hand there, with the reason.

## Verification that executes the diff

Every gate that looked at a worker's change read it: the review rubric, intent
verification and the cross-model check are model reads of the diff text, and
the generated-integration-test lane writes one happy-path test. A changed
function that mishandles the empty list or the empty string reached the
reviewer through the channel that had already missed it. The `behavior_probe`
gate derives boundary inputs from the changed callables' own signatures and
runs them in the worktree, one probe per subprocess. Derivation uses no model
and no randomness, so a red verdict is replayable: the receipt on the gate
result carries the probe-set hash, every probe outcome, the minimal failing
call, and a reason code for each callable the deriver could not probe. The
claim is crash-level, not semantic — undocumented exception, return value
contradicting the return annotation, or no return inside the budget. Off by
default (#3377).
