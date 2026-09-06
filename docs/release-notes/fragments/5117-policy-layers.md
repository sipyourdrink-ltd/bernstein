## Policy composes in a fixed order and says which layer wrote what

`PlaybookClause` declared posture as a flat list, with no notion of
layering and no record of where a clause came from.
`bernstein.core.govern.policy_layers` composes declared layers in one
fixed order — classification, baseline, instrumentation, then exactly one
class overlay — and every effective clause carries the layer AND the
layer's name, plus the sources it overrode. An operator asking "why is
this the effective value" reads the answer instead of the source.

A target matching zero or more than one class overlay is a reported
finding, not a silent default: taking whichever overlay the iteration
reached last is an answer that depends on file layout and changes when
somebody adds an unrelated overlay above it. An ambiguous target still
composes everything below the overlay, because the baseline applies
whatever the class turns out to be.

`PolicySet.content_hash` folds each layer's POSITION in with its content,
so reordering the baseline — same sub-policies, different precedence —
moves the hash. Without that it would be the one kind of edit a
desired-state diff cannot see (#5117).
