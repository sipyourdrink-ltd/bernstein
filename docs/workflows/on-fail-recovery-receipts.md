# on_fail recovery receipts

**When an upstream node fails and my DAG routes to a recovery node, what does
the recovery agent start from?**

The workflow DSL lets a recovery node depend on an upstream node under a guard:

```yaml
nodes:
  run-tests:
    phase: verify
    role: qa
    depends_on: [build]

  fix-bugs:
    phase: implement
    role: backend
    depends_on:
      - source: run-tests
        condition: "status == 'failed'"
    retry:
      max_attempts: 3
      until: "status == 'done'"
```

When `run-tests` fails, `DAGExecutor` routes control to `fix-bugs`. The recovery
task it creates carries a **lineage-attested failure receipt** built from the
failure the run already captured, so the recovery agent continues from it
instead of re-deriving the diagnosis.

---

## What the receipt contains

| Field | Meaning |
|---|---|
| `failing_node_id` | The upstream node that failed (`run-tests`). |
| `recovery_node_id` | The recovery node (`fix-bugs`). |
| `source_status` | The failing task's terminal status. |
| `condition_context` | The guard evaluation context (`status` / `result` / `output`). |
| `gate_report` | The failing task's quality gate findings. |
| `journal_tail` | The tail of the run journal, filtered to the failing task, wall-clock stripped. |

The recovery task's prompt is prepended with a markdown preamble rendered from
the receipt (the same `orchestrator._context_recovery` channel that
context-degradation eviction uses), so the failing status, gate findings, and
journal tail reach the recovery agent directly.

## How it is anchored

The receipt is serialized canonically (sorted keys, minimal separators) and
recorded on the run's lineage spine. The spine returns a Merkle-chained,
HMAC-tagged entry hash, stamped on the recovery task metadata:

| Metadata key | Value |
|---|---|
| `recovery_receipt_hash` | The spine entry hash (present only when a spine is supplied). |
| `recovery_receipt_content_hash` | The receipt's content hash. |
| `recovery_failing_node` | The failing node id. |

Because the receipt bytes are the recorded content, the anchored entry binds to
the exact failure summary. A reviewer can prove offline which upstream failure a
recovery task was spawned to fix:

```bash
bernstein lineage verify <run_id> --receipt-hash sha256:<entry-hash>
```

See [Lineage - on_fail recovery receipts](../lineage.md#on_fail-recovery-receipts)
for verification detail and guarantees.

## Behaviour notes

- **No source, no receipt.** `create_task(node_id)` without a failing source
  task is byte-identical to a plain node instantiation - root and unconditional
  nodes are unchanged.
- **Guard routing is unchanged.** The receipt rides alongside the existing edge
  resolution; it does not alter which nodes become ready.
- **Determinism.** Two runs over identical fixtures produce a byte-identical
  receipt content hash and an identical spine entry hash.
