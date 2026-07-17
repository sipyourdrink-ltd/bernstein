# CodeBuddy (Tencent Cloud Code Assistant)

Registry key: `codebuddy` - Binary: `codebuddy` (shorthand `cbc`)

[CodeBuddy](https://www.codebuddy.ai/docs/cli/headless) is Tencent Cloud's
terminal coding agent, shaped like the Claude Code CLI. Bernstein drives its
headless print mode.

## Invocation

```bash
codebuddy -p "<prompt>" --model <id> --output-format stream-json \
  --dangerously-skip-permissions
```

| Flag | Purpose |
|---|---|
| `-p` | Run non-interactively and print the result. |
| `--model` | Select the model. |
| `--output-format stream-json` | Emit newline-delimited JSON events. |
| `--dangerously-skip-permissions` | Required in non-interactive mode so authorized operations (file, shell, network) proceed without a prompt. |

## Auth

Set `CODEBUDDY_API_KEY`; the underlying provider keys (`ANTHROPIC_API_KEY` /
`OPENAI_API_KEY`) are also forwarded for BYO-model routing.

## Strategy

| Axis | Value |
|---|---|
| Resume | unsupported (fresh session per run) |
| Dangerous mode | cli-flag (`--dangerously-skip-permissions`) |
| Event channel | stream-json |

## Conformance

Contract: [`tests/contract/contracts/codebuddy.yaml`](https://github.com/sipyourdrink-ltd/bernstein/blob/main/tests/contract/contracts/codebuddy.yaml).
Golden transcript: `tests/golden/codebuddy_adapter.yaml`.
The nightly [conformance canary](conformance-canary.md) probes `codebuddy`; a
last-green row appears once a passing receipt exists.
