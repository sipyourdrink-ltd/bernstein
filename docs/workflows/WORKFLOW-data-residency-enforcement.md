# WORKFLOW: Data Residency Enforcement (ENT-009)
**Version**: 1.0
**Date**: 2026-04-08
**Author**: Workflow Architect
**Status**: Draft
**Implements**: ENT-009 — Add data residency controls for compliance

---

## Overview

Ensures that all API calls to model providers route through compliant geographic regions, and that task data (state files, agent outputs, audit logs) remains within tenant-configured residency boundaries.  Bridges the gap between the existing `DataResidencyController` (policy storage + write validation) and the `ModelPolicy`/`PolicyFilter` in the router (provider-level region filtering) by adding an enforcement layer that validates actual data flow at runtime.

---

## Actors

| Actor | Role in this workflow |
|---|---|
| Operator / Tenant admin | Configures residency policies via CLI or seed config |
| Orchestrator | Initializes `DataResidencyController`, passes policy to router |
| Router (`ProviderRouter`) | Filters providers by region, attaches `ResidencyAttestation` |
| Task server (routes) | Validates region on task creation, progress, and completion |
| `DataResidencyController` | Stores policies, validates writes, creates attestation records |
| Audit log | Records residency attestations and violations |
| Compliance reporter | Aggregates attestations for evidence export |

---

## Prerequisites

- Tenant ID is resolved for every request (via `request_tenant_id()` from `tenanting.py`)
- Each `ProviderConfig` has a truthful `region` field reflecting where API calls are routed
- `.sdd/config/` directory exists for policy persistence
- Compliance config is loaded (at minimum `data_residency: true` + `data_residency_region`)

---

## Trigger

Any of:
1. **Orchestrator startup** — loads persisted residency policies, initializes controller
2. **Task creation** (`POST /tasks`) — validates target region before accepting
3. **Provider routing** (`ProviderRouter.select_provider_for_task`) — filters providers, creates attestation
4. **Task progress** (`POST /tasks/{id}/progress`) — validates files_changed regions
5. **Metrics export** — serializes attestation records

---

## Workflow Tree

### STEP 1: Policy Initialization (on orchestrator startup)

**Actor**: Orchestrator
**Action**: Load persisted residency policies from `.sdd/config/residency_policies.json`, create `DataResidencyController`, set its `node_region` from environment or config, register policies for all configured tenants.
**Timeout**: 5s
**Input**: `{ sdd_path: Path, compliance_config: ComplianceConfig, seed_config: SeedConfig }`
**Output on SUCCESS**: `{ controller: DataResidencyController, policies_loaded: int }` -> GO TO STEP 2 (on first task)
**Output on FAILURE**:
  - `FAILURE(file_not_found)`: No persisted policies file -> Recovery: log warning, start with empty policies (lenient boot), continue
  - `FAILURE(parse_error)`: Malformed JSON -> Recovery: log error, start with empty policies, emit audit event `residency.policy_load_failed`
  - `FAILURE(invalid_policy)`: Policy references unknown region -> Recovery: skip invalid policy, log, continue with valid policies

**Observable states during this step**:
  - Customer sees: nothing (startup is transparent)
  - Operator sees: log line `"Loaded N residency policies"` or warning `"No residency policies found"`
  - Database: `.sdd/config/residency_policies.json` read (not modified)
  - Logs: `[orchestrator] Residency controller initialized, node_region=us-east, policies=3`

---

### STEP 2: Policy Enforcement on Provider Routing

**Actor**: Router (`ProviderRouter.select_provider_for_task`)
**Action**: Before selecting a provider, apply `PolicyFilter.filter_providers()` which calls `ModelPolicy.is_provider_allowed()` with each provider's `region`. The `required_region` from the active policy restricts to matching providers. Build `ResidencyAttestation` on the routing decision.
**Timeout**: 1s (routing is synchronous, CPU-bound)
**Input**: `{ task: Task, providers: list[ProviderConfig], model_policy: ModelPolicy }`
**Output on SUCCESS**: `{ decision: RoutingDecision, attestation: ResidencyAttestation }` -> GO TO STEP 3
**Output on FAILURE**:
  - `FAILURE(no_compliant_provider)`: All providers filtered out by region constraint -> **BRANCH A**
  - `FAILURE(region_mismatch_with_fallback)`: No in-region provider, but `allow_cross_region_fallback=True` -> **BRANCH B**

**Observable states during this step**:
  - Customer sees: nothing (internal routing)
  - Operator sees: routing decision log with attestation
  - Database: `RoutingDecision.residency_attestation` populated on task record
  - Logs: `[router] Task T123 routed to provider=anthropic region=eu-west compliant=True`

#### BRANCH A: No compliant provider available (strict mode)

**Triggered by**: `required_region` set, `allow_cross_region_fallback=False`, no provider in allowed region
**Actions**:
  1. Log `residency.no_compliant_provider` with task_id, required_region, available_regions
  2. Emit audit event `residency.routing_blocked`
  3. Return task to backlog with status `blocked` and reason `"No provider available in required region {region}"`
  4. Notify operator via bulletin: `"Task T123 blocked: no provider in {region}"`
**What customer sees**: Task stays in pending/blocked state
**What operator sees**: Bulletin + audit log entry with specific region constraint failure

#### BRANCH B: Cross-region fallback (degraded mode)

**Triggered by**: `required_region` set, `allow_cross_region_fallback=True`, no in-region provider
**Actions**:
  1. Select best available out-of-region provider
  2. Create `ResidencyAttestation` with `compliant=False`
  3. Log `residency.cross_region_fallback` with task_id, required_region, actual_region
  4. Emit audit event `residency.fallback_used`
  5. Continue routing with degraded attestation
**What customer sees**: Task proceeds (possibly with latency difference)
**What operator sees**: Warning log + non-compliant attestation in metrics export

---

### STEP 3: Residency Validation on Task Data Writes

**Actor**: Task server routes (`POST /tasks`, `POST /tasks/{id}/progress`, `POST /tasks/{id}/complete`)
**Action**: Before persisting task data (backlog YAML, progress snapshots, completion results), call `DataResidencyController.validate_write_or_raise(tenant_id, node_region)` to verify that the current node is in an allowed region for this tenant's data.
**Timeout**: <1ms (in-memory lookup)
**Input**: `{ tenant_id: str, node_region: str (from controller), data_type: str }`
**Output on SUCCESS**: `{ allowed: True }` -> proceed with write, GO TO STEP 4
**Output on FAILURE**:
  - `FAILURE(residency_violation)` strict mode: `ResidencyViolation` raised -> Return HTTP 403 with `{ "error": "Data residency violation", "code": "RESIDENCY_VIOLATION", "required_regions": [...], "actual_region": "..." }`
  - `FAILURE(residency_warning)` lenient mode: Log warning, proceed with write, create non-compliant attestation

**Observable states during this step**:
  - Customer sees: HTTP 403 if strict violation; transparent otherwise
  - Operator sees: Violation/warning in logs
  - Database: Write blocked (strict) or write proceeds with non-compliant attestation (lenient)
  - Logs: `[task_server] Residency violation: tenant=acme region=us-east not in allowed=[eu-west, eu-central]`

---

### STEP 4: Attestation Recording

**Actor**: `DataResidencyController`
**Action**: After every validated write (pass or fail), create a `ResidencyAttestation` record. Persist attestations to `.sdd/audit/residency_attestations.jsonl` (append-only).
**Timeout**: 50ms (file append)
**Input**: `{ tenant_id: str, resource_id: str, resource_type: str, region: str }`
**Output on SUCCESS**: `{ attestation: ResidencyAttestation, persisted: True }` -> END (per-request)
**Output on FAILURE**:
  - `FAILURE(disk_full)`: Cannot write attestation file -> Log error, continue (attestation loss is not data loss — it is an audit gap). Emit bulletin `"Attestation write failed — audit gap"`
  - `FAILURE(io_error)`: Permission denied or path issue -> Same recovery as disk_full

**Observable states during this step**:
  - Customer sees: nothing
  - Operator sees: attestation count in metrics export
  - Database: `.sdd/audit/residency_attestations.jsonl` — one JSON line per attestation
  - Logs: `[residency] Attestation created: tenant=acme resource=task-T123 region=eu-west compliant=True`

---

### STEP 5: Policy Persistence (on policy change)

**Actor**: `DataResidencyController` (triggered by `set_policy()`)
**Action**: After setting or updating a policy in memory, persist the full policy set to `.sdd/config/residency_policies.json` (atomic write via temp file + rename).
**Timeout**: 100ms
**Input**: `{ policies: dict[str, ResidencyPolicy] }`
**Output on SUCCESS**: `{ path: str, policies_count: int }` -> END
**Output on FAILURE**:
  - `FAILURE(io_error)`: Cannot write config file -> Log error, policy remains in-memory only. Emit audit event `residency.policy_persist_failed`. On restart, policy will be lost.

**Observable states during this step**:
  - Operator sees: Updated policies file on disk
  - Logs: `[residency] Persisted N policies to .sdd/config/residency_policies.json`

---

### STEP 6: Compliance Evidence Export

**Actor**: Metrics exporter / compliance reporter
**Action**: On evidence bundle export or metrics export, serialize all `ResidencyAttestation` records via `_serialize_residency_attestations()`. Include non-compliant count, compliant count, and per-tenant breakdown.
**Timeout**: 10s (depends on attestation volume)
**Input**: `{ attestations: list[ResidencyAttestation], tenant_filter: str | None }`
**Output on SUCCESS**: `{ export: dict with attestation summaries }` -> END

**Observable states during this step**:
  - Operator sees: Residency section in exported evidence bundle
  - Logs: `[metrics] Exported N residency attestations (M non-compliant)`

---

## State Transitions

```
[no_policy]  -> (set_policy)                   -> [policy_active]
[policy_active] -> (write to allowed region)   -> [compliant_write] -> attestation(compliant=True)
[policy_active] -> (write to disallowed, strict) -> [violation_blocked] -> attestation(compliant=False) + HTTP 403
[policy_active] -> (write to disallowed, lenient) -> [violation_warned] -> attestation(compliant=False) + log warning
[policy_active] -> (routing, no in-region provider, strict) -> [routing_blocked] -> task blocked
[policy_active] -> (routing, no in-region provider, fallback) -> [routing_degraded] -> attestation(compliant=False)
```

---

## Handoff Contracts

### Orchestrator -> DataResidencyController (initialization)

**Method**: Constructor + `set_policy()` calls
**Payload**:
```python
DataResidencyController(node_region="eu-west")
controller.set_policy(ResidencyPolicy(
    tenant_id="acme",
    allowed_regions=frozenset({"eu-west", "eu-central"}),
    primary_region="eu-west",
    enforce_strict=True,
    require_encryption_at_rest=False,
))
```
**Success**: Controller ready, policies loaded
**Failure**: Raises `ValueError` if region string is invalid

### DataResidencyController -> Router (policy bridging)

**Method**: `ComplianceConfig.data_residency_region` -> `ModelPolicy.required_region`
**Payload**: Region string (e.g. `"eu"`)
**Gap identified**: Currently the `ComplianceConfig` sets `data_residency_region` and the router reads `ModelPolicy.required_region`, but there is no code that bridges these two. The orchestrator must copy `compliance.data_residency_region` into the `ModelPolicy.required_region` when constructing the router state.

### Task Server -> DataResidencyController (write validation)

**Endpoint**: In-process method call `controller.validate_write_or_raise(tenant_id, node_region)`
**Payload**: `{ tenant_id: str, target_region: str }`
**Success**: No exception raised, write proceeds
**Failure**: `ResidencyViolation` exception -> caller returns HTTP 403

### Metrics Exporter -> DataResidencyController (attestation export)

**Method**: `controller.get_attestations(tenant_id)` + `_serialize_residency_attestations()`
**Payload**: Optional tenant filter
**Success**: List of attestation dicts
**Failure**: Empty list (no attestations recorded)

---

## Cleanup Inventory

| Resource | Created at step | Destroyed by | Destroy method |
|---|---|---|---|
| In-memory policies dict | Step 1 | Process exit | GC |
| In-memory attestations list | Step 4 | Process exit | GC |
| `.sdd/config/residency_policies.json` | Step 5 | Manual operator action | File delete |
| `.sdd/audit/residency_attestations.jsonl` | Step 4 | Audit retention policy | `AuditLog.archive()` |

No cleanup needed on failure — residency validation is read-only validation that gates writes. Failed validation = write never happens.

---

## Reality Checker Findings

| # | Finding | Severity | Spec section affected | Resolution |
|---|---|---|---|---|
| RC-1 | `DataResidencyController` is never instantiated by the orchestrator | Critical | Step 1 | Must add initialization in `Orchestrator.__init__()` when `compliance.data_residency` is True |
| RC-2 | `ComplianceConfig.data_residency_region` is never bridged to `ModelPolicy.required_region` | Critical | Step 2, Handoff: Controller->Router | Orchestrator must set `model_policy.required_region = compliance.data_residency_region` |
| RC-3 | Task server routes do not call any residency validation | Critical | Step 3 | `POST /tasks`, `/progress`, `/complete` must call `validate_write_or_raise()` |
| RC-4 | Policies are in-memory only — lost on restart | High | Step 5 | Need persistence to `.sdd/config/residency_policies.json` |
| RC-5 | Attestations are in-memory only — lost on restart | High | Step 4 | Need append-only persistence to `.sdd/audit/residency_attestations.jsonl` |
| RC-6 | `require_encryption_at_rest` on `ResidencyPolicy` is declared but never enforced | Medium | N/A (future workflow) | Out of scope for this workflow — flag for separate encryption-at-rest workflow |
| RC-7 | `_region_matches()` in router uses prefix matching (`eu` matches `eu-west`) which may be too loose | Low | Step 2 | Acceptable for now; document that `"eu"` matches any `eu-*` sub-region |
| RC-8 | No integration test verifying end-to-end residency enforcement through router -> task server -> attestation | High | Test Cases | Must add integration test |

---

## Test Cases

| Test | Trigger | Expected behavior |
|---|---|---|
| TC-01: Happy path — compliant routing | Task created, EU tenant, EU provider available | Provider selected, attestation compliant=True |
| TC-02: Strict block — no compliant provider | EU-only policy, only US providers registered | Task blocked, bulletin posted, audit event logged |
| TC-03: Cross-region fallback | EU-only policy with fallback, only US providers | US provider selected, attestation compliant=False, warning logged |
| TC-04: Write validation — strict violation | Task progress report on US node for EU-only tenant | HTTP 403, `ResidencyViolation`, write blocked |
| TC-05: Write validation — lenient warning | Same as TC-04 but `enforce_strict=False` | Write proceeds, warning logged, non-compliant attestation |
| TC-06: No policy configured | Tenant with no residency policy | All writes allowed, no attestation constraints |
| TC-07: Policy persistence round-trip | Set policy, restart controller, verify policy loaded | Policy survives restart from `.sdd/config/residency_policies.json` |
| TC-08: Attestation persistence | Create attestations, verify JSONL file written | `.sdd/audit/residency_attestations.jsonl` contains entries |
| TC-09: Evidence export includes residency | Export metrics with attestations | Residency attestation section present in export |
| TC-10: Region normalization | `required_region="EU"`, provider `region="eu-west"` | Match succeeds (case-insensitive, prefix match) |

---

## Assumptions

| # | Assumption | Where verified | Risk if wrong |
|---|---|---|---|
| A1 | `ProviderConfig.region` accurately reflects where API calls are routed | Not verified — provider self-declares | If provider lies about region, compliance is violated silently |
| A2 | Single-node deployment means `node_region` is consistent for all writes | Verified: Bernstein runs as single process | Low risk for single-node; must revisit for cluster mode |
| A3 | Tenant ID is always available in request context | Verified: `request_tenant_id()` in `tenanting.py` | If missing, residency check cannot run — must fail-closed |
| A4 | `.sdd/config/` is writable by the Bernstein process | Verified: other configs write there | Low |
| A5 | Attestation volume stays manageable (< 100K records/day) | Not verified | If volume is extreme, JSONL file grows unbounded — need retention/rotation |

## Open Questions

- Should residency violation on a task route cause the task to be requeued with a delay (hoping a compliant provider comes online), or immediately fail?
- Should the `DataResidencyController` be tenant-scoped (one per tenant) or global (one per orchestrator) as it is now?
- For cluster mode: should residency validation happen on the leader node or on the node that executes the task?
- Should `require_encryption_at_rest` be enforced in this workflow or as a separate encryption-at-rest workflow?

## Spec vs Reality Audit Log

| Date | Finding | Action taken |
|---|---|---|
| 2026-04-08 | Initial spec created from code audit of data_residency.py, router.py, compliance.py, orchestrator.py | 8 Reality Checker findings documented (RC-1 through RC-8) |
