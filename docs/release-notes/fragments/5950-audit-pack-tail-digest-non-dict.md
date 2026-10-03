## Audit pack tail-digest scan now has regression coverage for non-dict, malformed, and non-string-hmac JSONL lines

`_hmac_chain_tail_digest` scans an audit-chain JSONL file for the last entry
carrying an `hmac` field, quoting it in the evidence pack as a single
content hash for the whole chain. Every existing test wrote one well-formed
`{"hmac": ...}` dict per line, so several branches were never exercised:
the `isinstance(entry, dict) and "hmac" in entry` guard that excludes a
non-dict JSON value (a bare string, an array, a number, `null`, a boolean);
the `except json.JSONDecodeError: continue` branch that skips a line that
fails to parse at all, including one truncated mid-write, the shape a
process that died mid-append actually leaves behind; and the existing
coercion of a non-string `hmac` value (`{"hmac": 42}` is quoted as
`sha256:42`).

New tests pin all three: every non-dict JSON type, two kinds of malformed
line (invalid JSON and truncated JSON), and a non-string `hmac` are all
handled correctly, and the scan still finds the right, later `hmac` entry
across them. Losing any of these guards would abort the pack export with a
`TypeError` or `KeyError` on the first such line, rather than silently
quoting the wrong tail - no mutation of any guard produces a
wrong-but-plausible hash (#5950).
