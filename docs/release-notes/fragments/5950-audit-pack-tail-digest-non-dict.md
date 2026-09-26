## Audit pack tail-digest scan now has regression coverage for non-dict and malformed JSONL lines

`_hmac_chain_tail_digest` scans an audit-chain JSONL file for the last entry
carrying an `hmac` field, quoting it in the evidence pack as a single
content hash for the whole chain. Every existing test wrote one well-formed
`{"hmac": ...}` dict per line, so the `isinstance(entry, dict) and "hmac" in
entry` guard that excludes a non-dict JSON value (a bare string, an array,
a number, `null`, a boolean) was never exercised, and neither was the
`except json.JSONDecodeError: continue` branch that skips a line that fails
to parse at all - including a line truncated mid-write, the shape a process
that died mid-append actually leaves behind.

New tests pin both: every non-dict JSON type plus two kinds of malformed
line (invalid JSON, and truncated JSON) are skipped without raising, and
the scan still finds the correct, later `hmac` entry across them. Losing
either guard would abort the pack export with a `TypeError` or `KeyError`
on the first such line, rather than silently quoting the wrong tail - no
mutation of either guard produces a wrong-but-plausible hash (#5950).
