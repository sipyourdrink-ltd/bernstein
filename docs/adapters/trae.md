# Trae Agent (ByteDance)

Registry key: `trae` - Binary: `trae-cli`

[Trae Agent](https://github.com/bytedance/trae-agent) is ByteDance's
open-source LLM agent for general software-engineering tasks. Its `run`
subcommand executes a single task non-interactively.

## Invocation

```bash
trae-cli run "<prompt>"
```

`run` completes after the task and exits, which suits CI and automation. The
agent plans and executes tool calls autonomously (no interactive permission
gate). Bernstein lets the child inherit the worktree cwd, so no
`--working-dir` is passed. Provider and model come from the Trae config.

## Auth

Provider keys are forwarded from the environment: `ANTHROPIC_API_KEY`,
`OPENAI_API_KEY`, `GOOGLE_API_KEY`, `OPENROUTER_API_KEY`.

## Strategy

| Axis | Value |
|---|---|
| Resume | unsupported (fresh session per run) |
| Dangerous mode | always-on (autonomous, no interactive permission gate) |
| Event channel | text-signals |

## Conformance

Contract: [`tests/contract/contracts/trae.yaml`](https://github.com/sipyourdrink-ltd/bernstein/blob/main/tests/contract/contracts/trae.yaml).
Golden transcript: `tests/golden/trae_adapter.yaml`.
The nightly [conformance canary](conformance-canary.md) probes `trae-cli`; a
last-green row appears once a passing receipt exists.
