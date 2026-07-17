# Jules Tools (Google)

Registry key: `jules` - Binary: `jules`

[Jules Tools](https://jules.google/docs/cli/reference/) is the command-line
companion for Google's asynchronous coding agent Jules. Its `remote new`
subcommand dispatches a session that runs in Jules' cloud sandbox against a
connected repository.

## Invocation

```bash
jules remote new --repo . --session "<prompt>"
```

| Token | Purpose |
|---|---|
| `remote new` | Create a remote session and return. |
| `--repo .` | Target the repository in the spawn cwd (the Bernstein worktree). |
| `--session` | Carry the task prompt. |

Jules executes remotely in its own isolated VM, so there is no local
permission gate, and it selects the model server-side (no model flag on argv).

## Remote-execution note

Unlike the local-subprocess adapters, Jules dispatches work to Google's async
cloud runner rather than editing the local worktree in place. This adapter
drives that dispatch and journals its output; treat its results as
remote-agent output rather than in-place local edits.

## Auth

Authenticate via `jules login` or set `JULES_API_KEY`.

## Strategy

| Axis | Value |
|---|---|
| Resume | unsupported (fresh session per run) |
| Dangerous mode | always-on (remote sandbox, no local permission gate) |
| Event channel | text-signals |

## Conformance

Contract: [`tests/contract/contracts/jules.yaml`](https://github.com/sipyourdrink-ltd/bernstein/blob/main/tests/contract/contracts/jules.yaml).
Golden transcript: `tests/golden/jules_adapter.yaml`.
The nightly [conformance canary](conformance-canary.md) probes `jules`; a
last-green row appears once a passing receipt exists.
