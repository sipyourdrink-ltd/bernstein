# Per-goal SLA contracts

The fleet SLO tracker (`bernstein slo`) reports three hardcoded aggregate
targets across the whole fleet, so a single recurring goal that quietly degrades
hides inside a green aggregate. Per-goal SLA contracts are the scoped
counterpart: an operator attaches a declarative contract to one recurring goal,
task family, or spend envelope, and the schedule supervisor evaluates it against
the evidence already on the audit chain, the work ledger, and the lineage spine.

A breach is not a dashboard blip. It is a signed, offline-verifiable receipt that
embeds the contract hash, the chain evidence of the miss, and the remediation
taken -- an artifact an operator can hand to a compliance reviewer instead of a
screenshot.

## What a contract declares

A contract binds to a subject (`schedule`, `task_family`, or `envelope`) and
declares one or more axes. Only the axes you set are evaluated.

| Axis | Meaning | Evidence source |
|---|---|---|
| `max_run_duration` | A run of the subject must finish within N seconds. | work ledger `task.started` / `task.completed` |
| `start_lateness` | The goal must start within N seconds of its fire instant. | `schedule.fire` events + work ledger |
| `fire_frequency` | The goal must fire at least once every N seconds. | `schedule.fire` events |
| `artifact_freshness` | The artifact this goal maintains must have been re-derived within N seconds. | lineage spine (no filesystem access to the artifact) |
| `spend_rate` | The subject's spend rate must stay under N USD/hour. | spend ledger / envelope rollup |

The contract is content-addressed: its canonical JSON is hashed into a
`contract_hash`, and the id is derived from that hash. Registering the same
contract body on two machines yields the identical id and hash; changing any
semantic field changes the hash.

Each contract carries its own error-budget policy (`budget_events` plus
burn-rate escalation tiers), generalising the hardcoded fleet targets into
operator-defined policy.

## Registering a contract

```bash
bernstein sla add \
  --subject-type schedule \
  --subject sched_nightly_triage \
  --start-lateness 300 \
  --max-run-duration 2400 \
  --artifact-freshness 90000 \
  --artifact-path .sdd/runs/nightly/report.md
```

`bernstein sla list` and `bernstein sla show <id>` render the registered
contracts; both accept `--json` for host-to-host diffing.

## How a breach is detected and recorded

Contracts are evaluated inside `schedule_supervisor.tick`, which already walks
schedules and writes fire events. The evaluation is **read-only** over chain
state and never dispatches a task. On a breach the supervisor:

1. assembles a signed **violation receipt** that embeds the evidence, the
   per-axis verdicts, and the deterministic remediation;
2. appends one `sla.violation` event to the HMAC audit chain, carrying the
   receipt digest and the previous chain digest;
3. normalises the breach into a `TriggerEvent` for the existing trigger pipeline
   (external delivery to automation platforms is out of scope here).

Those three are the only side effects of a breach.

## The receipt IS the report

The receipt verifies offline, byte for byte. `bernstein sla verify
<receipt.json>` reads nothing but the file: it recomputes the contract hash from
the embedded body, re-derives every axis verdict and the remediation from the
embedded evidence, walks the embedded chain-slice linkage, and checks the
Ed25519 signature. Flipping any single byte of the receipt fails verification.

```bash
bernstein sla verify .sdd/runtime/sla/receipts/sla_ab12cd34ef56-1700000000.json
# OK -- receipt sla_ab12cd34ef56-1700000000 verifies offline (contract 9f3c...)
```

Because freshness is judged purely from lineage-spine entries, a freshness
receipt embeds the exact spine entry hashes it judged; a reviewer confirms the
artifact was last re-derived outside the window without ever touching the
artifact bytes.

## Cost-aware, bidirectional remediation

Remediation is a pure function of the contract and the evidence window, so the
same receipt drives the same action every time. Contracts are bidirectional: a
deadline breach may remediate by spending more (a model upgrade), and a
spend-rate breach by throttling. A spend-more remediation is admitted or refused
by the budget-envelope dispatch gate; when refused, the receipt records the
blocked action and the deterministic fallback, and both decisions appear on the
audit chain.

## Error budgets as projections

`bernstein sla report <id>` projects a per-contract error budget over the work
ledger segment: remaining budget, burn rate, and escalation tier. The projection
is pure -- two independent checkouts holding the same ledger segment produce
byte-identical report JSON, so an operator and a stakeholder derive the same
numbers from the same history.

```bash
bernstein sla report sched_nightly_triage --json
```

## REST surface

The contracts, receipts, and reports are also served alongside the SLO routes:

| Endpoint | Returns |
|---|---|
| `GET /sla` | every registered contract |
| `GET /sla/{id}` | one contract |
| `GET /sla/{id}/report` | the deterministic error-budget report |
| `GET /sla/receipts` | the operator projection of every receipt |
| `GET /sla/receipts/{id}/verify` | the offline verification verdict |
