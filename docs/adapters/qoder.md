# Qoder (Alibaba)

Registry key: `qoder` - Binary: `qodercli`

[Qoder](https://qoder.com/en/cli) is Alibaba's agentic coding platform. Its
command-line client runs headless via the `-p` flag.

## Invocation

```bash
qodercli -p "<prompt>"
```

In headless mode `-p` runs prompt-type commands (file editing, command
execution, creating commits) without the interactive TUI, so the process
exits when the task is done. TUI-only commands are unavailable headless.
Qoder resolves the model from its own `/model` configuration, so no model
flag is passed on argv.

## Auth

For non-interactive terminals, supply a personal access token from the Qoder
Integrations page. The adapter forwards `QODER_API_KEY` and `DASHSCOPE_API_KEY`
(Alibaba Cloud Model Studio).

## Strategy

| Axis | Value |
|---|---|
| Resume | unsupported (fresh session per run) |
| Dangerous mode | unsupported (no skip-permissions flag; `-p` runs prompt commands headless) |
| Event channel | text-signals |

## Conformance

Contract: [`tests/contract/contracts/qoder.yaml`](https://github.com/sipyourdrink-ltd/bernstein/blob/main/tests/contract/contracts/qoder.yaml).
Golden transcript: `tests/golden/qoder_adapter.yaml`.
The nightly [conformance canary](conformance-canary.md) probes `qodercli`; a
last-green row appears once a passing receipt exists.
