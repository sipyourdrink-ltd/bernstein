# Native Subagent Delegation

Bernstein keeps the *coordination plan* in its deterministic scheduler and
delegates the *mechanical execution* of each leaf task to a native subagent
primitive - Claude Code's `--agents` flag, Codex, or another adapter's own
sub-agent mechanism. `core/agents/subagent_delegation.py` is the boundary
that lets that composition stay replayable: the plan side is a pure,
hashable data structure, and only the boundary crossing (validating and
anchoring the native result) is a side effect.

## The plan is data, not a live call

An `OuterPlan` is an ordered tuple of `DispatchNode` leaves. Each node names
a native `target` (`claude`, `codex`, ...) and the per-agent knobs the
native primitive consumes - `model`, `effort`, `prompt`, `tools`,
`background`, `batch` - plus a `result_schema` the native structured output
must satisfy:

```python
from bernstein.core.agents.subagent_delegation import DispatchNode, OuterPlan, delegate_plan

node = DispatchNode(
    name="backend-leaf",
    target="claude",
    model="sonnet",
    effort="medium",
    prompt="Implement the endpoint per the spec.",
    result_schema={"type": "object", "properties": {"files_changed": {"type": "array"}}, "required": ["files_changed"]},
    tools=("edit", "bash"),
    batch=False,
)
plan = OuterPlan(nodes=(node,))
```

`DispatchNode.node_hash()` and `OuterPlan.plan_hash()` are pure functions of
these *plan* fields alone - never of what the native subagent returns - so
two byte-identical plans produce identical hashes across replays even
though the inner execution is stochastic. `OuterPlan` rejects duplicate
node names at construction.

## Crossing the boundary

The module does not invoke the native subagent itself - the caller runs
the native primitive (via the adapter surface) and passes the parsed
structured result into `dispatch_node()` or `delegate_plan()`:

```python
result = dispatch_node(
    node,
    native_result={"files_changed": ["app.py"]},
    journal=run_journal,          # optional
    chain=audit_chain_store,      # optional
    ledger=spend_ledger,          # optional
    undiscounted_cost_usd=0.02,
    input_tokens=1200,
    output_tokens=340,
)
```

`dispatch_node()` performs three steps in order:

1. **Schema validation at the worker boundary.** The native result is
   checked against the node's `result_schema`, sealed against additional
   properties. Hallucinated keys or missing required fields raise
   `NativeResultRejected` (a `SchemaViolation` subclass) *before* anything
   is anchored, so a bad payload never reaches the journal.
2. **Journal anchoring.** When a journal is supplied, the crossing is
   recorded as a `subagent.delegation` event carrying the node's
   replay-invariant `node_hash` and the (stochastic) `result_content_hash`
   (SHA-256 of the canonical validated payload). The outer DAG - the
   ordered sequence of `node_hash` values - therefore replays
   byte-identically even when the anchored result content differs run to
   run. `delegate_plan()` dispatches every node of a plan in strict plan
   order for exactly this reason.
3. **Cost attribution.** When a spend ledger is supplied, a `batch=True`
   node's cost is discounted by `BATCH_TIER_DISCOUNT` (0.5, i.e. the
   standard non-interactive batch rate) and tagged `tier=batch` in the
   ledger; `cache_read_tokens` / `cache_write_tokens` are recorded so
   `bernstein cost` can attribute prompt-cache savings.

When an `AuditChainStore` is also supplied, the same boundary is mirrored
into the HMAC-chained audit log via `record_subagent_delegation` (event
type `EVENT_SUBAGENT_DELEGATION = "subagent.delegation"`), cross-referencing
the run id, node hash, result content hash, and journal position.

## Guarantees

- **No live LLM in the coordination loop.** The outer plan's identity
  (`node_hash` / `plan_hash`) never depends on the native subagent's
  output, so the *coordination* graph is deterministic even though each
  leaf's *execution* is not.
- **Rejection before anchoring.** A native result that fails schema
  validation raises before any journal or audit write happens - a
  hallucinated or malformed payload cannot enter the replay record.
- **Deterministic ordering.** `delegate_plan()` always dispatches nodes in
  `OuterPlan.nodes` order, so the sequence of anchored events is a pure
  function of the plan.

## Limitations

- There is no dedicated CLI command for this boundary. It is an internal
  API called by the scheduler's dispatch path, not an operator-facing
  subcommand.
- `bernstein delegation verify <run>` verifies a **different** surface -
  the per-hop HMAC *authorization* receipt chain in
  `core/identity/delegation.py` (the ACT log: which principal authorized
  which sub-agent action). It does not reconstruct or verify
  `subagent.delegation` journal events. Verifying a run's delegation
  boundaries means replaying its journal (see
  [Deterministic replay](../operations/deterministic-replay.md)) and, when
  an audit chain was wired in, checking `bernstein audit verify` for the
  mirrored `subagent.delegation` audit events.
- `delegate_plan()` requires every plan node to have a matching entry in
  the caller-supplied `native_results` mapping; a missing entry raises
  `KeyError` rather than skipping the node.

## Source

`src/bernstein/core/agents/subagent_delegation.py`
