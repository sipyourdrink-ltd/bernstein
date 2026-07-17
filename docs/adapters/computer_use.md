# Browser / computer-use adapter (`computer_use`)

Every coding adapter under `src/bernstein/adapters/` models an agent whose
output is a file diff: the lineage recorder hashes the written content, chains it
under the operator HMAC, and Ed25519-signs the entry, so a coding run is
replayable and offline-verifiable.

Browser and computer-use agents (web tasks, form filling, back-office click
flows) do not fit that shape. They own their own decision loop and emit a stream
of GUI actions, not a diff. A browser agent that "completed" a signup flow leaves
no verifiable record of what it actually did: which fields it typed into, which
button it clicked, what the page looked like at each decision. Because such an
agent is non-deterministic by construction, absent an anchored action log it is
unauditable, which is exactly the property every other agent kind on the
substrate does not have.

The `computer_use` adapter family closes that gap. It fronts a **third-party
autonomous** browser / computer-use agent and instruments the boundary crossing
so each action the external agent decides on becomes an anchored, signed,
replayable record on the same substrate coding tasks already use.

> Scope: this admits an externally-driven agent that decides its own actions and
> makes that stream verifiable. A bernstein-driven browser worker where we own
> the step loop is a separate, disjoint surface.

## Per-action lineage anchor

For each action the external agent takes, the boundary builds an observation and
anchors it:

```
observation_hash = sha256(pre_action_screenshot_bytes + dom_accessibility_digest)
action_anchor    = sha256(canonical(prev_anchor, observation_hash, action))
```

- The pre-action screenshot bytes are stored once in the content-addressed store
  (`.sdd/cas/`) by SHA-256, so duplicate frames dedupe and replay retrieves the
  exact bytes the agent saw.
- `action` is the canonicalised action: kind (`navigate` / `type` / `click` /
  ...), target selector or URL, and a digest of any typed value. Only the digest
  is anchored, so a form-filling agent's secrets never enter the chain in plain
  text.
- Each `action_anchor` folds in the prior anchor, so the action stream is a
  single-parent Merkle chain. Every action is recorded through the lineage
  recorder with the prior anchor as its `parent_hashes`: HMAC-enveloped,
  Ed25519-signed, one entry per action. The anchor **is** the entry's
  `content_hash`, not a field bolted next to it. The head anchor is the run's
  identity.

A `computer_use.action` audit event mirrors `multimodal.attach`: it is the
per-action replay manifest, carrying the anchor, the prior anchor, the pre-action
screenshot's CAS SHA-256, the normalised DOM digest, the observation hash, the
canonicalised action, the signed lineage entry hash, the worker identity, the
worktree id, and the previous chain digest.

## Deterministic replay and divergence detection

Because each anchor folds in the exact observed bytes and the chosen action, a
recorded run replays by walking the chain: re-hash the stored pre-action bytes,
recompute each `action_anchor`, and compare against the signed chain head. Replay
needs only the lineage store, the audit chain, and the CAS that a fresh checkout
already ships; it never re-runs the agent.

A non-deterministic re-run that observed different bytes or chose a different
action surfaces as a **hash mismatch at the exact action index**, not a flaky
text assertion. `replay_run(...)` returns a `ReplayResult`; on divergence its
`ReplayDivergence` names the first differing action index and both the expected
(signed) and recomputed anchors.

This is the empirical verifiability property: flip one byte of one stored
pre-action screenshot and replay fails, naming the exact action index. A plain
implementation that writes screenshots as files with no anchor binding cannot
reproduce this, because it has nothing to recompute the tampered bytes against.

## Isolation

Two concurrent browser tasks share no profile state. `isolated_profile_dir(...)`
returns a per-session browser profile plus working directory under the worktree
(`.sdd/computer-use/profiles/<session_id>`); distinct sessions resolve to disjoint
directories by construction, so cookies cannot bleed between concurrent tasks.

## Terminal states

Driver failure and timeout never surface as free text. They map onto
`ComputerUseTerminalState` (`ok` / `driver_failure` / `timeout` / `refused`) via
`classify_terminal_state(...)`, and a driver fault is raised as a typed
`ComputerUseDriverError` carrying its terminal state.

## Capability gating

An incapable adapter handed a browser task raises `ComputerUseRefusal` before any
process launch, with `suggested_adapters` populated. This is the same structured
refusal the multimodal boundary raises (`ComputerUseRefusal` subclasses
`CapabilityRefusal`), just for the computer-use boundary. `is_computer_use_capable(name)`
is the predicate, mirroring `is_multimodal_capable(name)`.

## CAS-growth guard

Per-action screenshots are large. A per-run cumulative byte cap
(`DEFAULT_RUN_BYTE_CAP`, 64 MiB) fires a typed `ActionByteCapExceeded` refusal on
overflow, so a runaway run cannot silently balloon the content-addressed store.

## Where the pieces live

| Concern | Module |
| --- | --- |
| Anchor primitives, action vocabulary, capability predicate | `bernstein.core.agents.computer_use` |
| Recording session, replay, refusal, byte cap | `bernstein.core.agents.computer_use_attestation` |
| Adapter family + reference adapter + terminal states | `bernstein.adapters.computer_use` |
| `computer_use.action` audit event | `bernstein.core.security.audit_chain` |
| YAML contract | `tests/contract/contracts/computer_use.yaml` |
| Golden transcript | `tests/golden/computer_use_adapter.yaml` |

The concrete browser driver stays behind the adapter contract: the boundary and
anchor logic never import a specific browser tool.
