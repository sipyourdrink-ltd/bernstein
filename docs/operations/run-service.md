# Detached run service

Submit a goal, disconnect the terminal, and reattach from any shell later
against a supervised run whose continuity across the detach boundary is
provable, not assumed.

```
bernstein run-service submit <goal> --task <id>...
bernstein run-service attach <run-id>
bernstein run-service status [<run-id>]
bernstein run-service stop <run-id>
bernstein run-service verify <run-id>
```

## Why

`bernstein run` ties a run's lifetime to the invoking terminal. For a single
long-running goal an operator wants to kick off and check back on later - not
a full multi-host cluster - `run-service` decouples execution from the
terminal on one host: a session-detached supervisor owns execution while a
durable work ledger owns state, and `attach` proves the ledger it reads is a
genuine continuation of the one last seen before rendering anything.
`bernstein worker` remains the separate multi-host fan-out path; this is
single-host detach/reattach.

## `submit`

```bash
bernstein run-service submit "Fix the auth regression" --task fix-auth --task add-tests
```

| Flag | Default | Meaning |
|---|---|---|
| `--task ID` (repeatable) | required | Task id to schedule; at least one is required. |
| `--foreground` | off | Advance the run in this process instead of spawning a detached supervisor. |
| `--per-task-delay SECONDS` | `0.0` | Dwell time per task, useful for making off-terminal progress observable. |
| `--backend {local,ssh}` | `local` | Execution backend for the run's tasks. |
| `--ssh-host HOST` | - | ssh host (required for `--backend ssh`). |
| `--ssh-path PATH` | - | Absolute remote dir under which per-task worktrees are provisioned (required for `--backend ssh`). |
| `--ssh-user USER` | - | Remote ssh user (optional). |
| `--ssh-port PORT` | `22` | Remote ssh port. |
| `--ssh-identity PATH` | - | Path to the ssh private key (optional). |
| `--ssh-repo PATH` | - | Remote repo path to git-worktree each task from (optional). |
| `--ssh-base-branch REF` | `main` | Base ref each per-task worktree branch is cut from. |
| `--ssh-secret ENV=PROVIDER` (repeatable) | - | Inject a vault credential into the remote environment variable `ENV`, resolved from the credential vault only. |
| `--workdir PATH` | cwd | Project root. |
| `--json` | off | Machine-readable output. |

Opening a run seeds the work ledger (`run.open` plus one `task.scheduled` row
per `--task`), persists a run descriptor, and signs a `submitted` lifecycle
receipt into the HMAC audit chain. **The goal text itself is never written to
the ledger - only its SHA-256 digest** (`goal_digest`), so the portable
ledger doesn't carry the goal's raw text. Without `--foreground`, a
session-detached supervisor process is spawned that survives the invoking
terminal; reattach later with `attach`.

### Off-host execution (`--backend ssh`)

`--backend ssh` runs each task off-host in its own isolated remote git
worktree (one branch per task) and appends a signed `run.ssh_task` receipt
binding that worktree, so an offline verifier can prove each task ran in
isolation across the ssh boundary. Remote credentials are resolved from the
credential vault only via `--ssh-secret ENV=PROVIDER` and never reach the
ledger or the receipts. A supervisor killed mid-run on the ssh backend
resumes from the ledger tip with zero lost completed tasks, the same
guarantee as the local backend.

## `attach`

```bash
bernstein run-service attach <run-id>
```

Reattaches from any shell: proves the current ledger head is a forward
extension of the head last seen - that proof, not a bare "still running"
check, is the reattach artefact - records a `reattached` receipt, and renders
the live projection (completed / in-flight / scheduled tasks).

Exit codes: `0` continuous, `1` no such run, `3` continuity broken (the
ledger diverged, or failed to verify).

## `status`

```bash
bernstein run-service status [<run-id>]
```

Shows supervisor liveness plus the ledger projection for one run. With no
`run-id`, lists every run known to the project (running state, completed /
in-flight / scheduled counts, closed state). Exit `1` when a named run does
not exist.

## `stop`

```bash
bernstein run-service stop <run-id>
```

Stops the run's supervisor process (SIGTERM, then SIGKILL after a grace
window) and records a `detached` boundary receipt so a later `attach` can
prove continuity across it. Exit `1` when the run does not exist; stopping an
already-stopped run still records the detach boundary and exits `0`.

## `verify`

```bash
bernstein run-service verify <run-id>
```

Re-verifies offline that the HMAC audit chain is intact, the work ledger
recomputes end to end, and every lifecycle receipt binds a ledger head that
genuinely exists in the chain (every reattach / daemon-restart boundary is a
true ancestor, not a fabricated one). Exit `0` verified, `2` a check failed
(each reason is listed).

## How continuity is proved

Every lifecycle boundary - submitted, reattached, detached - is a receipt
signed into the HMAC audit chain, and the run's state (completed / in-flight
/ scheduled tasks) is always a projection of the durable work ledger, never a
mutable side table. `attach` and `verify` both replay the ledger from
genesis rather than trusting a cached status, so a supervisor killed mid-run
resumes execution from the ledger tip with zero lost completed tasks, and a
tampered or truncated ledger fails `verify` at the exact position it
diverges.

## Source

- `src/bernstein/core/run_service/service.py` - `RunService` (submit,
  attach, project, detach).
- `src/bernstein/core/run_service/descriptor.py` - the run descriptor and
  `goal_digest()`.
- `src/bernstein/core/run_service/supervisor.py` - the detached supervisor
  process and liveness checks.
- `src/bernstein/core/run_service/ssh_runner.py` - the ssh backend.
- `src/bernstein/core/run_service/verify.py` - offline verification.
- `src/bernstein/cli/commands/run_service_cmd.py` - the `bernstein
  run-service` command group.

See also: [Known limitations §2](../reference/KNOWN_LIMITATIONS.md) for how
this fits alongside cluster/worker fan-out.
