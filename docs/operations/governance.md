# Verifiable governance: RBAC, budgets, and seats

For teams, Bernstein expresses **role-based access control**, **budget
enforcement**, and **per-seat attribution** as deterministic projections over
the signed lineage spine rather than mutable, separately-logged database state.
Each governance decision is a signed, anchored record: strip the spine and it is
just a file; anchored, every decision recomputes offline and is independently
verifiable rather than merely recorded.

Module: `src/bernstein/core/security/governance.py`. CLI group:
`bernstein governance`.

## The decision record

Every access and budget decision is one record binding:

| Field | Meaning |
|---|---|
| `subject` | The seat / actor / user id the decision is about. |
| `action` | The requested permission string, or `budget` for a budget check. |
| `verdict` | `allow` / `deny` (access) or `allow` / `refuse` (budget). |
| `inputs_hash` | Content hash of the projection inputs the verdict was derived from. |
| `journal_entry_hash` | The lineage-spine entry hash over the record bytes: its chain-verifiable identity. |

Records land under `.sdd/lineage/<run>/governance_decisions/`, colocated with
the run's spine so the record and its anchor share one root.

## Access control (RBAC)

`decide_access` resolves a subject's IDP groups to a role via a **signed
`RoleBindings`** (IDP-group -> role, role -> permissions), then projects the
role's permissions onto the requested action. When the role grants the action
the verdict is `allow`; otherwise a signed `deny` record is written. A denied
action is still a signed, anchored record - not merely a log line. When a
subject's groups map to more than one role, the highest-privilege role wins
(`admin` > `operator` > `viewer`).

## Budgets

`check_budget_decision` recomputes the subject's cumulative spend from the cost
ledger (`.sdd/cost/ledger.jsonl`), never a stored counter. When prior spend plus
the next call would breach the per-subject cap, a signed `refuse` record is
written and the action is blocked (`BudgetRefused`). The operator policy inputs
(`cap_usd`, `next_cost_usd`) are carried in the record so a verifier can
recompute the verdict; the ledger-derived part - prior spend - is re-projected
at verify time.

## Seat / cost attribution

`seat_spend(ledger_path, subject)` is a pure projection over the ledger rows on
disk: every row is re-read and the matching subject's `cost_usd` summed. Two
operators holding the same ledger compute the byte-identical total. The
attribution dimension (`agent` / `task` / `role` / `feature_label`) is
selectable; `agent` is the per-seat default.

## Coverage: what a run can and cannot account for

A screen built out of decision records only ever shows the decisions that
exist, so it cannot show the actions no decision covers.
`src/bernstein/core/security/governance_coverage.py` projects that gap for one
run:

| Metric | Meaning |
|---|---|
| `attributable_action_ratio` | Recorded actions whose actor is named as the `subject` of some decision in the run. A spine entry's `actor` is a free-form string the writing adapter supplied; a decision naming it is where the installation resolved it to a principal. |
| `decision_coverage` | Recorded actions whose actor holds an `allow` verdict in the run. A `deny` or `refuse` attributes the actor without authorising it, so it counts in the denominator only. |

Three rules keep the numbers from flattering the run:

- Decision records are anchored into the same spine, so they are excluded from
  the action count. Otherwise a run would raise its own coverage by recording
  more decisions.
- The journal-head seal and artifact-attempt rows are chain bookkeeping, not
  agent actions, and are excluded the same way every other spine consumer
  excludes them.
- A run that recorded no actions reports `null`, not `0` and not `1`. Zero over
  zero is absent evidence.

`chain_status` travels with the numbers and carries the spine verify status
verbatim, so a `tampered` run cannot read as a clean one that merely scored
badly.

```
GET /governance/coverage?run_id=<run>
```

returns the canonical document. The route returns exactly the bytes
`governance_coverage_json` produces, so a number pinned from the dashboard
recomputes offline from `.sdd` alone.

## Verifying a dropped receipt

```
POST /governance/verify-receipt
```

The request body is a run receipt, verbatim. The response is the canonical
verdict document `src/bernstein/core/security/governance_receipt_verdict.py`
produces from
[`verify_run_receipt`](../reference/receipt.md) — the same verifier
`bernstein verify receipt` runs. Nothing under `.sdd` and no key material is
read, so the endpoint answers about the uploaded file and not about the
installation serving it.

| Field | Meaning |
|---|---|
| `status` | `ok`, `tampered` (a recompute or the signature diverged), or `malformed` (not a receipt at all, an empty upload included). |
| `tier` | `integrity-only` on a pass; `null` when the receipt did not verify. |
| `caveat` | Set exactly when `tier` is set. Names the key source, so the pass cannot be rendered as a bare tick. |
| `divergent_step` | The first divergent journal step, when journal tamper was located. |

The tier is always `integrity-only` because the signature is checked against
the key embedded in the receipt: that proves the file is internally consistent
and that no byte changed after signing, not who produced it. Provenance
requires the operator's key out of band —
`bernstein verify receipt <file> --public-key <pem>` — and no key can be pinned
through the endpoint, because a key arriving in the same request as the receipt
is the same channel rather than an independent anchor.

A receipt that does not verify is answered with `200` and a failing verdict.
The result is a statement about the evidence, not a failed request.

## Guarantees

- **Verifiability** - `bernstein governance verify <run>` re-resolves and
  re-projects every recorded access and budget decision from the presented
  bindings and ledger and confirms the recorded verdicts. Any single-byte tamper
  to a record, the bindings, or the spine fails the check.
- **Correctness** - a budget breach writes a signed refusal and blocks the
  action; per-seat spend is recomputable from the ledger rather than a mutable
  counter.
- **Determinism** - every decision row is canonical JSON and every field is a
  pure function of caller input, so two replays of a governance-gated run
  produce byte-identical decision records. No LLM in the decision loop.

## CLI

```
bernstein governance verify <run> --bindings bindings.json [--ledger ledger.jsonl]
```

Exit codes: `0` verified, `1` no records / bad input, `2` mismatch.

The `--bindings` file is a signed `RoleBindings` JSON (`RoleBindings.to_dict()`).
`--ledger` is required when the run carries budget decisions. Each recorded
decision is also mirrored into the HMAC audit chain as a `governance.decision`
event, so an operator can confirm from the chain alone that a decision bound the
claimed inputs to a named spine entry.

## `govern audit-compliance`: what this install can and cannot show

```
bernstein govern audit-compliance [--workdir .] [--only CMP] [--skip CMP-014]
                                  [--profile soc2] [--list] [--json-output]
```

Runs every registered check over the install and reports one finding per check.
A finding carries a stable id, one of three verdicts, and the evidence it read.

| Verdict | What it means | Carries |
|---|---|---|
| `measured` | the check read an artefact on disk | `passed`, plus `(locator, sha256)` for every locator probed |
| `declared` | the operator asserted the control in configuration; nothing was read that confirms it | no `passed` — the summary names the gap |
| `not_measurable` | the check could not run | what would make it measurable |

The compliance namespace (`CMP-001` … `CMP-023`) is the policy library in
`core/security/compliance_library.py`. Most of its checks test whether a key is
present in `bernstein.yaml` or `.sdd/config.yaml`, so they report `declared`: an
empty `auth:` section is a declaration, not a measurement. The five checks that
read the filesystem — the audit directory, the state directory, the incident
response document, the privacy document, the dependency lock file — report
`measured` and name the bytes they read. A locator that was probed and found
missing is recorded as `absent`, so a measured finding never carries empty
evidence.

`--profile <framework>` marks which ids that framework requires. It selects ids
and states nothing about the result: the findings are the same findings whichever
profile is named, and no output asserts that the install conforms to anything.

There is no score and no grade. The report ends in four counts —
`measured pass`, `measured fail`, `declared`, `not measurable` — each against
the number of checks that ran.

`govern audit-compliance` and `compliance check` both delegate to the same
`check_*` functions in `core/security/compliance_library.py`, so the snapshot
the policy engine evaluates and the findings the audit reports cannot re-decide
a control the other would deny — the only way the two surfaces can disagree
is by misreading the same library result.

Inventory topology is `bernstein govern inventory --render`.
See [govern inventory --render](govern-inventory.md).

## Reconciling the governed surface

`bernstein govern reconcile --propose` answers a different question from
`govern verify`: not "did these decisions recompute" but "is what is there
still what was decided".

```
bernstein govern reconcile --propose --desired desired.json [--workdir w] [--full]
```

The run enumerates four entity kinds -- registered adapters, cost lanes,
scheduled tasks, and declared capability entries -- into a snapshot stamped with
one `observed_at`, diffs that against the desired-state document, and writes the
result as one anchored governance decision record. Nothing else moves: no entity
is added, removed, or mutated, so the diff stays a reviewable artefact an
operator reads before anything executes.

Stable ids, one scheme per kind: an adapter is its registry key, a lane its lane
name, a scheduled task its schedule id, and a capability entry `<profile>/<axis>`
-- the profile that declares the axis, then the axis.

The desired-state document declares entities and per-kind defaults:

```json
{
  "v": 1,
  "defaults": {"scheduled_task": {"prune": false, "self_heal": true}},
  "entities": [
    {"kind": "lane", "id": "batch", "declared_value": "0.5", "self_heal": true}
  ]
}
```

Each entity classifies as `unchanged`, `new`, `changed`, `declared_but_absent`,
or `present_but_undeclared`. `prune` and `self_heal` then decide what is
proposed: an undesired entity under `prune: false` becomes a `hold` finding, never
a queued removal, and a drifted entity under `self_heal: false` is likewise held
rather than repaired.

`new` is relative to the previous run's own record, so a second run over an
unchanged environment reports nothing. By default only drifted entities print;
`--full` prints one line per entity.

Exit codes: `0` no drift, `1` unreadable desired state, `2` drift.

## Skip vs. suppress

`bernstein govern audit-compliance` is the compliance check set above. The check
contract and ID scheme are defined in issue #5072. Two mechanisms remove a
finding from a clean report, but they are different operations with different
governance semantics and different records:

| | `--skip ID` (check exclusion) | `bernstein audit suppress ID --reason ... --until DATE` |
|---|---|---|
| **What it means** | The check was excluded from the run before it ran. The finding was **never raised**. | A finding **was raised**; the operator has recorded a bounded-time decision to accept it as known-risk. |
| **When it applies** | At **plan / pre-check time**: the check does not appear in the run at all. | At **post-finding time**: the finding exists; the operator explicitly accepts it. |
| **Governance record** | No finding, no record. The exclusion itself is recorded by the mechanism that performed it (e.g., `--skip` on the audit run command, or the check registry's skip list). | A `GovernanceDecision` with `verdict=accepted`, `subject=<finding_id>`, `action=suppress`, and `context={reason, expiry}`. Anchored in the govern-audit spine. |
| **Finding in report** | Not present — the check never ran. | Present in the report as accepted, annotated with the suppression decision anchor and expiry. |
| **Expiry behaviour** | N/A — no record to expire. | After `--until DATE` the finding reverts to its normal verdict on the next audit run. The suppression record is read at report-generation time to determine whether to annotate a finding as accepted. |

**They produce different governance records.** `skip` produces no finding and no
decision artefact. `suppress` produces a chain-anchored `GovernanceDecision`
that binds the finding ID, the operator's reason, and the expiry date — so a
future verifier can recompute whether the finding was accepted at the time of
the audit, and whether the acceptance window had lapsed.

**Suppressed findings appear in the report as accepted.** The report generator
consults the suppression records when building the finding list. A finding
with a live (unexpired) suppression is emitted with the suppression decision's
journal anchor and the expiry date, so the record is self-describing.

**Past `--until` the finding reverts to its normal verdict.** There is no
active enforcement in `suppress` itself; downstream consumers — the audit report
generator, the posture scorer — consult the suppression record's `expiry` field
and treat the finding as accepted only while the current date is within the
window.

For `--skip` (check exclusion), see the audit check contract defined in
issue #5072.
