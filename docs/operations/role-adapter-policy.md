# Role adapter defaults

The role adapter policy maps role names to adapter allow-lists in
`.sdd/security/role_adapter_policy.json`. A named role takes precedence over
the optional `*` default. Existing named empty lists remain unrestricted.

Configure a default for roles without their own entry:

```shell
bernstein security role-adapter-policy set --role '*' --allow claude
bernstein security role-adapter-policy show
bernstein security role-adapter-policy test --role new-role --adapter claude
```

Quote `'*'` so the shell does not expand it. No additional CLI flag is needed.
The JSON representation is the same mapping used by named roles:

```json
{
  "*": ["claude"],
  "docs": ["aider"],
  "backend": []
}
```

Here `docs` permits only `aider`; `backend` remains unrestricted because its
named entry is empty; other roles permit only `claude`. Omitting `*` preserves
the existing unrestricted behavior for roles with no entry.

An empty default (`"*": []`) denies every adapter to unlisted roles. Set it
with `set --role '*'` and no `--allow` arguments. The `show` command reports
this as "no adapters allowed", and `test` returns exit code 1 for a denied
adapter and 0 for an allowed one.

## Scope

This configures the existing policy predicate and its CLI. It does not change
how an orchestrator loads policy or which spawn/resume paths consult it.
Enforcement on those paths must be established separately. Missing, unreadable
or malformed policy files retain the existing unrestricted fallback; storing a
default in this file does not make policy-file deletion fail closed. A startup
requirement outside the policy file is a separate opt-in contract.
