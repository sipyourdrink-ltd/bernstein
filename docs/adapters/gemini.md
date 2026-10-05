# `gemini` adapter - Google Gemini CLI

Bernstein's adapter for the Google Gemini CLI (`gemini` binary).

The hosted backend behind the free / AI Pro / Ultra path was
discontinued for non-enterprise users on **2026-06-18**. The `gemini`
adapter remains supported for Enterprise licenses (Standard /
Enterprise) and paid Gemini Enterprise Agent Platform API keys.
Consumer-path operators should use the separate [`agy` adapter](agy.md)
for the Antigravity CLI; the `antigravity` registry key is an alias of
that adapter (see [`antigravity.md`](antigravity.md)).

---

## Binary discovery

Deterministic, runs on every spawn:

1. **Operator override.** If `BERNSTEIN_GEMINI_BINARY` is set and
   non-empty, that binary is used. If it does not resolve on `PATH`
   the adapter raises `BinaryNotInstalledError`.
2. **`gemini`.** Otherwise the `gemini` binary is used.
3. **Hard error.** In strict mode (`bernstein adapters check`, doctor)
   a missing binary raises `BinaryNotInstalledError` naming the binary
   and the override env var.

Implemented in `bernstein.adapters.gemini.resolve_google_cli_binary`,
covered by `tests/unit/test_adapter_gemini.py::TestBinaryDiscoveryCascade`.

---

## Install

```bash
npm install -g @google/gemini-cli
gemini auth
```

---

## Operator override

For non-default install paths (vendored binary, pre-release build,
shadow install under a custom name) set `BERNSTEIN_GEMINI_BINARY`:

```bash
export BERNSTEIN_GEMINI_BINARY=/opt/google/gemini/bin/gemini
bernstein adapters check gemini
```

The override accepts an absolute path or a bare binary name that
resolves on `PATH`. Blank / whitespace-only values are treated as unset.

---

## Verifying the install

```bash
bernstein adapters check gemini
```

A passing row shows the binary path, the captured `--version` line,
and `conformance: ok` (`--help` advertised every flag the adapter relies
on: `-p`, `-m`, `--output-format`, `--yolo`).

---

## Contract

* Command line: `-p`, `-m`, `--output-format`, `--yolo`.
* Env-isolation allow-list: `GOOGLE_API_KEY`, `GEMINI_API_KEY`,
  `GOOGLE_CLOUD_PROJECT`, `GOOGLE_APPLICATION_CREDENTIALS`.
* Rate-limit meter label: `google_generative_language`.
* `STRATEGY_MATRIX`: `resume=UNSUPPORTED`, `dangerous_mode=CLI_FLAG`,
  `event_channel=STREAM_JSON`.

---

## Models

| Model | Notes |
|---|---|
| `gemini-3.1-pro` | Highest reasoning. |
| `gemini-3-flash` | Default in the Gemini app, Pro-grade reasoning at Flash speed. |
| `gemini-3.1-flash-lite` | Cheapest tier. |

No Anthropic models are available through this binary. Operators who
want Claude use the `claude` adapter.
