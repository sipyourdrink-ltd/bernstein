# Log redaction (PII filter)

Bernstein installs a `logging.Filter` on the root Python logger at startup
that rewrites email addresses, phone numbers, SSNs, and credit-card numbers
to `[REDACTED]` before any log handler emits the record. Because filters run
before handlers, every sink attached to the root logger - console, file,
structured JSON - receives the sanitised text; nothing upstream of the filter
is written to disk or stdout unredacted.

Source: `src/bernstein/core/observability/log_redact.py`.

## How it is installed

`install_pii_filter()` runs unconditionally as a module-level side effect
when `bernstein.core.orchestration.bootstrap` is imported:

```python
# core/orchestration/bootstrap.py
install_pii_filter()
```

There is no `bernstein.yaml` switch or environment variable to disable it -
the filter is attached on every Bernstein process startup. Calling
`install_pii_filter()` again (on the same logger) is a no-op: the function
stores the filter instance on an attribute (`_bernstein_pii_filter`) and
returns the existing one instead of double-attaching.

To protect a specific logger instead of the root logger:

```python
from bernstein.core.observability.log_redact import install_pii_filter
import logging

install_pii_filter(logging.getLogger("my_module"))
```

## What it redacts

| Pattern | Regex intent |
|---|---|
| `email` | standard `local@domain.tld` addresses |
| `phone` | US-style phone numbers, with or without a leading country code |
| `ssn` | `###-##-####` |
| `credit_card` | 16 digits, optionally grouped in 4s with spaces or dashes |

Matches are replaced with the literal string `[REDACTED]`. The filter
mutates `record.msg` (when it is a string) and, for `%`-style lazy logging,
each string value inside `record.args` (`dict` or `tuple`) - so both eager
f-string messages and lazy `logger.info("... %s ...", value)` calls are
covered.

These four patterns are the same ones the [PII scan quality gate](pii-scan-gate.md)
checks for at medium severity; the two mechanisms are independent (one scrubs
runtime log output, the other scans agent-produced diffs before merge) and
are not sharing state or configuration.

## Limitations

- **Regex-only.** No context awareness beyond the four patterns above; text
  that doesn't match one of them (names, addresses, free-text PII, secrets)
  is not touched by this filter. Secret/credential detection is a separate
  concern, covered by the [PII scan quality gate](pii-scan-gate.md).
- **Only `record.msg` / `record.args` are scrubbed.** Exception tracebacks
  attached via `exc_info=True` are rendered by the log formatter directly
  from the traceback object (`formatException`), which this filter does not
  touch. PII embedded in an exception's own message string or in a frame's
  local variables is not redacted when it is logged through `exc_info`.
- **Logging-module scoped.** Output that bypasses Python's `logging` module
  entirely - `print()`, direct stdout writes, structured audit-chain entries
  - is not covered by this filter.
- **No opt-out.** Because installation is unconditional at bootstrap import
  time, there is currently no supported way to disable the filter short of
  monkeypatching or not importing the bootstrap module.

## Related

- [PII scan quality gate](pii-scan-gate.md) - pre-merge secret/PII scanning
  of agent-produced diffs (a different mechanism, same four PII patterns).
