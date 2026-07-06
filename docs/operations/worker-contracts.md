# Worker completion contracts

Worker completion payloads used to be free-form prose in `result_summary`,
and a worker that could not proceed had no structured way to say so. The
completion contract defines the two closed terminal shapes a worker may
submit at the completion API boundary: a **completion** or a typed
**refusal**. An invalid payload is a typed `contract_violation` failure,
never a silent accept.

Source:

- `src/bernstein/core/tasks/contracts.py` - the contract and validators
- `src/bernstein/core/routes/task_crud.py` - completion-API enforcement

The contract is versioned (`worker-completion/v1`); the version and the
validation outcome are recorded in the task's ledger and audit trail for
every validated payload.

## The two shapes

### Completion

A successful terminal payload:

```json
{
  "contract": "worker-completion/v1",
  "summary": "Added JWT refresh-token rotation and tests.",
  "files_changed": ["src/auth/jwt.py", "tests/test_jwt.py"],
  "verification": {"command": "pytest tests/test_jwt.py", "exit_code": 0},
  "receipt_ref": "lineage:task-9f3a"
}
```

| Field | Required | Notes |
|-------|----------|-------|
| `summary` | yes | Non-empty string, capped at 2000 characters. |
| `files_changed` | no | List of non-empty path strings. |
| `verification` | no | `{command, exit_code}` - a step the worker ran before completing. |
| `receipt_ref` | no | Optional lineage/receipt reference. |

### Refusal

A worker that cannot proceed submits a typed refusal instead. The `kind`
is a **closed** taxonomy - there is deliberately no catch-all - so the
orchestrator can route every refusal deterministically:

| `kind` | Required payload field | Meaning |
|--------|------------------------|---------|
| `awaiting_operator` | `question` | Needs a human decision before continuing. |
| `underspecified` | `question` | The task is too vague to act on. |
| `scope_exceeded` | `proposed_split` (non-empty list) | Too large; proposes a split. |
| `blocked_on_dependency` | `blocking_dep` | Blocked on another task/resource. |

```json
{
  "contract": "worker-completion/v1",
  "kind": "scope_exceeded",
  "detail": "Task spans three subsystems; splitting.",
  "proposed_split": ["Migrate the auth module", "Migrate the billing module"]
}
```

## Validation is strict

At the completion API boundary the payload is validated against the closed
schema:

- Unknown top-level fields are rejected.
- Unknown refusal kinds are rejected.
- Missing required fields (per shape and per refusal kind) are rejected.
- Malformed JSON is rejected.

Every violation carries a JSONPath-style `path` (for example `$.summary`,
`$.proposed_split[1]`) so the failure is diagnosable from the ledger
alone.

### What happens on a violation

A payload that fails validation auto-fails the task with
`terminal_reason='contract_violation'`, releasing the worker slot
atomically. The completion API returns `422` with the schema error path
(or `409` if the task is already terminal). The violation path is recorded
in the audit trail.

### Legacy prose is still accepted

Backward compatibility is preserved: a `result_summary` that is plain
prose (does not open as a JSON object) is accepted unchanged. Only an
explicit `payload` object, or a `result_summary` that is itself a JSON
object, is validated against the contract. A truncated JSON document is a
contract violation rather than a silently accepted prose summary.

## Deterministic follow-up derivation

A `scope_exceeded` refusal is routed by deriving follow-up task specs from
its `proposed_split`. The derivation is a pure function of
`(parent_task_id, contract version, split index, split text)`: follow-up
task ids are content-addressed SHA-256 prefixes (`fu-<hash>`), so the same
refusal payload always produces the same follow-up set. A re-delivered
refusal cannot duplicate the split.

## Related

- [Task lifecycle](../architecture/LIFECYCLE.md)
- [Task lifecycle CLI](../reference/cli/task-lifecycle.md)
- [Audit log (operator guide)](../security/audit-log.md)
