# `antigravity` / `gemini` adapter - Google CLI (dual-binary)

Bernstein's adapter for the Google CLI. The upstream CLI is changing
binary names ahead of a deprecation date: the legacy `gemini` binary
stops serving free / AI Pro / Ultra subscribers on **2026-06-18**, and
the replacement `antigravity` binary carries the same model set and
the same `--output-format` semantics forward. Enterprise customers on
Standard or Enterprise licenses retain the legacy binary via paid
Gemini Enterprise Agent Platform API keys.

The adapter is **dual-binary aware**. At spawn time it discovers which
binary is on `PATH` and uses whichever the operator has installed. The
adapter contract (flags passed, env-isolation allow-list, sandbox
profile, network policy, rate-limit meter) is identical on either
binary; only the discovered binary name and the discovery step differ.

The same adapter is registered under two registry keys:

* `gemini` - back-compat for existing routines and operator muscle
  memory.
* `antigravity` - so `bernstein adapters check antigravity` and
  `bernstein run --cli antigravity ...` work the moment the new binary
  is on `PATH`.

---

## Discovery cascade

The cascade is deterministic and runs every time the adapter spawns:

1. **Operator override.** If `BERNSTEIN_GEMINI_BINARY` is set and
   non-empty, that binary is used. If it does not resolve on `PATH`
   the adapter raises `BinaryNotInstalledError` and exits.
2. **Prefer the new binary.** `antigravity` is checked next. If it
   resolves on `PATH`, it is used.
3. **Fall back to the legacy binary.** `gemini` is checked. If it
   resolves on `PATH`, it is used (operator on the transition path
   who has not migrated yet, or an Enterprise license holder).
4. **Hard error.** If none of the above resolves, the adapter raises
   `BinaryNotInstalledError` with a message naming both expected
   binaries and the override env var.

The cascade is implemented in
`bernstein.adapters.gemini.resolve_google_cli_binary` and is covered
by `tests/unit/test_adapter_gemini.py::TestBinaryDiscoveryCascade`.

---

## Install paths

### `antigravity` (recommended; required for free / Pro / Ultra after
2026-06-18)

The Antigravity CLI is a Go binary distributed via an upstream
installer. Follow the official transition notice for the current
install command for your platform:

* Transition notice:
  <https://developers.googleblog.com/an-important-update-transitioning-gemini-cli-to-antigravity-cli/>

After installing, run `antigravity auth` to populate the system
keyring; on a headless / SSH host the CLI falls back to a URL flow.

> **Operator note.** Bernstein does not ship any installer URL inside
> the package. Operators install the binary from the upstream
> installer of their choice and Bernstein discovers whatever is on
> `PATH`.

### `gemini` (legacy; Enterprise-only after 2026-06-18)

```bash
npm install -g @google/gemini-cli
gemini auth
```

The legacy binary keeps working for operators on Enterprise licenses
with paid Gemini Enterprise Agent Platform API keys. Free / AI Pro /
Ultra subscribers should migrate to `antigravity` before 2026-06-18.

---

## Operator override

For non-default install paths (vendored binary, pre-release build,
shadow install under a custom name) set `BERNSTEIN_GEMINI_BINARY`:

```bash
export BERNSTEIN_GEMINI_BINARY=/opt/google/antigravity/bin/antigravity
bernstein adapters check antigravity
```

The override accepts either an absolute path or a bare binary name
that resolves on `PATH`. Blank / whitespace-only values are treated
as unset.

---

## Verifying the install

```bash
# Confirm the binary is discoverable.
bernstein adapters check antigravity

# When only the legacy binary is on PATH this still reports useful
# information for the operator, even though the registry key is the
# new name.
bernstein adapters check gemini
```

A passing row shows the binary path, the captured `--version` line,
and `conformance: ok` (meaning `--help` advertised every flag the
adapter relies on: `-p`, `-m`, `--output-format`, `--yolo`).

---

## Models

The model set is identical on either binary:

| Model | Notes |
|---|---|
| `gemini-3.1-pro` | Highest reasoning. |
| `gemini-3-flash` | Default in the Gemini app, Pro-grade reasoning at Flash speed. |
| `gemini-3.1-flash-lite` | Cheapest tier. |

No Anthropic models are available through this binary. Operators who
want Claude continue using the existing `claude` adapter.

---

## What is not changing

* The adapter passes the same command line on either binary:
  `-p`, `-m`, `--output-format`, `--yolo`.
* The env-isolation allow-list still scopes the spawned process to
  `GOOGLE_API_KEY`, `GEMINI_API_KEY`, `GOOGLE_CLOUD_PROJECT`,
  `GOOGLE_APPLICATION_CREDENTIALS`.
* The rate-limit meter still labels upstream pressure under
  `google_generative_language`.
* The strategy declaration in
  `bernstein.adapters._contract.STRATEGY_MATRIX` is identical for both
  registry keys: `resume=UNSUPPORTED`,
  `dangerous_mode=CLI_FLAG`, `event_channel=STREAM_JSON`.

---

## Migration checklist

1. Install `antigravity` from the upstream installer.
2. Run `antigravity auth` to populate the system keyring.
3. Run `bernstein adapters check antigravity` and confirm the row
   reports the binary path and `conformance: ok`.
4. Optionally uninstall the legacy `gemini` binary. The adapter
   handles either ordering.

---

## Successor CLI (`agy`)

The hosted backend behind the non-enterprise path was discontinued in
June 2026. Consumer-path operators (free / AI Pro / Ultra) should use
the separate [`agy` adapter](agy.md) for the successor Antigravity CLI
(`agy` binary). This dual-binary adapter remains the enterprise /
API-key lane.
