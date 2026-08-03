# Tool-call attestation interlock

## Scope

The interlock is the orchestrator-owned boundary between evidence preparation
and a live connector side effect. It does not replace
`RunInstrumenter.log_tool_call`: instrumentation remains observe-only and may
never take a run down. The interlock is opt-in and applies only to calls that
cross a host dispatch hook such as `MCPGateway`.

## Contract

`ToolCallAttestationInterlock` accepts a `ToolCallEvidenceProvider` and an
opaque `scope_id`. The scope binds the provider's run, agent identity, and
authority context without requiring Bernstein's gateway to understand the
provider's identity, key, or policy schema.

For each pending call, the gateway derives a `ToolCallIntent` from:

- the opaque scope;
- server and method;
- tool name and JSON-RPC request id;
- the content-derived request span;
- a SHA-256 digest of canonicalized arguments.

The provider must verify and durably record the call's attestation and a
dispatch marker that references it. It returns opaque attestation and dispatch
references plus the exact intent digest. The interlock validates the scope,
non-empty references, and intent-digest equality before upstream I/O becomes
reachable.

## Modes and invariants

### Enforced

Any provider, verification, durability, empty-handle, or intent-mismatch
failure raises `ToolCallInterlockError`. The connector is invoked zero times.

### Observed

The same failure is logged and dispatch may continue. Receipt projection
reports `observed`; a caller-supplied `complete` label has no authority.

### Chain-derived verdict

`complete` requires at least one `toolcall.enforced_dispatch` marker and a
preceding `toolcall.attestation` with the same attestation reference and intent
digest. Missing, reordered, or mismatched evidence deterministically
downgrades to `observed`.

## Trust boundary

`NativeToolCallEvidenceProvider` is Bernstein's first implementation. It writes
the ordered `toolcall.attestation` and `toolcall.enforced_dispatch` records to
the existing HMAC audit chain while holding one cross-process chain
transaction. Raw tool arguments are never retained; only their canonical
SHA-256 digest is recorded. If the second append fails, no evidence handle is
returned, the connector remains unreachable in enforced mode, and the lone
attestation projects as `observed` rather than `complete`.

This native provider proves host-enforced capture and durable chain ordering.
It does **not** claim per-agent signed identity, issue an identity, or evaluate
agent policy. Those remain a separate provider layer. External providers can
implement the same contract without becoming core dependencies.

This boundary does not contain direct filesystem, process, network, or
connector effects that bypass the host hook. A completeness statement is valid
only for the dispatch surfaces wired through the interlock.

## Performance measurement

`scripts/bench_toolcall_interlock.py` compares full
`MCPGateway.handle_jsonrpc` dispatches with and without the enforced seam under
parallel load. The bundled in-process provider isolates host overhead; it does
not measure future signature verification or durable-chain storage. When a
native provider lands, use the same gateway-level harness so the measured path
still includes the actual interlock location.
