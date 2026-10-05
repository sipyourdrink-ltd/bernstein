# `antigravity` registry key - alias of the `agy` adapter

`antigravity` is the product name of the Antigravity CLI, which ships
as the `agy` binary. The `antigravity` registry key resolves to the
[`agy` adapter](agy.md): same binary, same command line, same contract
and strategy.

```bash
# equivalent
bernstein run --cli antigravity "fix the flaky test"
bernstein run --cli agy "fix the flaky test"
```

## Install

The Antigravity CLI is a closed-source binary distributed by an upstream
installer; Bernstein does not ship an installer URL. Install it, run
`agy install` (environment paths and shell settings), sign in (keyring /
OAuth by default), then confirm discovery:

```bash
bernstein adapters check antigravity
```

The check looks for `agy` on `PATH` (override: `BERNSTEIN_AGY_BINARY`).
There is no binary named `antigravity`. See [`agy.md`](agy.md) for the
invocation, auth and conformance details.

## Changed behaviour

Earlier releases mapped `antigravity` to the Gemini adapter, with a
discovery cascade that looked for an `antigravity` binary before
`gemini` and passed the Gemini command line (`-m`, `--yolo`). The
Antigravity CLI does not accept those flags, so that mapping could not
spawn it.

* Configs that set `cli: antigravity` now spawn `agy`.
* Operators on the Gemini CLI (Enterprise / API-key lane) should select
  `cli: gemini`; that key is unchanged. See [`gemini.md`](gemini.md).
