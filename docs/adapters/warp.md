# Warp Agent (`oz`)

Registry key: `warp` - Binary: `oz`

[Warp](https://docs.warp.dev/reference/cli/) ships its agent CLI as the `oz`
binary (the older `warp-cli` is deprecated). Its `agent run` subcommand
executes a task non-interactively.

## Invocation

```bash
oz agent run --prompt "<prompt>" --model <id>
```

`agent run` prints tool calls and responses as it works and exits when done.
It uses the current working directory by default, so Bernstein lets the child
inherit the worktree cwd rather than passing `-C`.

## Permissions

Warp governs permission behaviour through the selected **agent profile**
(`--profile`), not a per-run CLI flag. To run fully unattended, configure an
allow-all profile in Warp and set it as the account default. Because there is
no single skip-permissions flag, the adapter declares its dangerous-mode
strategy as unsupported and relies on the profile for autonomy.

## Auth

Warp authenticates via `oz login` (device auth cached under `$HOME`), so no
provider key is threaded onto argv or the child environment.

## Strategy

| Axis | Value |
|---|---|
| Resume | unsupported (fresh session per run) |
| Dangerous mode | unsupported (permissions governed by the Warp agent profile) |
| Event channel | text-signals |

## Conformance

Contract: [`tests/contract/contracts/warp.yaml`](https://github.com/sipyourdrink-ltd/bernstein/blob/main/tests/contract/contracts/warp.yaml).
Golden transcript: `tests/golden/warp_adapter.yaml`.
The nightly [conformance canary](conformance-canary.md) probes `oz`; a
last-green row appears once a passing receipt exists.
