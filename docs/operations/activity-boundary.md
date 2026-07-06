# Typed activity boundary

The deterministic scheduler is validated for coding agents, but the same control
plane runs research, browser/computer-use, data, and ops agents. The typed
activity boundary is the one contract that lets any modality participate as a
replayable step: every activity returns an artifact plus the hashes needed to
replay it, so the scheduler stays deterministic and the agent stays an opaque
stochastic activity behind a hash-in / hash-out contract.

```
bernstein activity verify <run>
```

## Why

Bernstein already writes one canonical, Merkle-chained event journal per run and
records every coding spawn against it. A non-coding agent -- one that fetches
pages, drives a browser, or runs a data/ops step -- had no uniform way to
participate as a replayable step, so its exogenous inputs sat outside the
determinism guarantee. The activity boundary closes that gap without a new
control plane: the same scheduler dispatches and journals a research or browser
activity exactly as it journals a coding spawn.

## The result contract

Every modality returns an `ActivityResult`:

| Field | Meaning |
|---|---|
| `kind` | The agent modality: `research` / `browser` / `data` / `ops` / `coding`. |
| `artifact` | The modality's opaque result payload (never inspected by the scheduler). |
| `artifact_hash` | SHA-256 of the canonical artifact projection. |
| `evidence_set_hash` | SHA-256 over the *set* of observation content hashes (order- and duplicate-independent). |
| `terminal_state` | Typed terminal state: `completed` / `refused` / `failed` / `timed_out`. |
| `reason_code` | A short, non-empty machine reason code for the terminal state. |

Each `Observation` is content-addressed at capture time: the bytes the agent saw
behind a decision (a fetched page, a screenshot, a DOM snapshot) are hashed the
moment they are captured, so the content hash is a forensic record of the exact
bytes. The `ref` (URL, decision-step label) is provenance only and is not part
of the content hash.

## What the scheduler enforces at the boundary

1. **Schema validation.** A result whose `artifact_hash` or `evidence_set_hash`
   does not recompute, whose `terminal_state` is not a known state, or whose
   `reason_code` is empty is rejected with a typed `ActivityRejected` refusal
   *before* it reaches the journal.
2. **Evidence pinning.** The `evidence_set_hash` is written into the run journal
   as one `activity.result` entry, so a replay reattaches the same bytes.
3. **Redundancy refusal.** The scheduler refuses to add a stage whose
   `evidence_set_hash` equals a prior stage's -- a stage that re-derives an
   already-seen exogenous signal introduces nothing new -- and logs the refusal.
4. **Audit mirror.** Each crossing is mirrored into the HMAC audit chain
   (`activity.result`) carrying only hashes, the kind, the terminal state, and
   the reason code -- never the artifact body or the fetched evidence.

## Declaring a role's modality

A team declares which modality a role runs as via `agent_kind` on its
`[[roles]]` entry (default `coding`, so existing manifests are unchanged):

```toml
[[roles]]
agent_kind = "research"
role = "scout"
```

## How verify reconstructs a run

`bernstein activity verify <run>` walks `<workdir>/.sdd/runs/<run>/journal.jsonl`:

1. Confirm the journal's Merkle chain recomputes cleanly (a tampered anchored
   field breaks the chain at a precise step).
2. For each `activity.result` entry, recompute the `evidence_set_hash` from the
   pinned observation hashes and compare it to the anchored value.
3. When the run's content store (`.sdd/cas`) is present, reattach each
   observation's bytes and re-verify the content hash.

A tampered journal entry or a divergent stored blob makes the reconstruction
diverge, and `verify` exits 2. Exit 0 is a clean verify; exit 1 means the run or
its activity entries were not found.

## Modalities shipped and deferred

- **Research** and **browser/computer-use** ship end to end: research
  content-addresses every fetched page at fetch time; browser records one
  observation hash per decision step. Both anchor identically to a coding spawn.
- **Data** and **ops** (deterministic-plan vs side-effecting split, signed
  input/output artifacts) build on the same substrate -- their side-effecting
  steps are observations over signed input/output artifacts dispatched through
  the identical boundary -- and are documented follow-ups.

## See also

- [`deterministic-replay.md`](deterministic-replay.md) -- the canonical event journal.
- [`stall-escalation.md`](stall-escalation.md) -- another journal-anchored receipt.
