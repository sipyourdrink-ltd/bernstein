## The pii_scan gate no longer drops a whole line for one benign value

`pii_output_gate` checked its allowlist (example domains, `test@`/`user@`
addresses, `placeholder`, `changeme`, `localhost`, the `FAKE`/`TEST`/...
prefixes) against the whole line before any rule ran, so a match anywhere on
a line suppressed every rule on it. A GitHub token beside a
`# replace placeholder` comment, a production database URL beside a
`# localhost in dev` note, or `{"email": "user@acme.io", "ssn": "xxx-xx-xxxx"}`
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
`jane@example.com.au`) is no longer exempt. The same holds for the local-host
and placeholder markers: `localhost-proxy.prod.internal`, `10.0.0.0`,
`127.0.0.10`, a `placeholder-proxy` or `db.changeme.prod.internal` host and
a `/placeholders`, `app-changeme-2`, `app_your_api_key`, `your_api_key_db`
or `your-api-key-2` database are real values, not markers. Built-in key
placeholder prefixes still exempt assigned fixture values, including with
operator-configured prefixes; they cannot exempt a database-name prefix.
Each match is checked against a per-line mask, so a long line dense with
markers and matches (a minified bundle, a source map) costs linear time. No
configuration changes; a line with no allowlisted value scans exactly as before.
