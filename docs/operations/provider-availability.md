# Provider availability policy

Upstream agent CLIs and models change under operators with little notice: a
backend gets discontinued, a model is withdrawn for weeks, a provider rate
limit turns into an outage mid-run. Unattended fleets need routing that
survives a provider disappearing -- without a human watching the first
failure.

The `provider_availability` section of `bernstein.yaml` declares, per role,
an ordered fallback chain of `(adapter, model)` pairs. Before dispatch the
scheduler health-probes the chain (results cached for a configurable TTL)
and picks the first healthy element. Every decision is a deterministic
projection of the declared chain and the recorded probe outcomes, mirrored
into the HMAC-chained audit log as a routing receipt -- two operators
holding the same recorded probe set replay the same routing, byte for byte.

## Config

```yaml
provider_availability:
  probe_ttl_minutes: 5        # how long a probe result is cached
  probes_enabled: true        # set false for offline runs
  roles:
    developer:
      conformance_floor: advanced
      chain:
        - {adapter: claude, model: opus, conformance: expert}
        - {adapter: codex, model: gpt-5.2, conformance: advanced}
    reviewer:
      conformance_floor: basic
      chain:
        - {adapter: gemini, model: gemini-3-pro, conformance: advanced}
        - {adapter: qwen, model: qwen3-coder, conformance: basic}
    default:
      conformance_floor: basic
      chain:
        - {adapter: claude, model: sonnet, conformance: advanced}
        - {adapter: codex, model: gpt-5.2, conformance: advanced}
```

Rules:

- **Chains are ordered.** Position 0 is the role's primary; dispatch walks
  the chain and picks the first element whose health probe passes.
- **Conformance floors are enforced at config validation time.** Every chain
  element declares a conformance level (`basic` | `advanced` | `expert`);
  an element below the role's `conformance_floor` is rejected when the
  config loads, never silently at dispatch. A fallback is therefore never
  less capable than the role requires without the operator saying so.
- **`default` applies to roles without their own chain.** Roles absent from
  `roles` (and with no `default` entry) keep the ordinary
  `role_model_policy` routing.
- **Probes are cheap and offline-safe.** The built-in probe checks that the
  adapter binary is on `PATH`. With `probes_enabled: false` (or in offline
  runs) every element is presumed healthy and the presumption itself is
  recorded on the receipt (`probe_kind: disabled`).

## Routing receipts

Every dispatch through a declared chain appends a `routing.failover_receipt`
event to the audit chain (`.sdd/audit/`), recording:

- the role and task id,
- the full chain considered (`adapter`/`model`/`conformance` per element),
- the recorded probe outcomes, position for position,
- the chosen chain position and the reason (`primary_healthy`, `failover`,
  or `no_healthy_provider`),
- the deterministic decision hash (`sha256:` over the canonical decision
  projection).

The decision is a pure function of the chain and the recorded probe
outcomes. Replaying the recorded probe set recomputes the identical
projection and hash, so a reviewer can verify -- from the chain alone --
why a fallback fired and that the dispatched provider was the deterministic
choice, not an override and not a race.

When no chain element is healthy the spawn is refused loudly (instead of
dispatching into a dead provider) and the refusal is receipted too, keeping
the outage window reconstructable.

## Failover drill

```bash
bernstein doctor --failover-drill          # human-readable table
bernstein doctor --failover-drill --json   # machine-readable, for CI
```

The drill probes every declared chain element and evaluates each position
as the dispatch target under a simulated outage of all its predecessors --
i.e. it exercises every declared fallback path. The exit code is non-zero
when any declared chain element is broken, and zero when all are healthy.
Each drill row carries the deterministic decision hash its simulated outage
prefix would produce, so operators can compare a drill against a later real
outage's routing receipt.

### Drill in CI

```yaml
# .github/workflows/failover-drill.yml
name: failover-drill
on:
  schedule:
    - cron: "0 6 * * 1" # weekly, before the work week starts
jobs:
  drill:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - run: pip install bernstein-cli
      - run: bernstein doctor --failover-drill --json
```

A red drill means a declared fallback path would not hold in a real outage:
fix the chain (or the runner image) before the outage finds it.

## Recommended chains per role

| Role | Primary | Fallback guidance |
|---|---|---|
| `developer` / `backend` | strongest coding model | one cross-vendor fallback at `conformance: advanced` or above |
| `reviewer` | mid-tier model | a second vendor's mid-tier; reviews tolerate more model variance than implementation |
| `docs` / `janitor` | cheap model | any `basic` element; set the floor to `basic` |
| `default` | your `cli: auto` resolution | at least one cross-vendor element so a single-vendor outage never idles the fleet |

Keep chains short (2-3 elements). A long chain hides capability drift; the
conformance floor makes the capability contract explicit, but only the
elements you actually drill are elements you can trust.
