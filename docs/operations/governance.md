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

## Reading recorded decisions

In the web UI, open **Governance**, enter a **Run ID** under **Recorded
decisions**, and choose **Load decisions**. Expand a row to see subject,
action, recorded verdict, input hash, timestamp, recorded context, policy
version and `journal_entry_hash`. When no policy version is named in the
record's context, the field says exactly `this decision names no policy version`.
The record is meaningless without its journal anchor. Displaying an anchor
does not verify it; these reads do not verify the journal or signer, and the
browser does not recompute the verdict. The separate coverage panel remains
an explicitly labelled checked-in sample.

The authenticated server exposes two additive read endpoints, also mounted
under `/api/v1`:

- `GET /governance/decisions?run_id=<id>` returns `run_id` and `decisions`.
- `GET /governance/decisions/<decision_id>?run_id=<id>` returns one decision,
  or 404 when that exact record is absent. `decision_id` is an opaque digest
  of the presented record, including its anchor; editing the record changes it.

Both return the recorded fields listed above. The list response schema is
[`recorded-decisions.schema.json`](../api/recorded-decisions.schema.json);
its `RecordedDecision` definition is also the detail response. Empty runs
return an empty list. Missing or invalid run IDs return 400. Unreadable records
or records naming a different run return 409. The existing record reader
skips malformed JSON records. These endpoints create no keys, files or
directories and accept no client-selected ledger path.

Budget rows additionally carry `budget.cap_usd` and `budget.prior_spend_usd`.
Each has `value` (USD or null), `state`, `provenance` and `reason`. The cap
comes from recorded `context.cap_usd`. Prior spend comes from the project's
`.sdd/cost/ledger.jsonl` through `seat_spend`, checked against the recorded
input hash and verdict by the existing budget verifier. Both values share
the resulting budget-input evidence state:

| State | Meaning |
|---|---|
| `verified` | The existing budget verifier reproduced the recorded input hash and verdict using the presented ledger. Prior spend is shown. This is budget input consistency only, not journal, signer or ledger authenticity verification. |
| `unavailable` | Required policy context or ledger evidence is absent, unreadable or changed during the read. Prior spend is null; a missing ledger never implies zero spend. |
| `mismatch` | The presented ledger and context do not reproduce the recorded inputs or verdict. Prior spend is null so a current total cannot be mistaken for decision-time spend. |

The recorded cap remains visible when available, including on a mismatch.
No historical ledger snapshot is stored in a decision, so later ledger
appends may produce a mismatch. These reads use the existing verifier's
ledger interpretation; they do not certify ledger completeness. To verify
the anchored record, use the separate offline governance verification workflow.

Regenerate the public response schema after changing its models:

```sh
uv run --locked python - <<'PY'
import json
from pathlib import Path
from bernstein.core.routes.governance import RecordedDecisions
Path('docs/api/recorded-decisions.schema.json').write_text(
    json.dumps(RecordedDecisions.model_json_schema(), indent=2) + '\n'
)
PY
```

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

In the web UI, open **Governance** and choose or drop a file under
**Verify a receipt**. Confirm the selected filename and select **Verify
receipt** to send the original bytes to the server. A successful check shows
the integrity-only tier and its trust caveat beside the verdict.

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
the key embedded in the receipt. The verifier checks the canonical signed
contents. JSON formatting such as trailing whitespace can change without
affecting the result. To check who signed the receipt, obtain the operator's
public key separately and use `bernstein verify receipt <file> --public-key
<pem>`. The endpoint uses only the embedded key.

A receipt that does not verify is answered with `200` and a failing verdict.
The result is a statement about the evidence, not a failed request.

## Guarantees

- **Verifiability** - `bernstein govern verify <run>` re-resolves and
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
bernstein govern verify <run> --bindings bindings.json [--ledger ledger.jsonl]
```

Exit codes: `0` verified, `1` no records / bad input, `2` mismatch.

The `--bindings` file is a signed `RoleBindings` JSON (`RoleBindings.to_dict()`).
`--ledger` is required when the run carries budget decisions. Each recorded
decision is also mirrored into the HMAC audit chain as a `governance.decision`
event, so an operator can confirm from the chain alone that a decision bound the
claimed inputs to a named spine entry.

## `govern audit-compliance`: what this install can and cannot show

The main audit runner exposes the same 23 policy-library observations:

```sh
bernstein govern audit --only CMP --json
bernstein govern audit --only CMP --profile soc2 --json
bernstein govern audit --list --only CMP
```

CMP checks are opt-in, like installation checks, so adding this catalogue does
not change the default workspace audit gate. `--profile` defaults to selecting
CMP when no `--only` is given and marks required IDs with `required` in JSON or
an asterisk in text. Supported profiles are `soc2`, `iso27001`, `pci_dss`,
`nist_800_53`, `hipaa` and `gdpr`, using the existing library mappings; selection
does not assert conformance. Unknown profiles are rejected before execution.
The older `audit-compliance` command remains available.

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

## `govern audit`: machine-readable output and CI gates

```bash
bernstein govern audit --format json
bernstein govern audit --format sarif --strict
bernstein govern audit --quiet --strict
```

`--format text` is the default. JSON is an array with one object per finding,
including every `Finding` field and its `id` alias: `check_id`, `verdict`,
`evidence` (locator and SHA-256 pairs), `what_would_make_it_measurable`, `reason`,
`message`, `summary`, `remediation`, `area`, and `passed`.
The existing `--json` and `--json-output` flags remain aliases for JSON output
and may be combined with `--format json` or the default text format.
Combining either alias with `--format sarif`, or using `--list --format sarif`,
is a usage error. `doctor --json` uses the
same complete finding objects in its `findings` array.

SARIF 2.1.0 includes one rule per check ID and one result per finding, with
`ruleId = finding.id`, a matching `ruleIndex`, and finding fields in
`properties`. Evidence retains its SHA-256 hashes but omits source locators,
and absolute local paths in diagnostic text are redacted. Each result has a
physical location: a file reported by the check's evidence within the workspace,
or the workspace's root `bernstein.yaml` / `bernstein.yml`
configuration for workspace-level evidence. When configuration is missing,
the location identifies the expected `bernstein.yaml`. Repository paths are
relative, URI-encoded paths; reported files outside the workspace fall back to
the root configuration. For code-scanning uploads, run the command from the
repository root and keep `--workdir` at that root so relative paths resolve
against the checkout correctly. SARIF never includes absolute local paths or
file URIs.
Passing results have `kind: pass` and `level: none`, so they are not code-scanning
findings. Failures have `kind: fail` and `level: error`; unmeasurable results
have `kind: review` and `level: none`, with the unmeasurable reason in the
message; declarations are informational with
`level: none`. `properties.kind` carries `measured`, `declared`, or
`not_measurable`; legacy `pass`/`fail` verdicts map to `measured` there, while
the original verdict and `passed` remain available. An offline JSON Schema
profile tests the emitted SARIF 2.1.0 subset, including rules and locations;
validation never fetches the `$schema` URL.

All checks selected by `--only` and `--skip` are required for this gate.

| Exit | Condition |
|---|---|
| `0` | At least one check ran, all selected measured checks passed, and none was unmeasurable; or a valid `--list` invocation completed. |
| `1` | A selected check failed or crashed, a measured result had no explicit pass, no checks matched, or a check was unmeasurable without `--strict`. Unexpected tool exceptions also exit `1`. |
| `2` | With `--strict`, a selected check is `not_measurable`, and no check failed or crashed. Click usage errors also exit `2`, such as an unknown format, nonexistent workdir, or incompatible output flags. |

Declared findings never affect the exit code. Failures and crashes take
precedence over strict incompleteness. As on `main`, unmeasurable checks fail
the default gate; `--strict` distinguishes missing prerequisites with code `2`
without reclassifying check crashes. The registry still reports a crashed
check as `not_measurable` and continues the remaining checks.

`--quiet` suppresses the report and registry crash warnings in every format,
including empty selections and `--list`. It preserves sentinel journaling and
notification dispatch. CLI usage diagnostics still go to stderr. `--list` does
not execute checks and exits `0`.

## `govern audit --only duplication`: duplicate implementations in this install

```
bernstein govern audit --only duplication [--json]
```

These checks count how many implementations of one capability the installed
`bernstein` package carries, and compare each count with the number there should
be (#5105). They read the package's own source, not `--workdir`, because no
project tree can change the answer. For the same reason they are **opt-in**: a
default `govern audit` does not run them, so a clean workspace stays green.
`--list` shows them with the others.

| Check | Expected | Today |
|---|---|---|
| `duplication:inline-canonical-bytes-sites` | 1 | measured: places that build canonical signing bytes inline instead of through one helper (#5094) |
| `duplication:private-hmac-key-loaders` | 0 | measured: private `_load_hmac_key` wrappers |
| `duplication:receipt-verify-kinds` | 1 | `not_measurable` until #5096 |
| `duplication:verification-result-shapes` | 1 | `not_measurable` until #5099 |
| `duplication:registry-duplicate-ids` | 0 | `not_measurable` until #5104 |
| `duplication:orphan-modules` | 0 | `not_measurable` until #5100 |

A count over the expected one is `fail`, with the count and the first offending
`path:line` locations in the finding. A count within it is `pass`. A check whose
subject does not exist yet is `not_measurable`, with the issue that would make it
measurable, and is never reported as a pass. The findings carry no score or
grade. Two runs over one install produce byte-identical `--json` output.

## Audit failure sentinel

Use the single canonical sentinel to exercise a harmless, labelled failure:

```bash
BERNSTEIN_GOVERN_AUDIT_SENTINEL=1 bernstein govern audit --workdir . --json
```

Only the exact value `1` activates it. The named check `sentinel:failure`
reports `verdict: measured`, `passed: false`, evidence of the environment
switch, and the fixed reason `Injected governance audit failure (selftest sentinel).`
Other checks still run normally. The command exits `1` for the injected failure.
The check is listed by `--list`, but is omitted from default runs when inactive.
`--only sentinel` can inspect its inactive, passing state explicitly.

An active sentinel prints its name and check ID to stderr unless `--quiet` is
set, including when `--only` or `--skip` excludes the check; JSON on stdout
remains parseable. Quiet mode still records and notifies an executed sentinel.
When the failure is raised, the run is persisted as one chain-anchored
`AuditReport` under `.sdd/lineage/govern-audit/reports/` before one notification
event is dispatched. The existing notification bridge handles delivery, retries
and sink filtering: enable `notifications` in `bernstein.yaml` and subscribe a
sink to `synthetic` events with severity `error` (or leave those filters unset).
The event includes the sentinel name, fixed reason, check ID and journal anchor.
Repeated active runs within one second may produce identical report bytes;
each run has its own journal anchor, verifies against that anchor, and dispatches
its own notification event.
Without configured, enabled sinks, the run is still recorded but no delivery is
claimed. Excluding the check produces neither its journal entry nor its event.
Ordinary audits' recording and notification behaviour is unchanged.

An environment variable is canonical because it can be scoped to one command
and leaves no sentinel file behind. If exported in a shell or CI environment,
clear it with `unset BERNSTEIN_GOVERN_AUDIT_SENTINEL`. The selftest in
`tests/unit/cli/commands/test_govern_audit_sentinel.py` sets and clears it,
verifies the real journal and a local recording notification sink for one or
two active runs with identical report bytes, then runs again with the sentinel
cleared to confirm there is no new injection, journal entry or notification.

## Declared discovery probes

Governance collectors can load probe declarations from a directory through
`src/bernstein/core/govern/probe.py`. A declaration names the fact rather than
embedding collection policy in a dispatch branch:

```json
{
  "id": "adapter-version",
  "attribute": "adapter.version",
  "collection_method": "command",
  "fallback_methods": ["api"],
  "max_retries": 1,
  "refresh_interval_s": 900,
  "timeout_s": 5,
  "cost_class": "cheap",
  "taint_tags": ["host-derived"]
}
```

`collection_method` is always attempted first. `fallback_methods` are optional
and run in declaration order; `max_retries` is the number of additional tries
allowed for each available source. Fields an older build does not understand
are preserved when a declaration is loaded and serialized again.

An answered fact may be held in the in-memory `ProbeFactCache` until its
`refresh_interval_s` expires; a value of `0` means collect on every run. This
cache is deliberately not durable, so a process restart collects again. An
uncached target/probe start receives bounded full jitter, and `timeout_s` is a
deadline for the entire invocation including retries and fallbacks. Exceptions,
missing collectors, and no-answer attempts are recorded as failure evidence
while declared retries and fallbacks continue within that deadline. The final
result is explicit `unknown` only when the overall deadline expires or every
declared source is exhausted without an answer; remaining probes and targets
still continue. The deadline stops the coordinator waiting; it does not cancel
the daemon worker, which may retain resources until the collector finishes, and
any result arriving after the timeout is ignored.

Each completed probe-set run carries one canonical journal record containing the
probe-set version, attempted targets in input order, targets that returned at
least one value, and normalized per-attempt failures. `record_probe_run()`
anchors that record with one `LineageSpine.record()` call for the whole run, not
one call per target. Failure records contain only the source, attempt, failure
kind, and a bounded reason category; collector output and tracebacks are not
journaled.

The current `bernstein govern discover` inventory command still uses its
existing inventory collector. Wiring declared probe values into observation
envelopes is a separate collector concern, so this runtime does not claim that
the CLI executed a probe set when it did not.

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

### Repeating and resuming a reviewed reconcile diff

The library helper `bernstein.core.govern.reconcile.apply_reconcile_diff`
consumes a `ReconcileDiff`, a `ChangeReceipt` context template, and trusted
callbacks for whole-diff authorization, fresh observation and applying one
entry. It has no built-in environment mutator and does not change the
read-only `govern reconcile --propose` CLI. The authorizer must approve the
entire diff under the template's effective policy digest before any callback
can mutate state, including any separate approval required for removals.

Each attempt uses the existing kind-qualified entity/desired-value key.
Policy changes do not rewrite that intent identity: authorization is a
separate input and previous receipts must match the exact plan, policy,
environment and approver. The template's plan digest is the canonical digest
of `diff.to_dict()`; its timestamp is supplied by the caller.

A matching fresh value produces a `skipped` attempt without calling the
mutator. An unread entity is unavailable; only explicitly confirmed absence
can satisfy a removal. Fresh unsatisfied drift that differs from the reviewed
observation fails rather than overwriting a changed target. The first failure
stops the loop, so never-attempted entries have no attempt in that receipt.

For partial resume, supply the trusted in-process prior `ChangeReceipt`.
Prior successful or satisfied entries are recorded as skips; failed and
never-attempted entries are reconsidered. Loading an external receipt requires
shared-protocol envelope verification and independent signer trust before
passing its data to this helper. A new diff is required for later drift or a
changed policy context. Existing `govern apply` admission and receipt
verification remain separate; an idempotency key is never approval.

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

CMP execution errors use the shared audit error handling: the failed control
remains in the report, remaining controls still run, and `--strict` returns 1
for the crash. Exit 2 is reserved for missing prerequisites without a failure
or crash.
