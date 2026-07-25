# Typed activity boundary

The deterministic scheduler is validated for coding agents. The typed activity
boundary is the one contract a non-coding modality -- research,
browser/computer-use, data, ops -- participates through as a replayable step:
every activity
returns an artifact plus the hashes needed to replay it, so the scheduler stays
deterministic and the agent stays an opaque stochastic activity behind a
hash-in / hash-out contract.

```
bernstein activity verify <run>
```

<!-- scope:activity-boundary-reachability start - delete this section when #2996 and #3110 land -->
## Reachability today

The boundary, its result contract, its refusals, and its verification path all
ship. Read the table before assuming a modality is one flag away.

| Surface | State on this release |
|---|---|
| `ActivityResult` contract, validation, journal anchoring | Ships. Covered by `tests/unit/test_activity_boundary.py`. |
| `bernstein activity verify <run>` | Ships. Verifies every activity already anchored in a run journal, across modalities. |
| Browser activity | Reachable from the CLI: `bernstein activity browser run --flow <f> --run <r>`. This is the only `dispatch_activity` call site in the tree. |
| Live browser driver | Needs `pip install 'browser-use>=0.7'`. The `browser` extra is deliberately empty so a default install stays license-clean; asking for a live driver without the backend raises a typed refusal naming the package. `--recording` drives a recorded tape with nothing extra installed. |
| Research activity | Python API only. `ResearchWorker.run()` requires a caller-injected `fetch_fn` and `synthesise`; it fetches nothing itself. |
| Data / ops activities | Python API only. `DataActivity` and `OpsActivity` have no caller outside `core/orchestration/`. |
| Dispatch from a goal-driven run | None. A `bernstein -g ...` run never dispatches a non-coding activity. No seed file, plan, backlog entry or server route constructs one (#3110). |
| Worktree allocation | Does not branch on modality, so a non-git activity still gets a git worktree (#2996). |
| Adapters declaring a non-git output mode | None. No shipped adapter declares `OutputMode.ARTIFACT`. |
| `agent_kind` on a role | Parsed, validated, and round-tripped by the team manifest. No scheduler code reads it, so setting it does not change how a run executes. |

Routing artifact-mode tasks away from the git-only paths is tracked in
[issue #2996](https://github.com/sipyourdrink-ltd/bernstein/issues/2996), and
declaring artifact output from a seed, plan, or backlog entry in
[issue #3110](https://github.com/sipyourdrink-ltd/bernstein/issues/3110). Until
those land, read this page as the contract plus one shipped CLI entry point
(browser), not as a set of modalities a seed file can select between.
<!-- scope:activity-boundary-reachability end -->

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

A team records which modality a role runs as via `agent_kind` on its
`[[roles]]` entry (default `coding`, so existing manifests are unchanged):

```toml
[[roles]]
agent_kind = "research"
role = "scout"
```

<!-- scope:activity-boundary-reachability start - delete this note when #2996 and #3110 land -->
The key is parsed, validated against the known modalities, and round-tripped
into the canonical manifest. It is not yet read on the execution path: a role
that declares `agent_kind = "research"` runs exactly as it would without the
key. Treat it as a forward-compatible declaration, not a dispatch switch.
<!-- scope:activity-boundary-reachability end -->

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

## Modalities

- **Research** and **browser/computer-use**: research content-addresses every
  fetched page at fetch time; browser records one observation hash per decision
  step. Both anchor identically to a coding spawn.
- **Data** and **ops**: a world-changing modality adds two guarantees on top of
  the same substrate.
  - **Deterministic-plan vs side-effecting split.** The activity records signed
    inputs, then derives a plan whose `plan_hash` is a byte-identical projection
    of those inputs and the declared steps, then records signed outputs. A side
    effect before a plan (or a new input after one) is refused, so the plan is
    provably the projection the effects were computed from.
  - **Signed input/output artifacts.** Every input and output is content-addressed
    and Ed25519-signed with the install key, bound into a `DataOpsReceipt` that is
    the activity's `artifact`. The receipt's canonical bytes hash to the anchored
    `artifact_hash` and are stored content-addressed, so `bernstein activity verify`
    reattaches the receipt offline and re-verifies the plan projection and every
    signature -- not just the evidence bytes. Strip the store or the journal and
    the receipt stops verifying, not merely stops logging.

## See also

- [`deterministic-replay.md`](deterministic-replay.md) -- the canonical event journal.
- [`stall-escalation.md`](stall-escalation.md) -- another journal-anchored receipt.
