# PII scan quality gate

Before a task's diff can merge, Bernstein scans every changed (or configured)
file for leaked secrets, credentials, and PII. The scan is regex-only - no
network calls, no LLM - and is one of the built-in gates in the quality-gate
pipeline. Leaked credentials and regulated PII (SSNs, card numbers)
hard-block merge; other PII (emails, phone numbers) is reported but does not
block.

Source: `src/bernstein/core/security/pii_output_gate.py` (scan engine),
`src/bernstein/core/quality/quality_gates.py` (`_run_pii_gate` /
`run_pii_gate_sync`, gate name `pii_scan`).

## Enabling and configuring it

The gate is **on by default**. It runs whenever any file in the diff changes
(`condition: any_changed` in the default pipeline), and is a **required**
gate, meaning a high-severity finding blocks merge.

```yaml
# bernstein.yaml
quality_gates:
  pii_scan: true                          # default: true
  pii_scan_paths: ["src/"]                # scanned when no changed-file list is supplied
  pii_ignore_paths: []                    # glob-style paths to skip entirely
  pii_allowlist_prefixes:                 # value prefixes treated as fixtures
    - "FAKE"
    - "TEST"
    - "EXAMPLE"
    - "DUMMY"
    - "PLACEHOLDER"
    - "LOCALHOST"
```

When the gate runs as part of a task's diff review, only the files reported
as changed for that task are scanned. Outside of a diff context (or when no
changed-file list is available), it recursively scans `pii_scan_paths`.

For the surrounding gate framework (pipeline ordering, `required` vs
`optional`, custom gate plugins), see [Quality Pipeline](../architecture/quality-pipeline.md).

## What it detects

| Category | Severity | Examples |
|---|---|---|
| AWS access/secret key, GCP service-account JSON | high | `AKIA...`, `aws_secret_access_key = "..."` |
| Platform tokens | high | GitHub (`ghp_`/`gho_`/...), Slack (`xox...`), Stripe (`sk_live_`/`sk_test_`) |
| Private key (PEM) | high | `-----BEGIN ... PRIVATE KEY-----` |
| Generic API key / secret / high-entropy assignment | high | `api_key = "..."`, `token: "<24+ mixed-case+digit chars>"` |
| Hardcoded password, DB/service connection string, bearer token, JWT | high | `password = "..."`, a `postgres://` / `mysql://` / `mongodb://` URL with credentials |
| Email address, phone number, SSN, credit-card number | medium | free-text PII in code or comments |

The gate blocks on each finding's `block_merge`, not on its severity
(`_run_pii_gate` in `quality_gates.py`). A finding blocks when it is high
severity or its rule is in `BLOCKING_PII_RULES` (`ssn`, `credit_card_number`),
so an SSN or card number hard-blocks although it is reported as medium. Emails
and phone numbers neither block nor hide: they are still listed in the gate's
detail output.

Two post-filters reduce false positives before a match is reported:

- **Credit-card numbers** must pass a Luhn checksum (`_looks_like_credit_card`).
- **High-entropy assignments** must contain a mix of uppercase, lowercase, and
  digit characters (`_has_mixed_case_and_digits`).

## What is skipped

- Matches that overlap a built-in allowlist value: `example.com/.org/.net`
  and their subdomains (not a host that merely contains them, such as
  `counterexample.com` or `example.com.au`), addresses whose local part is
  exactly `test`/`user`/`admin`/`noreply`/`no-reply` (not `sysadmin@`,
  `contact.test@`, or a URL password such as `svc:admin@`), the whole
  names `placeholder`/`changeme` (not `/placeholders`, a
  `placeholder-proxy` host, an `app-changeme-2` database or a
  `db.changeme.prod.internal` host: they are bounded like the hosts below,
  so `-` and `_` join words and a following `.label` continues a host
  name), fixture values starting `your-api-key`/`your_api_key` (including
  optional separator spellings, also with operator-configured prefixes).
  The bare marker is not a DNS label or part of a host or database token,
  as in `db.your-api-key.prod.internal`, `app_your_api_key`,
  `your_api_key_db` or `your-api-key-2`. Other built-in markers include masking runs of
  `xxxx`, the hosts
  `localhost`/`127.0.0.1`/`0.0.0.0` (not `localhost-proxy`,
  `localhost.prod.internal`, `app_localhost`, `10.0.0.0` or `127.0.0.10`),
  and password assignments using known test values
  (`test`/`password`/`changeme`/`secret`/`admin`).
- Matches that overlap a value carrying a configured `pii_allowlist_prefixes`
  prefix (default: `FAKE`, `TEST`, `EXAMPLE`, `DUMMY`, `PLACEHOLDER`,
  `LOCALHOST`).

  The allowlist exempts the value, not the line: `postgres://...@localhost`
  and `ssn = "FAKE-123-45-6789"` are skipped, but a real token beside a
  `# placeholder` comment, or an SSN beside a `user@` address, is still
  reported.
- Paths matching `pii_ignore_paths` (glob or prefix match).
- Binary-ish file suffixes: `.pyc .pyo .so .dylib .whl .egg .gz .zip .tar
  .png .jpg .gif .ico .pdf`.
- For diff scans, only added (`+`) lines are checked - removed and context
  lines are never scanned, since the goal is catching *new* secrets.

## Output

Findings never store the raw secret. Each finding carries a redacted excerpt
(the matched span replaced with `***`, up to ~60 characters of surrounding
context) plus the rule name, severity, and line number. Gate detail output is
truncated at 2000 characters.

## Related

- [Quality Pipeline](../architecture/quality-pipeline.md) - the gate
  framework this gate plugs into (pipeline ordering, custom gates,
  `bernstein.yaml` schema).
- [Log redaction](log-redaction.md) - the sibling control that scrubs PII
  from Bernstein's own runtime log output (a different mechanism, applied to
  logs rather than agent-produced diffs).
