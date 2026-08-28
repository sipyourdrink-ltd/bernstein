## Trust Record field surface matches the TRACE 0.2 draft

A spec-review pass against the producer-mapping draft (agentrust-io/trace-spec#231)
returned five field-level corrections to the Trust Record emitter #4684 shipped:
`subject` is now a self-certifying `did:key` URI instead of a bare run URN;
`enforce`, `runtime` (`platform` + `measurement`), `references[]`
(`rel`/`id`/`resolver`), and `appraisal` (`status` + `verifier`) are now
present; and the old `delegation` string is replaced by one Trust Record per
execution hop, linked through `parent_record_hash`. The signed seal proves
the journal presented matches what was sealed; it cannot prove that every
action was recorded. Closes #4692.
