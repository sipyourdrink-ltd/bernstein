# Supervisor surface

This document describes the JSON shapes the `bernstein supervisor`
command emits. Two surfaces are documented:

1. **Aggregated supervisor snapshot** - the body returned by
   `bernstein supervisor status --json` and embedded as the
   `supervisor` field in `bernstein status --json`.
2. **Signed escalation receipt** - the envelope persisted under
   `.sdd/runtime/supervisor/receipts/` whenever the supervisor or the
   operator escalates a stalled worker.

Both shapes are versioned via an explicit `schema_version` field.

## Supervisor snapshot

```jsonc
{
  "schema_version": "1.0.0",
  "generated_ts": 1700000000.0,
  "stuck_count": 2,
  "oldest_stall_age_s": 95.0,
  "parked_available": true,
  "workers": [
    {
      "worker_id": "abc123def456",
      "session_id": "sess-abc123",
      "role": "backend",
      "task_id": "t-12",
      "worktree_id": "wt-007",
      "last_heartbeat_age_s": 42.0,
      "is_stuck": false,
      "stall_reason": "unknown",
      "recommended_action": "inspect",
      "respawn_budget_remaining": 3,
      "stuck_since_ts": null,
      "details": {"status": "working"}
    }
  ]
}
```

### Fields

| Field | Type | Description |
|-------|------|-------------|
| `schema_version` | string | Aggregator schema version. Currently `1.0.0`. |
| `generated_ts` | float | Unix timestamp the snapshot was captured. |
| `stuck_count` | integer | Number of workers with `is_stuck=true`. |
| `oldest_stall_age_s` | float \| null | Age, in seconds, of the oldest currently-stuck worker; `null` when no worker is stuck or no stall timestamp is available. |
| `parked_available` | bool | Whether this run found a record of the spawn supervisor's parked-session store (the `.sdd/runtime/spawn_supervisor/parked.json` marker, or a `respawn_exhausted` failure-log fallback entry). `false` means the store was never written this run, so a `stuck_count` of `0` should not be read as "definitely nothing parked". |
| `workers[].worker_id` | string | Operator-decodable worker handle. |
| `workers[].session_id` | string | Adapter session id. |
| `workers[].role` | string | Worker role (`manager`, `backend`, `qa`, ...). |
| `workers[].task_id` | string | Current task id, or empty string. |
| `workers[].worktree_id` | string | Worktree the worker is running in. |
| `workers[].last_heartbeat_age_s` | float \| null | Seconds since the last heartbeat; `null` when none recorded. |
| `workers[].is_stuck` | bool | True iff at least one detector classifies the row as stuck. |
| `workers[].stall_reason` | string | One of `manager_no_children`, `watchdog_model_question`, `respawn_budget_exhausted`, `heartbeat_stale`, `no_progress`, or `unknown`. |
| `workers[].recommended_action` | string | One of `respawn`, `escalate`, `park`, `inspect`. Deterministic over the chain slice (see below). |
| `workers[].respawn_budget_remaining` | integer | Respawns remaining under the session's budget. |
| `workers[].stuck_since_ts` | float \| null | Unix timestamp the stall first fired; `null` when not known. |
| `workers[].details` | object | Free-form detector context. The aggregator currently includes `status` (raw agent status). |

## Escalation receipt envelope

```jsonc
{
  "schema_version": "1.0.0",
  "worker_id": "abc123def456",
  "worktree_id": "wt-007",
  "session_id": "sess-abc123",
  "stall_reason": "manager_no_children",
  "recommended_action": "escalate",
  "audit_entries": [
    {
      "event_type": "stalled_manager",
      "session_id": "sess-abc123",
      "details": {"runtime_s": 120.0, "hook_event_count": 12}
    }
  ],
  "identity": {
    "install_rev": "abc123def4567890",
    "keyid": "...64 hex chars...",
    "run_id": "run-2026-05-21-001"
  },
  "prev_chain_digest": "...64 hex chars...",
  "payload_digest": "...64 hex chars...",
  "signature_b64": "...base64 Ed25519 signature...",
  "details": {
    "operator_reason": "wedged on credential rotation",
    "respawn_budget_remaining": 0
  }
}
```

### Receipt fields

| Field | Type | Description |
|-------|------|-------------|
| `schema_version` | string | Receipt schema version. Currently `1.0.0`. |
| `worker_id` | string | Stable worker identifier. |
| `worktree_id` | string | Worktree the worker was running in. |
| `session_id` | string | Adapter session id. |
| `stall_reason` | string | Structured stall reason - same vocabulary as the aggregator. |
| `recommended_action` | string | Deterministic action - same vocabulary as the aggregator. |
| `audit_entries` | array of object | Captured chain slice (default 16 trailing entries) leading up to the stall. |
| `identity.install_rev` | string | Operator-decodable install fingerprint. |
| `identity.keyid` | string | sha256 of the Ed25519 public key (hex). |
| `identity.run_id` | string | Orchestrator run id, when known. |
| `prev_chain_digest` | string | HMAC of the previous audit-chain entry. Links the receipt into the tamper-evident audit log. |
| `payload_digest` | string | sha256 of the canonical signing payload. Lets verifiers detect a swapped signature blob. |
| `signature_b64` | string | base64-encoded Ed25519 signature over the canonical payload. |
| `details` | object | Free-form context. The CLI populates `operator_reason` and `respawn_budget_remaining`. |

### Determinism contract

`recommended_action` is a **pure function** of the receipt's
`(stall_reason, audit_entries, respawn_budget_remaining)`. The function

* never reads files or environment,
* never opens a socket,
* never reads a wall clock.

Two operators handed the same receipt bytes (or independently
reassembled receipts from the same chain prefix) compute the
byte-identical `recommended_action`. The contract is enforced by the
unit test
`tests/unit/test_supervisor_receipt.py::test_recommended_action_determinism`,
which drives the same chain slice through the function from two
different temp dirs and asserts equality.

### Cross-worktree fence

Every receipt asserts that the stuck session never crossed worktree
boundaries during the stall window. An audit entry whose
`event_type` ends in `.resolved` or starts with `cross_worktree.` and
references the stuck `session_id` from a sibling `worktree_id` is a
fence violation and aborts receipt assembly. Verifiers re-run the same
check from the receipt bytes alone, so a tampered audit slice that
smuggled a leak past assembly fails verification.

### Verification

The standalone verifier loads only the public side of the install
Ed25519 keypair (`<workdir>/.sdd/runtime/supervisor/install.key.pub`,
PEM-encoded). It

1. recomputes `payload_digest` over the canonical signing bytes and
   asserts byte-equality with the receipt's `payload_digest`,
2. re-asserts the cross-worktree fence,
3. re-derives `recommended_action` from the embedded slice and
   asserts equality with the receipt's `recommended_action`,
4. verifies the Ed25519 signature over the canonical bytes.

A receipt that survives all four checks is byte-portable: any auditor
holding the install's public key validates it offline without
contacting the orchestrator.
