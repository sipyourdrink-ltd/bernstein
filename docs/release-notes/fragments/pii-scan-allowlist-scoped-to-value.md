## The pii_scan gate no longer drops a whole line for one benign value

`pii_output_gate` checked its allowlist (example domains, `test@`/`user@`
addresses, `placeholder`, `changeme`, `localhost`, the `FAKE`/`TEST`/...
prefixes) against the whole line before any rule ran, so a match anywhere on
a line suppressed every rule on it. A GitHub token beside a
`# replace placeholder` comment, a production database URL beside a
`# localhost in dev` note, or `{"email": "user@acme.io", "ssn": "123-45-6789"}`
produced no finding, and the `pii_scan` gate passed a change it now blocks
on (#6321).

The allowlist now exempts only a match that overlaps the benign value, the
same scoping the `dlp_scan` gate uses. Values such as `user@example.com`,
`postgres://...@localhost` and `"TEST_..."` are still skipped, and every
other match on the line is reported. The test-password exemption now covers
only the assignment (`password = "test"`), so it cannot reach over a real
token between `password` and a later `"admin"` on the same line. The
address and example-domain markers are anchored, so a real value that only
contains one (`sysadmin@acme-corp.io`, a database URL whose password is
`latest`, or is `admin` itself, `ceo@counterexample.com`,
`jane@example.com.au`) is no longer exempt. No configuration
changes; a line with no allowlisted value scans exactly as before.
