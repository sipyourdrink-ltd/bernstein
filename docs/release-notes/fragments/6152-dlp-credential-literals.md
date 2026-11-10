## `dlp_scan` flags a provider key passed as an argument or written as a default

A credential parked in the default of a lookup passed every gate:

```python
API_KEY = os.getenv("API_KEY", "sk-live-...")
```

`dlp_scan` had no credential rules at all. `pii_scan`'s key rules are
assignment-shaped (`api_key = "..."`), and nothing on that line has that shape.
The default is what runs whenever the variable is unset, so it is committed
all the same. The same evasion works through `settings.get("K", "...")`,
`kwargs.setdefault(...)`, a parameter default and a dataclass field default.

`dlp_scan` now parses each changed non-test Python file and checks every string
or bytes literal in one of those positions against provider credential shapes:
live-mode payment keys (`sk_live_` / `sk-live-` / `rk_live_`), AWS access key
IDs, GitHub and Slack tokens, and Google API keys. A match is a
`credential_literal` finding and blocks the merge. A value that declares
itself live (`sk-live-`, `sk_live_`) is matched in any position, including a
plain assignment. AWS `ASIA` temporary keys count too. An allowlist word inside the
value (`AKIAIOSFODNN7EXAMPLE`) exempts it, except on a live-mode key, because
the provider reads the prefix and ignores the words after it. Filler bodies
(`sk_live_xxxxxxxx`), values that start with an allowlist prefix, and test
files are skipped. Credential findings retain file, line and rule information
with a source-free redacted marker, including for escaped sibling literals.
Turn the pass off with `dlp_check_credential_literals: false`,
or keep it as a warning with `dlp_block_credential_literals: false`.

The initial author-run pre-rollout sample reported no findings in 8,799 non-test
files of third-party packages, and none in bernstein's own `src/`, `scripts/` or
`examples/` beyond the gate-evasion fixture. That sample was not remeasured for
the later bytes, path-format and redaction corrections. The integrated
`gate-evasion-v1` baseline was remeasured with the locked Python 3.13 tools
(`ruff 0.17.2`, `pytest 9.1.1`, `vulture 2.16`) and records **4/8** caught.
