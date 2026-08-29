## An aggregate record's member references carry their digest in `digest`

The `member-execution` entries on a run-level aggregate Trust Record put the member's content digest in `id` and emitted no `digest` field, so a verifier that content-binds references found nothing to bind. `id` now carries the member's `subject` — what names it inside the resolver — and `digest` carries the SHA-256 over the member's own bytes, matching how the sibling `produced-artifact` entry on an execution record already splits the two. The aggregate test vector is re-minted; the other three are unchanged.
