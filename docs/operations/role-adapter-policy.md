# Restrict adapters by role

The spawner reads `.sdd/security/role_adapter_policy.json` under its configured
project root. Each key is a task role and each value is a list of adapter registry
names. For example:

```json
{"docs": ["codex"], "security": ["claude", "codex"]}
```

Use `bernstein security role-adapter-policy show` to inspect the policy, or
`bernstein security role-adapter-policy set --role docs --allow codex` to save
an allow-list. The runtime uses its project-root file; an alternate CLI
`--policy-file` is only applied to the file named by that CLI command.

A missing file, an empty object, an unlisted role, or an explicit empty list
allows all adapters for that role. To remove a restriction deliberately, use
`set --role docs` without `--allow`. An empty list does not mean deny all.

A present but unusable policy stops dispatch. This includes a directory or
other non-regular file, an unreadable file, a dangling symlink, malformed JSON,
invalid UTF-8, a root value other than an object, or any role value other than a
list of strings. For example, `{"docs": "codex"}` is invalid. Fix the saved file
and retry; do not delete it to work around a refusal. The older programmatic
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
process-global policy. A saved change applies to subsequent checks without a
restart. This does not revoke already running agents or make policy edits
atomic with workspace preparation; stop running work separately when needed.

Refusals raise `RoleAdapterDenied` and write `role.adapter.denied` to the local
HMAC audit chain at `.sdd/audit`. The event identifies the role, adapter,
allow-list and reason, including the reason a saved policy could not be used.
If the audit directory or key is unavailable, or writing the event fails, the
adapter is still refused and a warning records the audit failure; an audit
record is not claimed in that case.

This is partial implementation of #6648 for `AgentSpawner` dispatch. Direct
calls to adapter APIs outside the spawner are not governed by this file. Model
registry, executor admission and other security gates still apply separately.
