# Fleet steering

Pause, resume, redirect, send guidance to, or abort a single running worker
mid-task, with every intervention recorded as a signed receipt before its
effect runs.

```
bernstein fleet steer <task_id> <pause|resume|guidance|redirect|abort> [flags]
```

## Why

Once a worker is running a task, an operator has no mid-task controls by
default: no pause, no way to queue guidance, no redirect of the objective, no
clean abort. Any ad hoc intervention - editing files under a worker, killing
its process - leaves no record, so an audited run a human touched can no
longer be explained end to end. Fleet steering closes that gap without
weakening the run's verifiability: **a steering action is a receipt first
and an effect second.**

## Command

```bash
bernstein fleet steer <task_id> pause --session-id <sid> [--reason "..."]
bernstein fleet steer <task_id> resume
bernstein fleet steer <task_id> guidance --guidance "watch the retry budget"
bernstein fleet steer <task_id> redirect --redirect-target "fix the auth path instead"
bernstein fleet steer <task_id> abort --session-id <sid> [--reason "..."]
```

| Flag | Applies to | Meaning |
|---|---|---|
| `--guidance TEXT` | `guidance` | Free-text guidance delivered to the worker. Required for this kind; rejected for every other kind. |
| `--redirect-target TEXT` | `redirect` | The new objective. Required for this kind; rejected for every other kind. |
| `--reason TEXT` | `pause` / `abort` | Optional human-readable reason. |
| `--session-id ID` | `pause` / `abort` | The worker session the effect targets. Required for these two kinds (they act on the worker process); ignored for the mailbox-only kinds. |
| `--adapter NAME` | `pause` | Adapter owning the session (used for the pause checkpoint). |
| `--worktree PATH` | `pause` | Worktree path (used as the pause checkpoint baseline). |
| `--principal NAME` | all | Declared operator seat, loopback attribution only (default `cli-operator`). |
| `--server URL` | all | Task server URL (default `$BERNSTEIN_SERVER_URL`). |
| `--token TOKEN` | all | Operator-scoped token (default `$BERNSTEIN_AUTH_TOKEN`). |
| `--json` | all | Emit the raw receipt JSON instead of a summary line. |

Every text field (`guidance`, `redirect_target`, `reason`) is capped at 2048
UTF-8 bytes.

## How it behaves

The CLI computes the command's payload hash locally, then `POST`s the
command to `/tasks/<task_id>/steer` on the task server. The server does the
same four steps, in this exact order:

1. **Validate.** The command shape is checked (right fields for the kind,
   size caps). A `pause` or `abort` without `--session-id` is rejected here.
2. **Authorize.** Only the read-write `operator` token scope may steer; a
   `viewer` scope or an absent credential is denied and the denial is
   recorded if a denial tracker is wired.
3. **Bind the receipt.** The confirmed command payload is hashed and bound
   into the HMAC audit chain as a `steering.receipt` event **before any
   effect runs.** If the CLI's locally-computed payload hash doesn't match
   what the server executes, the request is rejected with no receipt and no
   effect.
4. **Apply the effect**, referencing the just-written receipt hash:
   - `guidance` / `redirect` are delivery-only: posted to the task's mailbox
     as a `steer.guidance` / `steer.redirect` message.
   - `pause` captures a resumable checkpoint (adapter session + workspace
     baseline), writes a `PAUSE` signal file under
     `<signals_dir>/<session_id>/`, and parks the task's claim so the
     scheduler stops dispatching it.
   - `resume` reads the parked checkpoint, clears the `PAUSE` signal, and
     re-grants the claim.
   - `abort` writes a `SHUTDOWN` signal file under the target session's
     directory - a filesystem fact the worker/adapter honours out of band,
     never routed through the model.

Delivery rides the task's existing mailbox journal, so guidance and redirect
messages reach the worker in chain append order, exactly once, even
mid-tool-call. The worker consumes pending steering messages
(`consume_steering()`) and records each one as a first-class step in its
per-step journal, binding the step hash to the receipt hash. **A message
whose receipt cannot be found on the chain is rejected outright** - no
effect, no journal row - so an effect can never exist without a matching
receipt.

## Replay classification

Because every consumed steering message is a journal step, a steered run
replays byte-identically and is distinguishable from a tampered one:
`classify_steering_run()` reads a worker's journal and returns one of:

- `clean` - the journal's Merkle chain verifies and carries no steering
  steps.
- `steered` - the journal verifies and carries one or more steering steps
  (an operator touched it, and the record proves it).
- `tampered` - the journal's Merkle chain no longer verifies; the
  divergence report names the first divergent line.

## Limitations

- `pause` and `abort` require `--session-id`; there is no "steer the task,
  resolve the session automatically" mode.
- Steering acts on one running worker/session at a time - there is no batch
  or fleet-wide steer.

## Source

- `src/bernstein/core/orchestration/steering.py` - `SteeringCommand`,
  `SteeringController`, `consume_steering`, `classify_steering_run`.
- `src/bernstein/cli/commands/fleet_cmd.py` - `bernstein fleet steer`
  (`steer_cmd`).
