## A junk line in the audit log no longer threatens the evidence pack's tail

`_hmac_chain_tail_digest` quotes the chain-tail HMAC from the newest audit
`.jsonl`. JSON Lines admits any JSON value per line, so a bare string, array or
number is well-formed input the parser hands back as a non-dict -- and the
`isinstance` guard that rejects those was untested. Losing it would either
crash the pack export on a number or quote a bogus tail from a string
containing `hmac`. Both shapes are now covered (#5950).
