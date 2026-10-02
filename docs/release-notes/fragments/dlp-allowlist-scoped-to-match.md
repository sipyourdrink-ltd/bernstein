## `dlp_scan` no longer drops a whole line because of one benign token on it

The DLP scanner's built-in allowlist (example domains, `test@`/`user@`
addresses, `localhost`, the words "placeholder"/"changeme", `xxxx`, and the
`FAKE`/`TEST`/... value prefixes) was checked against the entire line before any
rule ran. One match anywhere on the line skipped every rule on it, including the
critical ones that hard-block a merge: `{"email": "user@acme.io", "ssn":
"123-45-6789"}`, or a labelled card number followed by a `# placeholder`
comment, passed the `dlp_scan` gate.

The built-in allowlist now exempts a finding only when it overlaps the flagged
value itself, so `ssn = "FAKE-123-45-6789"` and the project's own copyright
header stay exempt while a real value next to an unrelated benign token is
reported. A fixture value earlier on a line no longer hides a real one after
it. Operator-configured `allowlist_patterns` still suppress the whole line, as
documented.
