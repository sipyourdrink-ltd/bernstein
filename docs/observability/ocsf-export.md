# OCSF export — mapping table

Status: **proposed, for review.** This is the mapping doc #6037 asks for before
any encoder is written. Nothing here is implemented yet.

Pinned schema: **OCSF 1.9.0** (released 2026-08-03, current latest). Every
`class_uid` and enum value below was read out of `ocsf/ocsf-schema` at tag
`1.9.0`, not from memory.

---

## A scope question that has to be settled first

#6037 is titled *"a run's security events reach no SIEM; export the journal as
OCSF events"*, and names three examples: **a denied tool call, a budget refusal
or a containment stop**. None of the three is in the run journal.

| What the issue names | Where it actually is |
|---|---|
| a denied tool call | the **HMAC audit chain**, as `auto_approve_decision` — `core/approval/gate.py:328` writes it through `AuditLog.log(...)` into `.sdd/audit`, never into `.sdd/runs/<id>/journal.jsonl` |
| a budget refusal | **nowhere yet.** #2918 (*"Budget envelopes with refusal receipts"*) is still open. `budget_exhausted` exists only as a display string in `cli/commands/activity_cmd.py:726` |
| a containment stop | **no such event kind.** Every `contain*` identifier in `src/` is `container_*` Docker plumbing or `contained_path` path-containment |

What the journal *does* carry is orchestration and lifecycle. The committed
demo run (`docs/assets/demo-run/run-receipt.json`) contains exactly eleven
kinds:

```
run_started  tick_start  provider_state_capability  agent_spawned
skills.injected  task_claimed  task_completed  task_retried
plan.graph  plan.graph.full  run_completed
```

So a journal-only export produces a run timeline, not security telemetry. The
security decisions an operator wants beside their other security events live in
the audit chain, which already has the right vocabulary for it — 28 event types
including `auto_approve_decision`, `human_approval_decision`, `approval_pending`,
`approval_resolved`, `admission_refusal`, `capability_matrix_refusal`,
`lineage_tamper_detected` and `always_allow_promotion`.

**Three ways to go, and this needs your call:**

- **(A) Journal only, renamed expectations.** Honest and small, but it does not
  deliver the issue's own headline — a denied tool call would not appear.
- **(B) Journal + audit chain.** Delivers the stated goal. Costs: two sources,
  and the issue's requirement 4 ("every event carries the journal entry hash it
  was projected from") needs widening to "the source record's hash", since an
  audit event has a chain position and not a journal entry hash.
- **(C) Audit chain only.** The tightest fit for "security events", and it
  leaves the run timeline to the existing OTel projection
  (`core/observability/otel_projection.py`), which already maps ten journal
  kinds to GenAI operations and is arguably the right home for lifecycle.

**I recommend (B)**, with the correlation requirement restated as *source
record identity* — `{store, record_hash}` — rather than specifically a journal
entry hash. The tables below are written for (B) and marked by source, so (A)
or (C) is a matter of deleting a block, not rewriting the doc.

Two follow-on consequences of (B), flagged rather than assumed:

1. "Same journal, same bytes out" becomes "same journal **and audit range**,
   same bytes out". The audit chain stamps wall-clock times, so determinism has
   to be scoped to a fixed input range the way `audit_receipt.py` already scopes
   its projections — not to "the run", which keeps growing.
2. The export needs the audit HMAC key to read the chain, where a journal-only
   export needs nothing. That changes the command's trust requirements and
   should be explicit in `--help`.

---

## How a decision is distinguished from an outcome

Requirement 2 says an enforcement decision and the action's outcome are two
facts, and a decision with no recorded outcome must export as unknown rather
than as success. OCSF already has exactly this split, so nothing is invented:

| Fact | OCSF field | Source | Values used |
|---|---|---|---|
| the **decision** | `action_id` (Security Control profile) | the gate's verdict | `1` Allowed, `2` Denied, `0` Unknown |
| the **outcome** | `status_id` (base event) | whether the call then ran | `1` Success, `2` Failure, `0` Unknown |
| what drove the decision | `policy` (Security Control profile) | `matched_pattern`, profile name | — |
| who decided | `authorizations` (Security Control profile) | the recorded principal | — |

So a denied tool call is `action_id = 2` with `status_id = 0`, and **never**
`status_id = 1`. An allowed call whose effect was never recorded is
`action_id = 1`, `status_id = 0`. This is the rule requirement 2 asks for, and
it falls out of the profile rather than needing a custom object.

Applying the `security_control` profile is therefore load-bearing, not
decoration: `action_id` and `disposition_id` are **not** on `base_event`, they
arrive only with that profile.

### Absence: determined-absent vs never-established

Requirement 3 wants these distinguished. `status_id = 0` ("Unknown") covers
*never established*. For *determined to be absent* — the journal looked and
recorded nothing there — OCSF has no standard enum, so this uses
`status_detail` with two fixed strings and does not overload `status_id`:

| Case | `status_id` | `status_detail` |
|---|---|---|
| the record establishes the outcome | `1` / `2` | — |
| the record determined the thing was absent | `0` | `bernstein:absent-determined` |
| the record never established it | `0` | `bernstein:absent-unestablished` |

Both land under `status_id = 0`, so a SIEM that only reads the enum is never
misled; a consumer that wants the distinction reads one string.

---

## Table 1 — journal kinds

Category `6` Application Activity unless stated. `class_uid = category_uid *
1000 + class uid`.

| Journal kind | OCSF class | `class_uid` | `activity_id` | Notes |
|---|---|---|---|---|
| `run_started` | Application Lifecycle | 6002 | `3` Start | the run is the application instance |
| `run_completed` | Application Lifecycle | 6002 | `4` Stop | `status_id` from the recorded result |
| `run_stalled` | Application Lifecycle | 6002 | `4` Stop | `status_id = 2` Failure |
| `run_quiescence` | Application Lifecycle | 6002 | `0` Unknown | lifecycle has no "idle"; a gap, see below |
| `agent_spawned` | Application Lifecycle | 6002 | `3` Start | one event per agent, parented to the run |
| `agent_reaped` | Application Lifecycle | 6002 | `4` Stop | |
| `task_claimed` | API Activity | 6003 | `1` Create | a unit of work requested |
| `task_completed` | API Activity | 6003 | `3` Update | `status_id = 1` |
| `task_retried` | API Activity | 6003 | `3` Update | `status_id = 2`, `count` carries the attempt |
| `task_verification_failed` | API Activity | 6003 | `3` Update | `status_id = 2` |
| `workflow_approval_granted` | Authorize Session | 3003 (IAM) | `1` Assign Privileges | `action_id = 1` Allowed |
| `workflow_phase_advanced` | Entity Management | 3004 (IAM) | `3` Update | |
| `skills.injected` | Entity Management | 3004 (IAM) | `1` Create | the loaded extension set |
| `plan.graph`, `plan.graph.full` | — | — | — | **not exported**, see gaps |
| `tick_start` | — | — | — | **not exported**, see gaps |
| `provider_state_capability` | — | — | — | **not exported**, see gaps |

## Table 2 — audit-chain kinds (option B only)

These are the security events. All carry the `security_control` profile.

| Audit `event_type` | OCSF class | `class_uid` | `activity_id` | Decision |
|---|---|---|---|---|
| `auto_approve_decision` | API Activity | 6003 | from the tool verb | `action_id` from the verdict: APPROVE→`1`, DENY→`2`, ASK→`0` |
| `human_approval_decision` | Authorize Session | 3003 | `1` Assign Privileges | `1` / `2` from the resolution |
| `approval_pending` | API Activity | 6003 | `0` Unknown | `action_id = 0`, `status_id = 0` — the gate is waiting; this is the TTL-deny precursor |
| `approval_resolved` | Authorize Session | 3003 | `1` Assign Privileges | `1` / `2` |
| `admission_refusal` | API Activity | 6003 | `0` Unknown | `action_id = 2` Denied |
| `capability_matrix_refusal` | API Activity | 6003 | `0` Unknown | `action_id = 2` Denied |
| `always_allow_promotion` | Entity Management | 3004 | `3` Update | `action_id = 4` Modified — a policy change, not a call |
| `lineage_tamper_detected` | Detection Finding | 2004 (Findings) | `1` Create | `is_alert = true` |

`approval_pending` with no matching `approval_resolved` is the canonical
"decision with no outcome" case, and exports as `action_id = 0`,
`status_id = 0`, `status_detail = bernstein:absent-unestablished`. It must not
become an Allowed.

---

## Correlation

Every event carries, under `metadata`:

| Field | Content |
|---|---|
| `metadata.correlation_uid` | the run id |
| `metadata.product.name` / `.vendor_name` | `bernstein` |
| `metadata.version` | `1.9.0` — the pinned OCSF version |
| `metadata.logged_time` | omitted; no wall clock enters the bytes |
| `unmapped.source` | `{"store": "journal"\|"audit", "record_hash": "<hex>", "index": <n>}` |
| `unmapped.delegation` | the delegation lineage, when the record carries one |

`unmapped.source.record_hash` is the journal `event_hash` or the audit entry
hash, so an event can be walked back to the record it came from and that record
verified against the chain head. This is requirement 4, with the widening noted
above: the key is `{store, record_hash}`, not a journal hash alone.

`raw_data` is **never** populated. `raw_data_hash` carries the digest instead —
see below.

---

## Request parameters are digests

Requirement: *"Request parameters are exported as digests, with the
canonicalisation named in the event."*

Tool arguments routinely contain file contents, prompts and shell commands, so
nothing is exported verbatim. For each record:

```
raw_data_hash = { "algorithm_id": 3,            # SHA-256
                  "value": "<hex of sha256(canonicalize_jcs(params))>" }
unmapped.canonicalization = "jcs-rfc8785"
```

`canonicalize_jcs` is the function `core/security/agent_card_signer.py` already
exposes and `openlineage_export.py` already uses, so the digest is reproducible
by anything that can do RFC 8785. Naming the canonicalisation in the event is
what makes the digest checkable rather than decorative.

One exception worth your opinion: `auto_approve_decision` already stores a
truncated `command` string in its details, in the clear, in the audit chain. I
propose to digest it like everything else rather than forward it, even though it
is already recorded — forwarding to a SIEM is a wider disclosure than keeping it
in `.sdd/audit`.

---

## Gaps — listed, not dropped

Per requirement 1, fields and kinds with no OCSF home are recorded here rather
than silently discarded.

| Thing | Why it is not mapped |
|---|---|
| **budget refusal** | no source. #2918 is open; there is no event to project. Mapping it now would mean inventing the producer, which is a different issue. |
| **containment stop** | no source. No containment event kind exists in `src/`. |
| `tick_start` | scheduler heartbeat, one per tick (13 in an 11-event demo run). Exporting it would dominate the stream with no security content. Available behind `--include-ticks` if you want it. |
| `plan.graph`, `plan.graph.full` | a task DAG, not an event. OCSF has no graph class; it belongs in the OTel projection or a dataset facet. |
| `provider_state_capability` | capability advertisement. Closest is Discovery (5), but the record is a config snapshot rather than an observation of an entity. |
| `run_quiescence` | Application Lifecycle has no "went idle" activity. Exported as `activity_id = 0` rather than invented. |
| journal kind vocabulary | **there is no central registry of journal event kinds.** `journal.py` defines no enum; `record()` takes an arbitrary string, and `otel_projection.py`'s `EVENT_TO_OPERATION` covers ten kinds while the demo run already shows eleven, two of which are absent from it. So any mapping table is a snapshot, and an unmapped kind must be a *recorded* omission, not a crash or a drop. Proposal: unmapped kinds export as `class_uid = 6003`, `activity_id = 0`, `unmapped.unmapped_kind = "<name>"`, and the CLI prints a count of them. Worth considering a registry as its own issue. |

---

## The proposed extension (#1760), behind a flag

`ocsf/ocsf-schema#1760` — *"`accounting_decision` object + normalized
containment reason"* — is an **open issue**, not a merged PR, filed 2026-09-17.
So nothing it proposes is in 1.9.0.

Per requirement 5 it stays behind `--include-proposed`, lives in one module so
it can be dropped or renamed in one edit, and every event it touches is marked:

```
unmapped.proposed = { "schema_proposal": "ocsf/ocsf-schema#1760",
                      "status": "open",
                      "fields": ["accounting_decision", "containment_reason"] }
```

Off by default. With the flag off, output validates against stock 1.9.0 — which
is the property CI should assert, since that is what a real SIEM will ingest.

Note that both things #1760 would carry (accounting decisions, containment
reasons) are also the two **gaps with no source** above. So the flag is
currently a shape with nothing to put in it, and the honest first slice may be
to implement the flag's plumbing and leave it unexercised until #2918 lands.

---

## What the first slice would be

Once the scope above is settled:

1. `docs/observability/ocsf-export.md` — this table, as agreed.
2. A vendored `1.9.0` schema subset plus a test-only validator
   (`jsonschema`, dev dependency — requirement: no new runtime dependency).
3. `core/observability/ocsf_projection.py`, built in the shape of
   `openlineage_export.py`: `build_ocsf_events()` → `render_ocsf_jsonl()` →
   `export_ocsf()`, pure functions over a loaded range.
4. `bernstein export ocsf --run <id>`. Note there is **no top-level `export`
   group** today — `trace export` and `lineage export` are subcommands of their
   own groups. Either a new group, or `trace export --format ocsf` beside the
   existing OpenLineage `--format`. The latter matches what is there; say which
   you prefer.
5. Tests: a fixture with an allow, a deny, and a decision with no outcome;
   byte-identical output across two runs; every event valid against the pinned
   schema; the no-outcome case asserted as `status_id = 0`.
