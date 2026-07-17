# Deterministic sandbox backend selector

The selector is a pure function that picks one sandbox backend
(`worktree`, `docker`, `e2b`, `modal`, `daytona`, `blaxel`,
`runloop`, `vercel`) given a workspace manifest, an operator policy,
and the credentials currently visible to the orchestrator. Same
inputs, same backend pick, every time. No I/O, no registry side
effects.

## Why it exists

Picking a sandbox by reading `os.environ` mid-spawn produced two
failure modes. First, plan replays and dry-runs disagreed with live
runs because the environment shifted between invocations. Second,
silent fall-throughs to a different runtime when the requested
backend lacked credentials hid configuration mistakes for hours.

The selector replaces that with three rules:

1. **Override-first.** An explicit `--sandbox <name>` flag (or the
   equivalent policy field) wins over every heuristic. Missing
   credentials raise `SandboxSelectionError` so the operator sees
   the failure loudly instead of getting a quiet runtime swap.
2. **Cost-aware default order.** When no override is set the
   selector prefers cheaper backends first (`worktree`, `docker`)
   and only escalates to paid cloud backends when the manifest
   demands a capability the cheaper backends cannot serve, or when
   the operator opted into paid execution.
3. **Capability-gated filtering.** Backends that cannot satisfy
   the manifest's required capability set are dropped before
   precedence runs. The selector's log lines explain why a given
   backend was skipped.

## Default precedence

```text
worktree -> docker -> e2b -> modal -> daytona -> blaxel
         -> runloop -> vercel
```

Backends not present in `DEFAULT_PRECEDENCE` are appended in
sorted-name order so plug-in backends still get a stable position.

## How to use it

Most callers materialise the registered backends with `list_backends`
and hand them to `select_sandbox`, which returns the chosen backend
instance directly:

```python
from bernstein.core.sandbox.selector import (
    SandboxEnvironment,
    SandboxPolicy,
    SandboxSelectionError,
    select_sandbox,
)
from bernstein.core.sandbox.registry import list_backends

policy = SandboxPolicy(allow_paid=False)  # cheap backends only
env = SandboxEnvironment(
    available_credentials=frozenset({"E2B_API_KEY"}),
    budget_remaining_usd=2.50,
)

backend = select_sandbox(list_backends(), policy=policy, environment=env)
```

Force a specific backend with an override:

```python
policy = SandboxPolicy(override="docker")
```

When no backend satisfies the policy, `select_sandbox` raises
`SandboxSelectionError`; its `attempted` attribute lists the backends
that were considered:

```python
try:
    backend = select_sandbox(list_backends(), policy=policy, environment=env)
except SandboxSelectionError as exc:
    print("no eligible backend; attempted:", exc.attempted)
```

## Configuration

| Knob | Default | Controls |
|---|---|---|
| `policy.override` | `None` | Force a named backend. Missing credentials raise `SandboxSelectionError`. |
| `policy.allow_paid` | `False` | When `False`, only `worktree` and `docker` are considered. |
| `policy.required_capabilities` | `{FILE_RW, EXEC}` | Capabilities the chosen backend must advertise. |
| `policy.required_credentials` | `frozenset()` | Env-var names the backend must have. |
| `policy.precedence` | `DEFAULT_PRECEDENCE` | Custom ordering; unmentioned backends are appended alphabetically. |

## Limitations

- The selector does not run health checks. A docker daemon that is
  installed but offline still counts as available; the spawner
  reports the failure once it tries to connect.
- Budget-driven escalation (e.g., switch from `worktree` to
  `e2b` once a CPU budget is exceeded) is not in this slice.
- The selector is a pure function. Composition with the registry
  is the caller's responsibility; a caller that ignores the
  returned `name` and resolves a different backend defeats the
  determinism guarantee.

## Pools as a declared selector input

A [named sandbox pool](../operations/sandbox-pools.md) composes with the selector
without changing it. `pool_to_sandbox_policy` maps a merged pool placement into a
`SandboxPolicy` (required capabilities from the merge, precedence from the pool
backend allowlist, an exposed `backend` override as the policy override), and
`select_pool_backend` filters the candidate backends to the allowlist before
delegating to `select_sandbox`. The selector stays a pure function; the pool is
just another declared input, so with no pool configured selection is
byte-identical to today.

## Related

- Source: `src/bernstein/core/sandbox/selector.py`
- Pool placement: `src/bernstein/core/sandbox/pool_placement.py`
- Registry: `src/bernstein/core/sandbox/registry.py`
- [Named sandbox pools](../operations/sandbox-pools.md)
- [Sandbox backends](../architecture/sandbox.md)
