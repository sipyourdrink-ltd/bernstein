# Grok (xAI Grok Build)

Registry key: `grok` - Binary: `grok`

[Grok Build](https://docs.x.ai/build/overview) is xAI's terminal coding
agent. Bernstein drives its native headless mode.

## Invocation

```bash
grok -p "<prompt>" --output-format json --always-approve --no-auto-update
```

| Flag | Purpose |
|---|---|
| `-p` | Send one prompt and exit (no interactive TUI). |
| `--output-format json` | Emit a machine-readable result. |
| `--always-approve` | Auto-approve tool executions (dangerous mode). |
| `--no-auto-update` | Suppress the background update check for unattended runs. |

## Auth

Set `XAI_API_KEY` (or `GROK_API_KEY`). With the key present Grok Build
authenticates without a browser, so headless use needs no interactive login.

## Strategy

| Axis | Value |
|---|---|
| Resume | unsupported (fresh session per run) |
| Dangerous mode | cli-flag (`--always-approve`) |
| Event channel | stream-json |

## Conformance

Contract: [`tests/contract/contracts/grok.yaml`](https://github.com/sipyourdrink-ltd/bernstein/blob/main/tests/contract/contracts/grok.yaml).
Golden transcript: `tests/golden/grok_adapter.yaml`.
The nightly [conformance canary](conformance-canary.md) probes `grok`; a
last-green row appears once a passing receipt exists.
