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
