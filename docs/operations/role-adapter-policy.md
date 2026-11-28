# Restrict adapters by role

The spawner reads `.sdd/security/role_adapter_policy.json` under its configured
project root. Each key is a task role (or the optional `*` default) and each
value is a list of adapter registry names. A named role takes precedence over
`*`. For example:

```json
{
  "*": ["claude"],
  "docs": ["codex"],
  "backend": []
}
```

Here `docs` permits only `codex`; `backend` remains unrestricted because its
named entry is empty; other roles permit only `claude`.

Use `bernstein security role-adapter-policy show` to inspect the policy,
`bernstein security role-adapter-policy set --role docs --allow codex` to save
a named allow-list, or `bernstein security role-adapter-policy set --role '*' --allow claude`
to configure a default for unlisted roles (quote `'*'` so the shell does not
expand it). Use `bernstein security role-adapter-policy test --role new-role --adapter claude`
to check an effective decision (exit code `0` for allowed, `1` for denied).
The runtime uses its project-root file; an alternate CLI `--policy-file` is
only applied to the file named by that CLI command.

A missing file or an empty object allows all adapters. Omitting `*` leaves
unlisted roles unrestricted. For a named role, an explicit empty list allows
all adapters; to remove a named restriction deliberately, run
`set --role docs` without `--allow`. By contrast, an empty `*` default
(`"*": []`, set with `set --role '*'` and no `--allow` arguments) denies every
adapter to unlisted roles (`show` reports "no adapters allowed").

A present but unusable policy stops dispatch. This includes a directory or
other non-regular file, an unreadable file, a dangling symlink, malformed JSON,
invalid UTF-8, a root value other than an object, or any role value other than a
list of strings. For example, `{"docs": "codex"}` is invalid. Fix the saved file
and retry; do not delete it to work around a refusal (deleting the file returns
to the missing-file unrestricted posture unless a separate startup contract
outside the policy file requires one). The older programmatic
`RolePolicy.from_dict` normalization is unchanged.

The spawner reloads the file before preparing a task workspace, before adapter
construction, and at dispatch. Cached adapters, crash resume, provider failover,
container and sandbox dispatch, and runtime bridges must pass the check. A
provider availability chain containing a forbidden adapter stops before health
probes. A denial is a hard stop, not a request to try another provider. Registry
identity is preserved through adapter caching; unregistered injected adapters
use their declared name. Bridges use their bridge name.

Resume always reads the configured project root, even when the preserved
worktree has its own policy file. Separate spawners do not share or replace the
process-global policy; if a caller installs a non-empty process-global policy
via `set_policy()`, the spawner enforces it alongside the project-root file so
either one can deny. A saved change applies to subsequent checks without a
restart. This does not revoke already running agents or make policy edits
atomic with workspace preparation; stop running work separately when needed.

Refusals raise `RoleAdapterDenied` and write `role.adapter.denied` to the local
HMAC audit chain at `.sdd/audit`. The event identifies the role, adapter,
allow-list and reason, including the reason a saved policy could not be used.
If the audit directory or key is unavailable, or writing the event fails, the
adapter is still refused and a warning records the audit failure; an audit
record is not claimed in that case.

## Scope

This covers `AgentSpawner` dispatch and resume paths (#6648) together with the
`*` wildcard default in `RolePolicy` and its CLI. Direct calls to adapter APIs
outside the spawner are not governed by this file. Model registry, executor
admission and other security gates still apply separately.
