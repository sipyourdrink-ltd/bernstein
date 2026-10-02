## The audit tail digest's non-dict guard is pinned by a test

`_hmac_chain_tail_digest` quotes the chain-tail HMAC from the newest audit
`.jsonl`. JSON Lines admits any JSON value per line, so a bare string, array or
number is well-formed input the parser hands back as a non-dict, and the
`isinstance` guard that rejects those was untested. Losing it would abort the
pack export with a `TypeError` on the first such line, whatever its JSON type.
Each shape is now covered, and so is the existing coercion of a non-string
`hmac` value (`{"hmac": 42}` is quoted as `sha256:42`) (#5950).
