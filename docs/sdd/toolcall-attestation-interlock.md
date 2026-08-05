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
digest. Each attestation reference may authorize at most one dispatch. For an
identity-anchored run, every attestation must also carry a valid signed identity
envelope whose run, agent, scope, request, arguments, intent, monotonic call
index, frozen journal head, chain predecessor, anchor reference, key id, and
attestation time verify against the public key frozen at spawn. Missing,
reordered, duplicated, substituted, or mismatched evidence deterministically
downgrades to `observed`.

## Trust boundary

`NativeToolCallEvidenceProvider` is Bernstein's first implementation. It writes
the ordered `toolcall.attestation` and `toolcall.enforced_dispatch` records to
the existing HMAC audit chain while holding one cross-process chain
transaction. Raw tool arguments are never retained; only their canonical
SHA-256 digest is recorded. If the second append fails, no evidence handle is
returned, the connector remains unreachable in enforced mode, and the lone
attestation projects as `observed` rather than `complete`.

With a run identity, lineage signer, and journal-head reader configured, the
native provider additionally creates a versioned, JCS-canonicalized Ed25519
identity envelope before either marker is appended. Its signature input is
domain separated with `bernstein.toolcall.identity-attestation/v1`, so a valid
signature from another Bernstein evidence family cannot be replayed here. The
private key remains behind the narrow signer protocol and is never serialized.
The signed `attested_at_ns` comes from an injected clock, making replay tests
byte-deterministic.

Legacy HMAC-only construction remains supported. The identity extension does
**not** issue identities, evaluate policy, provide revocation, require hardware
keys, or introduce Nxtlinq (or any vendor) as a dependency. External providers
can implement the same interlock contract without becoming core dependencies.

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

`scripts/bench_toolcall_identity.py` separately compares the native signed
provider with the native HMAC-only provider at authenticated history depths 1,
1,000, and 10,000 under concurrency 32. It reports signed p95 overhead,
throughput regression, and p95-minus-p50 as a lock-wait proxy against the issue
budgets (1 ms p95 and 10 percent throughput regression).

The warm identity path preserves those invariants without repeating avoidable
work: it parses the frozen Ed25519 keys and immutable RFC 7797 header once,
reuses the already canonical record when hashing the signed envelope, and
skips rescanning self-written history only while a locked re-read proves the
audit head is exactly the provider's last known head. A different head resumes
the authenticated cursor and reconciles other-process attestations before the
next index is allocated. Offline verification still reconstructs the envelope
from first principles; no cache is a trust source.
