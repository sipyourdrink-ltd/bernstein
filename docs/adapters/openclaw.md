# OpenClaw CLI adapter

The `openclaw` adapter runs one embedded `openclaw agent exec` turn per
Bernstein task. It passes the prompt by file and uses the task worktree as
both the workspace and tool working directory. Bernstein captures the CLI's
JSON result and diagnostics in the task log and verifies the worktree diff
through its normal completion path. Each invocation gets fresh OpenClaw
state. The OpenClaw Gateway bridge is a separate integration.

## Install and select

Install the version verified by the adapter contract:

```bash
npm install -g openclaw@2026.11.3
openclaw agent exec --help
```

This release requires Node 24.16 or later on Node 24, or Node 26.1 or
later. Select `openclaw` as the task's CLI adapter and supply a
model, qualified with the provider for a built-in provider (for example
`anthropic/<model>`). If set, `BERNSTEIN_OPENCLAW_MODEL` must match that
model. Bernstein passes the task effort as a thinking level, mapping
`normal` to `medium`. Available levels depend on the provider and model.

## Authentication

- For a built-in provider, supply its API key in the launch environment.
  Bernstein forwards only `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`,
  `GEMINI_API_KEY` and `GOOGLE_API_KEY` from the provider credential
  environment. OpenClaw runs with `--auth-env-only`, which skips config
  and saved credentials.
- For an OpenAI-compatible gateway, supply
  `BERNSTEIN_OPENCLAW_OPENAI_BASE_URL` and
  `BERNSTEIN_OPENCLAW_OPENAI_API_KEY` together, and select a model ID
  understood by that gateway. Bernstein pins a generated config and a
  fresh private OpenClaw home. The config references `OPENAI_API_KEY`,
  which the worker receives from the dedicated gateway key. The key value
  stays out of the config and command arguments. OpenClaw rejects
  combining `--config` with `--auth-env-only`; the gateway invocation
  uses the pinned config and isolated home. The worker starts there to keep
  configuration loading away from a worktree-local `.env`. The CLI still
  receives the worktree as its explicit `--cwd`.

This adapter uses the direct subprocess backend, including local Git
worktrees. Bernstein container, remote sandbox and in-process backends
are unsupported. The configured gateway host and port must pass the active Bernstein network
policy before launch. The built-in-provider path requires an unrestricted
policy because the adapter cannot determine all provider endpoints. Run with
an OS-level process sandbox for filesystem and network confinement. In
this invocation OpenClaw defaults to host execution, with filesystem
tools scoped to the task worktree.

The Bernstein timeout also sets the OpenClaw deadline. A nonpositive
timeout disables both deadlines. A missing OpenClaw binary is reported by
the Bernstein worker in the task log with exit status 127.

[OpenClaw CLI reference](https://docs.openclaw.ai/cli/agent)
