# Executor admission policy

`bernstein.yaml` already *selects* an adapter, a model and an endpoint profile.
It does not let you say which of those a repository is allowed to use at all, so
a mis-pinned role or a stray `--adapter` runs on an executor nobody approved and
the only trace is a log line.

The optional `admission:` block closes that gap. It declares the executors this
repository may spawn on; every spawn is checked against the declaration before
any agent process starts, and every decision is recorded.

## The block

```yaml
goal: Ship the payments service

admission:
  mode: enforce            # enforce (default) | warn | off
  rules:
    - id: approved-adapters
      effect: allow
      adapters: [claude, codex]
      models: ["claude-*", "gpt-5-*"]
      sandboxes: [docker]

    - id: research-on-internal-endpoint
      effect: allow
      roles: [researcher]
      adapters: [openai_agents]
      endpoints: ["https://llm.internal.example/*"]
      sandboxes: [docker]

    - id: no-unsandboxed-work
      effect: deny
      sandboxes: [none, worktree]
```

## How a spawn is judged

Each spawn is reduced to one **subject** - six axes describing the executor that
would run it:

| Axis | Value at spawn time |
| --- | --- |
| `roles` | The agent role being spawned. |
| `adapters` | The adapter that will serve the spawn. |
| `models` | The model that adapter will run. |
| `endpoints` | The endpoint base URL, or empty when the adapter uses its own. |
| `sandboxes` | The sandbox tier (see below). |
| `task_types` | The task's type: `standard`, `fix`, `research`, `upgrade_proposal`. |

Evaluation order is fixed, so the same config and the same spawn always reach the
same rule:

1. Every `effect: deny` rule is evaluated first, in declaration order. An
   explicit deny can never be re-opened by a later `allow`.
2. Then the first matching `effect: allow` rule admits the spawn.
3. A subject matching no allow rule is **refused**. The block is fail closed:
   widening it is an edit to the config, not an omission in it.

Every axis matches by shell glob (`fnmatch`, case-sensitive). An axis a rule
omits does not constrain that rule - `adapters: [claude]` alone admits any model
on the `claude` adapter. An `allow` rule must constrain at least one axis, so a
policy cannot be opened by accident; write `adapters: ["*"]` when you mean it.

An empty endpoint (the adapter's built-in one) is matched by `"*"` but not by a
URL pattern, so `endpoints: ["https://llm.internal.example/*"]` refuses a role
that fell back to the adapter default.

### Sandbox tiers

The `sandboxes` axis names the boundary the agent runs inside, most specific
first:

1. the bound sandbox backend's name (`docker`, `e2b`, `modal`, …);
2. the container runtime named by an enabled `sandbox:` block;
3. otherwise the isolation mode - `container`, `worktree` or `none`.

## Modes

| Mode | Effect |
| --- | --- |
| `enforce` | A refused spawn raises and no agent process starts. |
| `warn` | The spawn proceeds; the refusing rule is still recorded. |
| `off` | The spawn proceeds; the evaluation is still recorded. |

`warn` is how you stage a policy on a live repository: run for a while, read the
recorded decisions, then flip to `enforce`.

## What a decision leaves behind

Every spawn - admitted or refused - writes a decision record to
`.sdd/runtime/spawn_admission/<session-id>.json`:

```json
{
  "allowed": false,
  "effect": "deny",
  "mode": "enforce",
  "reason": "admission denied by rule 'no-unsandboxed-work'",
  "rule_id": "no-unsandboxed-work",
  "session_id": "backend-1f2e3d4c",
  "subject": {
    "adapter": "claude",
    "endpoint": "",
    "model": "claude-sonnet-4",
    "role": "backend",
    "sandbox": "worktree",
    "task_type": "standard"
  }
}
```

An admitted spawn records the rule id that admitted it, so a replay of the same
config against the same spawn identity reproduces the decision rather than
re-deriving it.

A refusal additionally appends an `admission_refusal` event to the HMAC-chained
audit log under `.sdd/audit/`, alongside the `capability_matrix_refusal` events
from the [capability matrix](../security/capability-matrix.md). Both classes of
blocked spawn are therefore readable from one verifiable chain:

```bash
bernstein audit query --event-type admission_refusal
```

## Checking the policy before a run

`bernstein admission check` evaluates the declared policy against the executors
the current config selects and prints the decision table. Nothing is spawned.

```console
$ bernstein admission check
Admission policy mode: enforce
ROLE        ADAPTER  MODEL            ENDPOINT  SANDBOX  DECISION  RULE
backend     claude   claude-sonnet-4  -         docker   allow     approved-adapters
researcher  codex    gpt-5-codex      -         docker   refuse
```

The command exits non-zero when any row is refused, so it works as a CI check
that a config change has not made a role unspawnable. Pin a hypothetical subject
to check a case the config does not pin:

```bash
bernstein admission check --role researcher --adapter claude
bernstein admission check --json
```

## Failure modes

- **A malformed block refuses.** An unknown key, an unknown `effect`, or a
  duplicate rule id fails at config load with the offending key named, and the
  spawn gate refuses rather than reading a broken policy as an absent one.
- **A duplicate rule id is rejected** because a decision record naming it would
  be ambiguous, and replay could not tell which rule decided.
- **No block means no gate.** A repository that declares no `admission:` block
  spawns exactly as before.

## What this does not do

- It does not revoke an agent that is already running; the decision is made once,
  before the process starts.
- It does not control network egress - the sandbox backends own that.

## Programmatic external policy admission

Embedders can register existing OPA, Cedar or custom hooks and pass their
`PolicyHookRegistry` as `AgentSpawner(..., policy_hook_registry=registry)`.
The same spawner can be supplied to `Orchestrator(config, spawner, workdir)`.
This constructor integration governs actual `spawn_for_tasks` calls before
an adapter starts an action. It does not load external policies through the
CLI, worker command or `bernstein.yaml`; those entry points still require
an external policy configuration and attachment contract.

Native admission is checked first; an external allow cannot override its
refusal. With a registry supplied, all registered hooks are evaluated and
recorded in the existing `.sdd/audit` HMAC chain using the install audit key.
The request identifies the spawn session and role; its digest also binds the
complete resolved execution subject.

For an injected registry, this native-plus-external gate runs on each final
direct-adapter attempt, including provider/model failover. It uses that
attempt's adapter, model and effective `base_url` configuration, rather than
the spawner's primary adapter or an earlier role-policy endpoint. Refusal is
outside adapter retry handling: DENY, UNAVAILABLE, unsupported obligations or
an audit failure cannot trigger another executor. An authorized selected
adapter is not refused merely because the primary adapter would be denied.

Injected registries currently support direct-adapter fresh spawns only.
Runtime bridges, in-process backends, container/sandbox transports (including
sandbox sessions), and crash-resume refuse before their action because this
integration cannot bind their complete execution subject. This refusal does
not fall back to local execution. With no injected registry these paths and
the existing early native admission behavior are unchanged.

The spawn boundary requires at least one explicit ALLOW and refuses any DENY
or UNAVAILABLE outcome. An empty registry or only ABSTAIN outcomes refuses.
Unlike `PolicyHookRegistry.first_decisive`, an earlier ALLOW cannot hide a
later denial or unavailable engine. The registry's `fail_open` and
`default_verdict` settings do not weaken spawn admission. ALLOW obligations
are refused because the spawner cannot enforce them. Evaluation or audit
append errors also prevent the action. Each hook's original verdict remains
in the chain, including ALLOW with obligations and UNAVAILABLE; native
admission records continue to describe the native policy decision.

The supplied registry's registration list and audit destination are not
modified. No registry preserves the existing native admission behavior.
