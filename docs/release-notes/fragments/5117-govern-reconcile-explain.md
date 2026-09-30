## `govern reconcile --explain` names the layer behind every clause

`bernstein govern reconcile --explain <target> --policy-set layers.json` prints
one target's effective policy with the layer each clause came from, composed in
the fixed order: classification, baseline, instrumentation, then exactly one
class overlay.

Each row names the tier *and* the named layer — "the baseline said so" is not an
answer when the baseline is an ordered list of named sub-policies — and a clause
that overrode a lower layer says which one it displaced.

A target matching zero or more than one class overlay is reported as a finding
and exits 2, rather than resolved by whichever overlay declaration order reached
last. Its lower layers still print: posture that is not in doubt is not withheld
because the class is.

`--json` prints the same composition for a machine. The command reads and
composes only; it records nothing.
