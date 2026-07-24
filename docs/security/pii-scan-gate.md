# PII scan quality gate

Before a task's diff can merge, Bernstein scans every changed (or configured)
file for leaked secrets, credentials, and PII. The scan is regex-only - no
network calls, no LLM - and is one of the built-in gates in the quality-gate
pipeline. High-severity findings (leaked credentials) hard-block merge;
medium-severity findings (PII) are reported but do not block by default.

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
| Hardcoded password, DB/service connection string, bearer token, JWT | high | `password = "..."`, `postgres://user:pass@host/db` |
| Email address, phone number, SSN, credit-card number | medium | free-text PII in code or comments |

Only **high**-severity findings set `blocked=True` on the gate result
(`quality_gates.py:834-846`). PII findings (email/phone/SSN/credit-card) are
all medium severity, so a PII-only diff passes the gate but is still recorded
in the gate's detail output - the gate hard-blocks on leaked secrets, and
flags-but-allows on PII.

Two post-filters reduce false positives before a match is reported:

- **Credit-card numbers** must pass a Luhn checksum (`_looks_like_credit_card`).
- **High-entropy assignments** must contain a mix of uppercase, lowercase, and
  digit characters (`_has_mixed_case_and_digits`).

## What is skipped

- Lines matching a built-in allowlist: `example.com/.org/.net`, addresses
  starting `test@`/`user@`/`admin@`/`noreply@`/`no-reply@`, values containing
  `placeholder`/`changeme`/`your-api-key`/`xxxx`, `localhost`/`127.0.0.1`/
  `0.0.0.0`, and password assignments using known test values
  (`test`/`password`/`changeme`/`secret`/`admin`).
- Lines whose value carries a configured `pii_allowlist_prefixes` prefix
  (default: `FAKE`, `TEST`, `EXAMPLE`, `DUMMY`, `PLACEHOLDER`, `LOCALHOST`).
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
