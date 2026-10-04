## Approval gates accept only signed decision records

The pre-spawn approval gate and the post-completion review gate used to treat
an existing `.sdd/runtime/approvals/<task>.approved` or `.rejected` file as
the operator's decision, whatever it contained. Both gates now accept a
decision file only when it is a signed decision record. A record names:

- the task and the outcome;
- the decision path: `cli`, `web`, `chat`, or `timeout-default`;
- the principal that decided;
- the nonce of the approval request it answers.

Its MAC is keyed from the install's audit key, which lives outside the project
directory. These writers produce records:

- `bernstein approve` / `bernstein reject`;
- the task server's `/approvals/{id}/approve` and `/reject` routes;
- `/approve` and `/reject` in `bernstein chat`.

A decision slot that holds anything else resolves the gate to **rejected**.
That includes an empty or plain-text file, a record for another task, and a
record answering an earlier request. The `approval_resolved` audit event then
says `decision_source: "unverified-file"` and gives the reason. The file is
moved aside as `<task>.<outcome>.unverified`.

Changes to the audit trail:

- `approval_resolved` events name the actual decision path and principal.
  Before, the pre-spawn gate recorded every file-based decision as `cli`.
- The review gate now writes `approval_resolved` events for file-based
  decisions and timeouts.
- `bernstein task resume` for a task parked `--until approval` also requires an
  authentic record.

**Behaviour change.** A bare decision file left by an older `bernstein
approve` / `reject` during an in-flight run is treated as unverified: that task
is rejected. Re-run the task and decide again with the upgraded CLI.

The CLI, the task server, and the orchestrator must resolve the same audit key
(`BERNSTEIN_AUDIT_KEY_PATH`). Otherwise records fail verification and tasks are
rejected.

The record stops a decision from being made by merely writing a file under
`.sdd/`. A process running as the same OS user can still read the audit key and
sign a record on purpose. Use a separate OS user or a sandbox for agents where
that boundary matters.
