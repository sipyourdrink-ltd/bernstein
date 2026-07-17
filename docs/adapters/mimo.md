# MiMo Code (Xiaomi)

Registry key: `mimo` - Binary: `mimo`

[MiMo Code](https://github.com/XiaomiMiMo/MiMo-Code) is Xiaomi's open-source
terminal coding agent, built on the OpenCode core. Bernstein drives its
non-interactive `run` surface.

## Invocation

```bash
mimo run --model <id> --dangerously-skip-permissions "<prompt>"
```

| Token | Purpose |
|---|---|
| `run` | Execute a single task and exit. |
| `--model` | Select the model (forwarded when set). |
| `--dangerously-skip-permissions` | Run unattended without the interactive permission gate. |

## Auth

Set `MIMO_API_KEY`; the common provider keys (`ANTHROPIC_API_KEY` /
`OPENAI_API_KEY`) are also forwarded, matching OpenCode's multi-provider core.

## Strategy

| Axis | Value |
|---|---|
| Resume | unsupported (fresh session per run) |
| Dangerous mode | cli-flag (`--dangerously-skip-permissions`) |
| Event channel | text-signals |

## Conformance

Contract: [`tests/contract/contracts/mimo.yaml`](https://github.com/sipyourdrink-ltd/bernstein/blob/main/tests/contract/contracts/mimo.yaml).
Golden transcript: `tests/golden/mimo_adapter.yaml`.
The nightly [conformance canary](conformance-canary.md) probes `mimo`; a
last-green row appears once a passing receipt exists.
